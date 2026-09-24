# -*- coding: utf-8 -*-
"""Рассылки: кампании, отправка по расписанию и разбор ответов.

Отправка идёт своим потоком, а не через общую очередь задач. Рассылка
тянется днями — по письму раз в несколько минут в рабочие часы, — и
в общей очереди она заперла бы поиск на всё это время.

Честно про условие, как и у повторов поиска: письма уходят, пока
программа открыта. Служб в системе она не заводит.

Главное правило модуля — не писать тем, кто ответил или отказался.
Поэтому перед каждым письмом условия проверяются заново, а не только
в момент добавления в кампанию: за три дня между шагами человек мог
ответить, попросить не писать или попасть в чёрный список.
"""
import datetime
import json
import random
import re
import threading
import time

from . import ai, db, enrich, mail

MAX_STEPS = 3
PLACEHOLDERS = ("имя", "руководитель", "компания", "город", "крючок",
                "первая_фраза", "подпись")
PH_RE = re.compile(r"\{([^{}\s]{1,30})\}")

# Шаблоны по умолчанию. Короткие намеренно: длинное холодное письмо
# не дочитывают, а напоминание длиннее трёх строк выглядит как нажим.
DEFAULT_STEPS = [
    {"delay_days": 0, "mode": "template",
     "subject": "{компания}: вопрос руководителю",
     "body": "Здравствуйте, {имя}!\n\n{первая_фраза}\n\n"
             "Коротко о нас: …\n\n"
             "Удобно ли обсудить это на 10 минут на этой неделе?\n\n"
             "{подпись}"},
    {"delay_days": 3, "mode": "template", "subject": "",
     "body": "{имя}, добрый день!\n\nПишу вдогонку — возможно, письмо "
             "выше затерялось. Актуален ли для вас этот вопрос? Достаточно "
             "ответить одним словом.\n\n{подпись}"},
    {"delay_days": 7, "mode": "template", "subject": "",
     "body": "{имя}, это последнее письмо по этому поводу. Если сейчас "
             "не время — ничего страшного, больше беспокоить не буду.\n\n"
             "{подпись}"},
]

PLACEHOLDER_GAP = "Коротко о нас: …"

# Догадка программы, а не найденный адрес. Писать на догадку — значит
# ловить возвраты, а каждый возврат портит репутацию ящика у почтовых
# служб: следующее письмо уже настоящему адресату уйдёт в спам.
GUESS_SOURCES = ("выведен по схеме домена",
                 "домен принимает любой адрес — это догадка")

ACTIVE = ("queued", "waiting")
FINAL_STOP = ("replied", "unsub")

STATUS_RU = {"queued": "ждёт отправки", "waiting": "ждём ответа",
             "replied": "ответили", "bounced": "не доставлено",
             "unsub": "отказались", "stopped": "остановлено",
             "error": "ошибка"}

PAUSE_MIN, PAUSE_MAX = 90, 240
POLL_EVERY = 300

# Состояние отправщика в памяти. В базу не пишется: после перезапуска
# программы начинать с чистого листа правильно — ошибку «нет сети» час
# назад незачем помнить.
STATE = {"next_send_at": 0.0, "pause_until": 0.0, "last_error": "",
         "error_at": 0.0, "last_sent": 0.0, "last_poll": 0.0,
         "poll_error": "", "poll_found": 0}
_lock = threading.Lock()


# ── Настройки отправки ───────────────────────────────────
def _int_setting(key, default, low, high):
    try:
        n = int(str(db.get_setting(key, "") or default).strip())
    except ValueError:
        n = default
    return max(low, min(high, n))


def hours():
    a = _int_setting("mail_hour_from", 9, 0, 23)
    b = _int_setting("mail_hour_to", 18, 1, 24)
    return (a, b) if b > a else (9, 18)


def in_window(ts=None):
    """Рабочее ли сейчас время для отправки."""
    t = time.localtime(ts if ts is not None else time.time())
    if t.tm_wday >= 5 and db.get_setting("mail_weekends", "0") != "1":
        return False
    a, b = hours()
    return a <= t.tm_hour < b


def _midnight(ts):
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def sent_today(ts=None):
    ts = ts if ts is not None else time.time()
    return db.conn().execute("SELECT COUNT(*) n FROM sent_mail WHERE sent_at>=?",
                             (int(_midnight(ts)),)).fetchone()["n"]


def day_limit(ts=None):
    """Сколько писем можно сегодня — с учётом прогрева нового ящика.

    Новый ящик, который с первого дня шлёт по сорок писем незнакомым
    людям, почтовые службы узнают по этому самому признаку. Первая
    неделя — пятнадцать, вторая — двадцать пять, дальше — сколько задано.
    """
    ts = ts if ts is not None else time.time()
    limit = _int_setting("mail_day_limit", 40, 1, 300)
    if db.get_setting("mail_warmup", "1") != "1":
        return limit
    first = db.conn().execute("SELECT MIN(sent_at) m FROM sent_mail").fetchone()["m"]
    days = 0 if not first else (ts - first) / 86400.0
    if days < 7:
        return min(limit, 15)
    if days < 14:
        return min(limit, 25)
    return limit


# ── Кампании ─────────────────────────────────────────────
def _clip(value, limit):
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    return str(value).strip()[:limit]


def unknown_placeholders(text):
    return sorted({p for p in PH_RE.findall(text or "") if p not in PLACEHOLDERS})


def clean_steps(raw):
    """Шаги кампании из того, что пришло с формы. Возвращает (шаги, ошибка)."""
    if not isinstance(raw, list) or not raw:
        return [], "нужен хотя бы один шаг"
    steps = []
    for i, s in enumerate(raw[:MAX_STEPS]):
        if not isinstance(s, dict):
            return [], "шаг %d заполнен неверно" % (i + 1)
        mode = "ai" if (s.get("mode") == "ai" and i == 0) else "template"
        try:
            delay = int(str(s.get("delay_days") or 0).strip() or 0)
        except ValueError:
            delay = 0
        delay = 0 if i == 0 else max(1, min(60, delay))
        subject = _clip(s.get("subject"), 200)
        body = _clip(s.get("body"), 5000)
        if mode == "template":
            if not body:
                return [], "в шаге %d нет текста письма" % (i + 1)
            if i == 0 and not subject:
                return [], "у первого письма нужна тема"
        # Заготовка по умолчанию нарочно не дописана: «Коротко о нас: …»
        # должен заполнить человек. Ушедшее с многоточием письмо читается
        # как рассылка, которую не глядя запустили.
        if PLACEHOLDER_GAP in body:
            return [], ("в шаге %d осталась заготовка «%s» — допишите, что вы "
                        "предлагаете" % (i + 1, PLACEHOLDER_GAP))
        bad = unknown_placeholders(subject + " " + body)
        if bad:
            return [], ("в шаге %d неизвестная подстановка {%s}. Доступны: %s"
                        % (i + 1, bad[0], ", ".join("{%s}" % p for p in PLACEHOLDERS)))
        steps.append({"delay_days": delay, "mode": mode,
                      "subject": subject, "body": body})
    return steps, ""


def save_campaign(name, steps, campaign_id=None):
    name = _clip(name, 120)
    if not name:
        return None, "назовите кампанию"
    steps, err = clean_steps(steps)
    if err:
        return None, err
    c = db.conn()
    blob = json.dumps(steps, ensure_ascii=False)
    if campaign_id:
        cur = c.execute("UPDATE campaigns SET name=?, steps=?, updated_at=? "
                        "WHERE id=?", (name, blob, db.now(), int(campaign_id)))
        c.commit()
        if not cur.rowcount:
            return None, "кампания не найдена"
        _reschedule(int(campaign_id), steps)
        return int(campaign_id), ""
    cur = c.execute("INSERT INTO campaigns (name, steps, status, created_at, "
                    "updated_at) VALUES (?,?,'active',?,?)",
                    (name, blob, db.now(), db.now()))
    c.commit()
    return cur.lastrowid, ""


def _reschedule(campaign_id, steps):
    """После правки шагов: кто дождался конца цепочки, а шагов стало больше
    — снова в очереди; у кого шагов стало меньше — ждёт ответа."""
    c = db.conn()
    n = len(steps)
    c.execute("UPDATE outreach SET status='waiting', updated_at=? "
              "WHERE campaign_id=? AND status='queued' AND step>=?",
              (db.now(), campaign_id, n))
    for r in c.execute("SELECT id, step, sent_at FROM outreach "
                       "WHERE campaign_id=? AND status='waiting' AND step<?",
                       (campaign_id, n)).fetchall():
        nxt = (r["sent_at"] or db.now()) + steps[r["step"]]["delay_days"] * 86400
        c.execute("UPDATE outreach SET status='queued', next_at=?, updated_at=? "
                  "WHERE id=?", (nxt, db.now(), r["id"]))
    c.commit()


def get_campaign(campaign_id):
    row = db.conn().execute("SELECT * FROM campaigns WHERE id=?",
                            (int(campaign_id),)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["steps"] = json.loads(d["steps"] or "[]")
    except ValueError:
        d["steps"] = []
    return d


def list_campaigns():
    c = db.conn()
    out = []
    for row in c.execute("SELECT * FROM campaigns ORDER BY id DESC"):
        d = get_campaign(row["id"])
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM outreach WHERE campaign_id=? "
            "GROUP BY status", (row["id"],))}
        d["counts"] = counts
        d["total"] = sum(counts.values())
        d["letters"] = c.execute("SELECT COUNT(*) n FROM sent_mail "
                                 "WHERE campaign_id=?", (row["id"],)).fetchone()["n"]
        out.append(d)
    return out


def set_status(campaign_id, status):
    if status not in ("active", "paused"):
        return False
    c = db.conn()
    cur = c.execute("UPDATE campaigns SET status=?, updated_at=? WHERE id=?",
                    (status, db.now(), int(campaign_id)))
    c.commit()
    return cur.rowcount > 0


def delete_campaign(campaign_id):
    """Удалить кампанию и её очередь. Отправленные письма остаются в
    журнале: ответ на них может прийти и через месяц, и узнать его надо."""
    c = db.conn()
    c.execute("DELETE FROM outreach WHERE campaign_id=?", (int(campaign_id),))
    cur = c.execute("DELETE FROM campaigns WHERE id=?", (int(campaign_id),))
    c.commit()
    return cur.rowcount > 0


def rows(campaign_id, status="", limit=300):
    c = db.conn()
    sql = ("SELECT o.*, co.name company, co.stage stage FROM outreach o "
           "JOIN companies co ON co.id=o.company_id WHERE o.campaign_id=?")
    args = [int(campaign_id)]
    if status:
        sql += " AND o.status=?"
        args.append(status)
    sql += " ORDER BY o.updated_at DESC, o.id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r, status_ru=STATUS_RU.get(r["status"], r["status"]))
            for r in c.execute(sql, args)]


def stop_row(outreach_id):
    c = db.conn()
    cur = c.execute("UPDATE outreach SET status='stopped', updated_at=? "
                    "WHERE id=? AND status IN ('queued','waiting','error')",
                    (db.now(), int(outreach_id)))
    c.commit()
    return cur.rowcount > 0


# ── Кому писать ──────────────────────────────────────────
def opted_out(email):
    return db.conn().execute("SELECT 1 FROM mail_optout WHERE email=?",
                             ((email or "").strip().lower(),)).fetchone() is not None


def optout_add(email, reason=""):
    c = db.conn()
    c.execute("INSERT OR IGNORE INTO mail_optout (email, reason, created_at) "
              "VALUES (?,?,?)", ((email or "").strip().lower(), reason[:200], db.now()))
    c.commit()


def pick_address(company_id):
    """Лучший адрес компании для письма. Возвращает (id контакта, адрес, причина).

    Руководитель впереди общего ящика; подтверждённый сервером — впереди
    неподтверждённого. Отвергнутые сервером и выведенные по схеме
    пропускаются: на них придёт возврат.
    """
    best, why = None, ""
    for r in db.conn().execute(
            "SELECT id, value, owner, verified, source FROM contacts "
            "WHERE company_id=? AND kind='email' "
            "ORDER BY (owner='director') DESC, (verified='ok') DESC, "
            "confidence DESC, id", (company_id,)):
        addr = (r["value"] or "").strip().lower()
        if not mail.ADDR_RE.match(addr):
            continue
        if r["verified"] == "bad":
            why = why or "адрес не существует"
            continue
        if r["verified"] != "ok" and (r["source"] or "") in GUESS_SOURCES:
            why = why or "только угаданный адрес"
            continue
        if opted_out(addr):
            why = why or "просили не писать"
            continue
        best = (r["id"], addr)
        break
    if best is None:
        return None, "", why or "нет почты"
    return best[0], best[1], ""


def _blocked(company):
    """Почему этой компании писать нельзя. Пустая строка — можно."""
    if (company.get("stage") or "") == "отказ":
        return "стадия «отказ»"
    if db.is_blacklisted(company):
        return "в чёрном списке"
    done = db.conn().execute(
        "SELECT status FROM outreach WHERE company_id=? AND status IN "
        "('replied','unsub') LIMIT 1", (company["id"],)).fetchone()
    if done is not None:
        return "уже ответили" if done["status"] == "replied" else "просили не писать"
    return ""


def enroll(campaign_id, company_ids):
    """Добавить компании в кампанию. Возвращает счётчики по исходам."""
    camp = get_campaign(campaign_id)
    if camp is None:
        return None
    c = db.conn()
    out = {"added": 0, "busy": 0, "no_email": 0, "guess": 0, "refused": 0,
           "blocked": 0}
    for cid in company_ids:
        row = c.execute("SELECT * FROM companies WHERE id=?", (int(cid),)).fetchone()
        if row is None:
            continue
        company = dict(row)
        if _blocked(company):
            out["blocked"] += 1
            continue
        busy = c.execute("SELECT 1 FROM outreach WHERE company_id=? AND "
                         "(campaign_id=? OR status IN ('queued','waiting'))",
                         (company["id"], camp["id"])).fetchone()
        if busy is not None:
            out["busy"] += 1
            continue
        contact_id, addr, why = pick_address(company["id"])
        if not addr:
            key = {"только угаданный адрес": "guess",
                   "просили не писать": "refused"}.get(why, "no_email")
            out[key] += 1
            continue
        c.execute("INSERT OR IGNORE INTO outreach (campaign_id, company_id, "
                  "contact_id, email, step, status, next_at, created_at, "
                  "updated_at) VALUES (?,?,?,?,0,'queued',?,?,?)",
                  (camp["id"], company["id"], contact_id, addr, db.now(),
                   db.now(), db.now()))
        out["added"] += 1
    c.commit()
    return out


# ── Текст письма ─────────────────────────────────────────
def _human_case(s):
    """«ИВАНОВ ИВАН» → «Иванов Иван». Смешанный регистр не трогаем."""
    s = (s or "").strip()
    if s and s.upper() == s and any(ch.isalpha() for ch in s):
        return " ".join("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))
                        for w in s.split())
    return s


def address_name(director):
    """Как обратиться: «Иван Петрович». Без отчества — просто имя."""
    parts = enrich.split_fio(_human_case(director))
    if not parts or not parts[1]:
        return ""
    return " ".join(p for p in parts[1:] if p)


def company_title(name):
    """Название для письма: без «ООО» и кавычек, без крика заглавными."""
    s = db._OPF_HEAD.sub("", (name or "").strip())
    s = re.sub(r"[«»\"“”„]", "", s).strip()
    if s.upper() == s and len(s) > 4:
        s = _human_case(s)
    return s or (name or "").strip()


def values_for(company, sign=""):
    return {"имя": address_name(company.get("director")),
            "руководитель": _human_case(company.get("director")),
            "компания": company_title(company.get("name")),
            "город": (company.get("region") or "").strip(),
            "крючок": (company.get("ai_hook") or "").strip(),
            "первая_фраза": (company.get("ai_opener") or "").strip(),
            "подпись": (sign or "").strip()}


def fill(template, values):
    """Подставить значения. Пустое значение убирается вместе с запятой:
    «Здравствуйте, {имя}!» без имени — «Здравствуйте!», а не «Здравствуйте, !»."""
    s = template or ""
    for key, val in values.items():
        ph = "{%s}" % key
        if ph not in s:
            continue
        if val:
            s = s.replace(ph, val)
            continue
        # «{имя}, добрый день!» → «Добрый день!»
        s = re.sub(r"(^|\n)[ \t]*%s[ \t]*,?[ \t]*(\w)" % re.escape(ph),
                   lambda m: m.group(1) + m.group(2).upper(), s)
        s = re.sub(r"[ \t]*,[ \t]*%s" % re.escape(ph), "", s)
        s = s.replace(ph, "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def finish_body(body, template, sign):
    """Подпись, если шаблон её не поставил, и строка про отписку — всегда."""
    body = (body or "").strip()
    if sign and "{подпись}" not in (template or "") and sign.strip() not in body:
        body += "\n\n" + sign.strip()
    return body + "\n\n" + mail.OPT_OUT_LINE


def _facts(company_id):
    c = db.conn()
    row = c.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if row is None:
        return None, {}, []
    sig = {r["key"]: r["value"] for r in
           c.execute("SELECT key, value FROM signals WHERE company_id=?", (company_id,))}
    cts = [dict(x) for x in c.execute(
        "SELECT kind, value, owner, confidence, verified, source "
        "FROM contacts WHERE company_id=? ORDER BY confidence DESC", (company_id,))]
    return dict(row), sig, cts


def compose(company, step_no, steps, sign, first_subject="", use_ai=True):
    """Тема и текст шага для компании. Возвращает (тема, текст, ошибка)."""
    st = steps[step_no]
    vals = values_for(company, sign)
    if st["mode"] == "ai" and step_no == 0:
        if not use_ai:
            return "", "", "ИИ напишет письмо по карточке в момент отправки"
        if not db.get_setting("ai_key", ""):
            return "", "", "первое письмо пишет ИИ, а ключ ИИ не задан"
        comp, sig, cts = _facts(company["id"])
        letter, err = ai.letter(comp or company, sig, cts,
                                offer=db.get_setting("ai_offer", ""),
                                icp=db.get_setting("ai_icp", ""))
        if err:
            return "", "", "ИИ не написал письмо: %s" % err
        return letter["subject"], finish_body(letter["body"], "", sign), ""
    subject = fill(st["subject"], vals) if st["subject"] else ""
    if not subject:
        base = first_subject or fill(steps[0].get("subject") or "", vals)
        subject = ("Re: " + base) if base and not base.lower().startswith("re:") else base
    body = fill(st["body"], vals)
    return subject, finish_body(body, st["body"], sign), ""


def preview(campaign_id, company_ids=None, limit=3):
    """Как письма выглядят на живых компаниях — до того, как они уйдут."""
    camp = get_campaign(campaign_id)
    if camp is None:
        return None
    c = db.conn()
    ids = [int(x) for x in (company_ids or [])][:limit]
    if not ids:
        ids = [r["company_id"] for r in c.execute(
            "SELECT company_id FROM outreach WHERE campaign_id=? "
            "ORDER BY status='queued' DESC, RANDOM() LIMIT ?",
            (camp["id"], limit))]
    if not ids:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM companies co WHERE EXISTS (SELECT 1 FROM contacts t "
            "WHERE t.company_id=co.id AND t.kind='email') "
            "ORDER BY score DESC LIMIT ?", (limit,))]
    sign = mail.conf_from_db().get("sign", "")
    out = []
    for cid in ids:
        row = c.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if row is None:
            continue
        company = dict(row)
        _, addr, why = pick_address(cid)
        letters, first = [], ""
        for i, st in enumerate(camp["steps"]):
            subj, body, note = compose(company, i, camp["steps"], sign,
                                       first_subject=first, use_ai=False)
            if i == 0:
                first = subj
            letters.append({"step": i + 1, "delay_days": st["delay_days"],
                            "subject": subj, "body": body, "note": note})
        out.append({"company": company["name"], "id": cid, "email": addr,
                    "why": why, "letters": letters})
    return out


# ── Отправка ─────────────────────────────────────────────
def _thread_of(outreach_id):
    return [dict(r) for r in db.conn().execute(
        "SELECT step, subject, message_id FROM sent_mail WHERE outreach_id=? "
        "ORDER BY step, id", (outreach_id,))]


def _note(company_id, text):
    db.add_note(company_id, text[:1900])


def _due_row(ts):
    return db.conn().execute(
        "SELECT o.* FROM outreach o JOIN campaigns k ON k.id=o.campaign_id "
        "WHERE k.status='active' AND o.status='queued' AND o.next_at<=? "
        "ORDER BY o.next_at, o.id LIMIT 1", (int(ts),)).fetchone()


def _fail(msg):
    STATE["last_error"] = msg
    STATE["error_at"] = time.time()


def tick(ts=None, ignore_pause=False):
    """Отправить одно письмо, если пора. Возвращает, что произошло."""
    with _lock:
        return _tick(ts if ts is not None else time.time(), ignore_pause)


def _tick(ts, ignore_pause):
    conf = mail.conf_from_db()
    if mail.problem(conf):
        return "not_configured"
    if not ignore_pause:
        if ts < STATE["pause_until"]:
            return "paused"
        if ts < STATE["next_send_at"]:
            return "waiting"
        if not in_window(ts):
            return "outside_hours"
    if sent_today(ts) >= day_limit(ts):
        return "day_limit"
    row = _due_row(ts)
    if row is None:
        return "nothing_due"
    row = dict(row)
    c = db.conn()
    camp = get_campaign(row["campaign_id"])
    company, _, _ = _facts(row["company_id"])
    if camp is None or company is None:
        c.execute("DELETE FROM outreach WHERE id=?", (row["id"],))
        c.commit()
        return "dropped"
    steps = camp["steps"]
    if row["step"] >= len(steps):
        c.execute("UPDATE outreach SET status='waiting', updated_at=? WHERE id=?",
                  (db.now(), row["id"]))
        c.commit()
        return "finished"

    # Условия проверяются заново перед каждым письмом — см. шапку модуля.
    blocked = _blocked(company)
    if not blocked and opted_out(row["email"]):
        blocked = "просили не писать"
    if blocked:
        c.execute("UPDATE outreach SET status='stopped', error=?, updated_at=? "
                  "WHERE id=?", (blocked, db.now(), row["id"]))
        c.commit()
        return "stopped"

    thread = _thread_of(row["id"])
    first_subject = thread[0]["subject"] if thread else ""
    subject, body, err = compose(company, row["step"], steps, conf["sign"],
                                 first_subject=first_subject)
    if err:
        # Сбой ИИ бывает временным: пробуем ещё дважды через полчаса.
        tries = (row["tries"] or 0) + 1
        status = "error" if tries >= 3 else "queued"
        c.execute("UPDATE outreach SET tries=?, status=?, error=?, next_at=?, "
                  "updated_at=? WHERE id=?",
                  (tries, status, err[:300], int(ts) + 1800, db.now(), row["id"]))
        c.commit()
        return "compose_error"

    ids = [t["message_id"] for t in thread if t["message_id"]]
    msg = mail.build(conf, row["email"], subject, body,
                     in_reply_to=ids[-1] if ids else "", references=ids)
    err, kind = mail.send(conf, msg)
    if err:
        if kind == mail.RECIPIENT:
            c.execute("UPDATE outreach SET status='bounced', error=?, updated_at=? "
                      "WHERE id=?", (err[:300], db.now(), row["id"]))
            _mark_bad(row["company_id"], row["email"])
            c.commit()
            _note(row["company_id"], "Письмо не отправлено: %s" % err)
            return "bounced"
        # Беда у нас, а не у адресата: строку не трогаем, отправку
        # придерживаем. Спам — надолго, остальное — на четверть часа.
        spam = "спам" in err
        STATE["pause_until"] = ts + (6 * 3600 if spam else 900)
        _fail(err)
        return "send_error"

    now_i = int(ts)
    c.execute("INSERT INTO sent_mail (outreach_id, campaign_id, company_id, step, "
              "email, subject, body, message_id, sent_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (row["id"], row["campaign_id"], row["company_id"], row["step"],
               row["email"], subject, body, msg["Message-ID"], now_i))
    nxt = row["step"] + 1
    if nxt < len(steps):
        c.execute("UPDATE outreach SET step=?, status='queued', next_at=?, "
                  "sent_at=?, tries=0, error='', updated_at=? WHERE id=?",
                  (nxt, now_i + steps[nxt]["delay_days"] * 86400, now_i,
                   db.now(), row["id"]))
    else:
        c.execute("UPDATE outreach SET step=?, status='waiting', sent_at=?, "
                  "tries=0, error='', updated_at=? WHERE id=?",
                  (nxt, now_i, db.now(), row["id"]))
    # Стадию двигаем только вперёд: «созвон» из-за напоминания обратно
    # в «написали» не превращается.
    if (company.get("stage") or "new") in ("new", "в работе"):
        c.execute("UPDATE companies SET stage='написали', updated_at=? WHERE id=?",
                  (db.now(), row["company_id"]))
    c.commit()
    _note(row["company_id"], "Письмо %d из %d «%s» ушло на %s (кампания «%s»)."
          % (row["step"] + 1, len(steps), subject, row["email"], camp["name"]))
    STATE["last_sent"] = ts
    STATE["last_error"] = ""
    STATE["next_send_at"] = ts + random.uniform(PAUSE_MIN, PAUSE_MAX)
    return "sent"


def _mark_bad(company_id, email):
    c = db.conn()
    c.execute("UPDATE contacts SET verified='bad' WHERE company_id=? AND "
              "kind='email' AND lower(value)=?", (company_id, (email or "").lower()))
    c.commit()


# ── Ответы ───────────────────────────────────────────────
def _company_for(item):
    """Какой нашей компании адресован ответ. Возвращает строку sent_mail."""
    c = db.conn()
    refs = item.get("refs") or []
    if item.get("bounce"):
        refs = list(refs) + list(item.get("bounce_refs") or [])
    for ref in refs:
        hit = c.execute("SELECT * FROM sent_mail WHERE message_id=? "
                        "ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
        if hit is not None:
            return hit
    addrs = list(item.get("failed") or []) if item.get("bounce") else [item.get("from")]
    for addr in addrs:
        if not addr:
            continue
        hit = c.execute("SELECT * FROM sent_mail WHERE lower(email)=? "
                        "ORDER BY id DESC LIMIT 1", (addr.lower(),)).fetchone()
        if hit is not None:
            return hit
    return None


def handle(items, ts=None):
    """Разнести найденные письма по компаниям. Возвращает счётчики."""
    ts = ts if ts is not None else time.time()
    out = {"replied": 0, "unsub": 0, "bounced": 0, "auto": 0}
    c = db.conn()
    today = datetime.date.fromtimestamp(ts).isoformat()
    for item in items:
        hit = _company_for(item)
        if hit is None:
            continue
        cid, email = hit["company_id"], hit["email"]
        snippet = (item.get("text") or "").strip()[:500]
        if item.get("bounce"):
            c.execute("UPDATE outreach SET status='bounced', error=?, updated_at=? "
                      "WHERE company_id=? AND lower(email)=? AND status IN "
                      "('queued','waiting')",
                      ("письмо вернулось", db.now(), cid, email.lower()))
            c.commit()
            _mark_bad(cid, email)
            _note(cid, "Письмо на %s не доставлено — адрес помечен как "
                       "несуществующий." % email)
            out["bounced"] += 1
            continue
        if item.get("auto"):
            # Автоответ «я в отпуске» — не ответ: цепочка идёт дальше.
            _note(cid, "Автоответ от %s: %s" % (item.get("from"), snippet[:200]))
            out["auto"] += 1
            continue
        if mail.is_refusal(item.get("text"), item.get("subject")):
            c.execute("UPDATE outreach SET status='unsub', updated_at=? "
                      "WHERE company_id=? AND status IN ('queued','waiting')",
                      (db.now(), cid))
            c.execute("UPDATE companies SET stage='отказ', next_step='', "
                      "next_date='', updated_at=? WHERE id=?", (db.now(), cid))
            c.commit()
            optout_add(email, "ответили отказом")
            if item.get("from") and item["from"] != email.lower():
                optout_add(item["from"], "ответили отказом")
            _note(cid, "Попросили больше не писать (%s): %s"
                  % (item.get("from"), snippet or item.get("subject", "")))
            out["unsub"] += 1
            continue
        c.execute("UPDATE outreach SET status='replied', updated_at=? "
                  "WHERE company_id=? AND status IN ('queued','waiting','error')",
                  (db.now(), cid))
        cur = c.execute("SELECT next_date FROM companies WHERE id=?",
                        (cid,)).fetchone()
        nd = (cur["next_date"] or "") if cur is not None else ""
        c.execute("UPDATE companies SET stage='ответили', next_step=?, "
                  "next_date=?, updated_at=? WHERE id=?",
                  ("ответить на письмо", nd if nd and nd <= today else today,
                   db.now(), cid))
        c.commit()
        _note(cid, "Ответ на письмо от %s: %s"
              % (item.get("from"), snippet or "(без текста)"))
        out["replied"] += 1
    return out


def poll(ts=None):
    """Забрать новые письма и разнести их. Возвращает счётчики или None."""
    ts = ts if ts is not None else time.time()
    STATE["last_poll"] = ts
    conf = mail.conf_from_db()
    if not mail.can_read(conf):
        STATE["poll_error"] = "" if mail.problem(conf) else "IMAP не настроен"
        return None
    first = db.conn().execute("SELECT MIN(sent_at) m FROM sent_mail").fetchone()["m"]
    if not first:
        STATE["poll_error"] = ""
        return {"replied": 0, "unsub": 0, "bounced": 0, "auto": 0}
    last = _int_setting("mail_imap_uid", 0, 0, 2 ** 62)
    items, validity, top, err = mail.fetch_new(conf, last_uid=last,
                                               since_ts=first - 86400)
    # Номера писем действительны, пока не сменился UIDVALIDITY. Сменился —
    # ящик пересоздан или переехал, и старый номер ничего не значит.
    old = db.get_setting("mail_imap_validity", "")
    if validity and old and validity != old and last:
        items, validity, top, err = mail.fetch_new(conf, last_uid=0,
                                                   since_ts=first - 86400)
    if validity:
        db.set_setting("mail_imap_validity", validity)
    if top and top > last:
        db.set_setting("mail_imap_uid", top)
    STATE["poll_error"] = err
    got = handle(items, ts)
    STATE["poll_found"] += sum(got.values())
    return got


# ── Поток ────────────────────────────────────────────────
def loop():
    while True:
        time.sleep(20)
        try:
            tick()
        except Exception as e:
            _fail("сбой отправщика: %s" % str(e)[:200])
        try:
            if time.time() - STATE["last_poll"] >= POLL_EVERY:
                poll()
        except Exception as e:
            STATE["poll_error"] = "сбой чтения ответов: %s" % str(e)[:200]


def reset_pause():
    """После правки настроек ящика — пробовать сразу, а не через 15 минут."""
    STATE["pause_until"] = 0.0
    STATE["last_error"] = ""


def status(ts=None):
    ts = ts if ts is not None else time.time()
    conf = mail.conf_from_db()
    c = db.conn()
    queued = c.execute(
        "SELECT COUNT(*) n FROM outreach o JOIN campaigns k ON k.id=o.campaign_id "
        "WHERE k.status='active' AND o.status='queued'").fetchone()["n"]
    due = c.execute(
        "SELECT COUNT(*) n FROM outreach o JOIN campaigns k ON k.id=o.campaign_id "
        "WHERE k.status='active' AND o.status='queued' AND o.next_at<=?",
        (int(ts),)).fetchone()["n"]
    a, b = hours()
    return {
        "configured": not mail.problem(conf),
        "problem": mail.problem(conf),
        "can_read": mail.can_read(conf),
        "address": conf.get("address", ""),
        "in_window": in_window(ts),
        "hours": "%02d:00–%02d:00" % (a, b),
        "weekends": db.get_setting("mail_weekends", "0") == "1",
        "sent_today": sent_today(ts),
        "day_limit": day_limit(ts),
        "queued": queued,
        "due": due,
        "next_send_in": max(0, int(STATE["next_send_at"] - ts)),
        "paused_for": max(0, int(STATE["pause_until"] - ts)),
        "last_error": STATE["last_error"],
        "last_poll_ago": int(ts - STATE["last_poll"]) if STATE["last_poll"] else None,
        "poll_error": STATE["poll_error"],
        "replies_total": c.execute("SELECT COUNT(*) n FROM outreach "
                                   "WHERE status='replied'").fetchone()["n"],
        # Ответили, а мы ещё нет: стадия «ответили» держится, пока
        # человек не перевёл компанию дальше.
        "answer_waiting": c.execute("SELECT COUNT(*) n FROM companies "
                                    "WHERE stage='ответили'").fetchone()["n"],
    }
