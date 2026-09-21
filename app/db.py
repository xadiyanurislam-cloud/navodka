# -*- coding: utf-8 -*-
"""SQLite: схема, миграции и доступ.

Почему SQLite, а не файлы: программа обходит тысячи компаний часами, и её
закроют посреди работы — это норма, а не авария. Состояние задачи должно
лежать на диске после каждого шага, иначе после закрытия окна всё начнётся
заново.

Соединение на поток. sqlite3 по умолчанию запрещает делить соединение между
потоками, а у нас фоновый воркер пишет одновременно с тем, как интерфейс
читает таблицу.
"""
import re
import json
import sqlite3
import threading
import time

from . import settings

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    inn         TEXT,
    ogrn        TEXT,
    director    TEXT,
    director_post TEXT,
    okved       TEXT,
    okved_name  TEXT,
    region      TEXT,
    address     TEXT,
    employees   INTEGER,
    founded     INTEGER,
    status      TEXT,
    capital     INTEGER,
    branches    INTEGER,
    founders_count INTEGER,
    founders    TEXT,
    okveds_extra TEXT,
    growth      TEXT,
    ai_summary  TEXT,
    ai_segment  TEXT,
    ai_fit      INTEGER,
    ai_why      TEXT,
    ai_hook     TEXT,
    ai_opener   TEXT,
    activity    TEXT,
    cms         TEXT,
    callcenter  TEXT,
    site        TEXT,
    hh_id       TEXT,
    score       INTEGER DEFAULT 0,
    stage       TEXT DEFAULT 'new',
    note        TEXT,
    source      TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_inn  ON companies(inn) WHERE inn IS NOT NULL AND inn <> '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_hh   ON companies(hh_id) WHERE hh_id IS NOT NULL AND hh_id <> '';
CREATE INDEX IF NOT EXISTS        idx_companies_score ON companies(score DESC);

CREATE TABLE IF NOT EXISTS contacts (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- email | phone | telegram | site
    value       TEXT NOT NULL,
    owner       TEXT,               -- director | sales | hr | general | unknown
    confidence  INTEGER DEFAULT 50, -- 0..100
    verified    TEXT,               -- ok | catch_all | bad | unchecked
    source      TEXT,               -- откуда взято: ЕГРЮЛ, сайт, вакансия...
    created_at  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_uniq ON contacts(company_id, kind, value);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    created_at  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_uniq ON signals(company_id, key);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    params      TEXT,
    status      TEXT DEFAULT 'queued',   -- queued | running | done | error | stopped
    done        INTEGER DEFAULT 0,
    total       INTEGER DEFAULT 0,
    message     TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER,
    level       TEXT,
    text        TEXT,
    created_at  INTEGER
);

-- Компании, к которым больше не возвращаемся. Хранится ключ, а не
-- ссылка на строку: компанию удаляют из базы, а помнить о ней надо —
-- иначе следующий поиск приведёт её обратно.
CREATE TABLE IF NOT EXISTS blacklist (
    key        TEXT PRIMARY KEY,   -- inn:… | hh:… | name:…
    name       TEXT,
    reason     TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Когда какой сайт обходили в последний раз.
--
-- Второй прогон по той же нише иначе заново стучится в те же сотни
-- сайтов: полчаса ожидания ради данных, которые уже лежат в базе.
CREATE TABLE IF NOT EXISTS site_visits (
    host        TEXT PRIMARY KEY,
    visited_at  INTEGER,
    pages       INTEGER,
    ok          INTEGER
);

-- Заметки по компании: что сказали, о чём договорились.
--
-- Отдельной таблицей, а не полем: разговоров бывает несколько, и
-- затирать предыдущий следующим — значит терять ровно то, ради чего
-- заметка и пишется.
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER NOT NULL,
    text        TEXT NOT NULL,
    created_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_notes_company ON notes(company_id, id DESC);

-- Сохранённые поиски. Набор «запросы + регионы + период + глубина»
-- складывается один раз и потом повторяется еженедельно: вакансии
-- обновляются, компании появляются новые, а условия те же. Набирать их
-- заново каждый раз — это и потеря времени, и разные условия от прогона
-- к прогону, из-за которых непонятно, что изменилось.
CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT DEFAULT 'hh_search',
    params      TEXT,
    runs        INTEGER DEFAULT 0,
    last_run    INTEGER,
    created_at  INTEGER
);
"""


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(settings.db_path(), timeout=30)
        c.row_factory = sqlite3.Row
        # WAL нужен именно здесь: воркер пишет, интерфейс читает, и без него
        # каждый опрос таблицы блокировал бы запись.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


# Колонки, добавленные после первого выпуска. CREATE TABLE IF NOT EXISTS
# на существующей таблице ничего не меняет, поэтому базу пользователя,
# заведённую прошлой версией, надо дополнять отдельно — иначе обновление
# программы уронит её на первом же запросе.
_LATER = {
    "founded": "INTEGER", "activity": "TEXT", "cms": "TEXT",
    "callcenter": "TEXT", "status": "TEXT", "capital": "INTEGER",
    "branches": "INTEGER", "founders_count": "INTEGER", "founders": "TEXT",
    "okveds_extra": "TEXT", "growth": "TEXT",
    # Результат работы модели живёт отдельно и никогда не перезаписывает
    # факты из ЕГРЮЛ и ФНС: смешать разобранное с сочинённым — значит
    # потерять возможность отличить одно от другого.
    "ai_summary": "TEXT", "ai_segment": "TEXT", "ai_fit": "INTEGER",
    "ai_why": "TEXT", "ai_hook": "TEXT", "ai_opener": "TEXT",
    "ai_kp": "TEXT",
    # Что делаем с компанией дальше и когда. Без этих двух полей список
    # через неделю превращается в кашу: стадия говорит, где компания, но
    # не говорит, чья сейчас очередь ходить.
    "next_step": "TEXT", "next_date": "TEXT",
}


# То же для сохранённых поисков: расписание появилось позже самой
# таблицы, и база, заведённая прошлой версией, о нём не знает.
_LATER_SEARCHES = {
    "every_days": "INTEGER DEFAULT 0",
    "next_run": "INTEGER",
    "enabled": "INTEGER DEFAULT 1",
}


def init():
    c = conn()
    c.executescript(SCHEMA)
    for table, cols in (("companies", _LATER), ("searches", _LATER_SEARCHES)):
        have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
        for col, kind in cols.items():
            if col not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s"
                          % (table, col, kind))
    c.commit()
    _repair(c)


# Две ошибки успели попасть в уже собранные базы: в «чем занимается»
# заезжал кусок разметки, а сайт сохранялся с чужими метками перехода.
# Разбор и то и другое чинит для новых компаний, но старые карточки от
# этого сами собой не исправятся — поэтому чиним их один раз здесь.
_JUNK = ("http-equiv", "charset=", "content-type", "<meta")


def _host_only(url):
    url = (url or "").strip()
    if "//" not in url:
        return url.split("/")[0].split("?")[0]
    scheme, rest = url.split("//", 1)
    return "%s//%s" % (scheme, rest.split("/")[0].split("?")[0])


def _clean_activity(text):
    """Отрезать разметку, прилипшую спереди, и оставить живой текст."""
    tail = text
    for mark in ('/>', '">', "'>"):
        pos = tail.rfind(mark)
        if pos >= 0:
            tail = tail[pos + len(mark):]
    tail = tail.strip(' "\'>/')
    return tail if len(tail) >= 25 else ""


def _repair(c):
    fixes = []
    for row in c.execute("SELECT id, site, activity FROM companies "
                         "WHERE (site IS NOT NULL AND site <> '') "
                         "   OR (activity IS NOT NULL AND activity <> '')"):
        site, act = row["site"] or "", row["activity"] or ""
        new_site = _host_only(site)
        low = act.lower()
        new_act = _clean_activity(act) if any(j in low for j in _JUNK) else act
        if new_site != site or new_act != act:
            fixes.append((new_site, new_act, row["id"]))
    if fixes:
        c.executemany("UPDATE companies SET site=?, activity=? WHERE id=?",
                      fixes)
        c.commit()


def now():
    return int(time.time())


# ── Компании ─────────────────────────────────────────────
def company_keys(row):
    """Чем компания опознаётся в чёрном списке.

    Ключей несколько: у одной и той же фирмы в разных источниках есть то
    ИНН, то идентификатор hh, то одно название. Совпадения любого хватает.
    """
    keys = []
    if (row.get("inn") or "").strip():
        keys.append("inn:" + row["inn"].strip())
    if (row.get("hh_id") or "").strip():
        keys.append("hh:" + str(row["hh_id"]).strip())
    name = (row.get("name") or "").strip().lower()
    if name:
        keys.append("name:" + name)
    return keys


def is_blacklisted(row):
    """Отказывалась ли уже эта компания.

    Тонкость в названии. «ООО Ромашка» есть в каждом регионе, и если
    одна такая сказала «нет», это не значит, что молчать должны все
    остальные. Поэтому совпадение по названию засчитывается только
    тогда, когда опознать компанию точнее нечем: ни ИНН, ни
    идентификатора работодателя у неё нет.

    Когда ИНН есть и он не совпал — это другая компания, и название тут
    ничего не решает.
    """
    keys = company_keys(row)
    if not keys:
        return False
    strong = [k for k in keys if not k.startswith("name:")]
    look = strong or keys
    marks = ",".join("?" for _ in look)
    got = conn().execute(
        "SELECT 1 FROM blacklist WHERE key IN (%s) LIMIT 1" % marks, look).fetchone()
    return got is not None


def blacklist_add(company_id, reason=""):
    c = conn()
    row = c.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if row is None:
        return 0
    data = {"inn": row["inn"], "hh_id": row["hh_id"], "name": row["name"]}
    for key in company_keys(data):
        c.execute("""INSERT OR IGNORE INTO blacklist (key, name, reason, created_at)
                     VALUES (?,?,?,?)""", (key, row["name"], reason, now()))
    c.commit()
    return 1


def blacklist_clear():
    c = conn()
    c.execute("DELETE FROM blacklist")
    c.commit()


def delete_company(company_id):
    """Удалить компанию вместе со всем, что к ней относится.

    Заметок здесь раньше не было, и они оставались в базе навсегда,
    привязанные к несуществующей компании: место занимают, показать их
    негде, а при совпадении нового идентификатора они всплыли бы в чужой
    карточке.
    """
    c = conn()
    for table in ("contacts", "signals", "notes"):
        c.execute("DELETE FROM %s WHERE company_id=?" % table, (company_id,))
    c.execute("DELETE FROM companies WHERE id=?", (company_id,))
    c.commit()


_OPF_HEAD = re.compile(
    r"^\s*(ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО|АНО|НАО|ГБУ|МБУ|ФГУП|МУП)\s+", re.I)


def _short_name(name):
    """Название без формы собственности и знаков — ключ для склейки.

    «ООО "Дентал"» из ЕГРЮЛ и «Дентал» с карты — одна компания, и пока
    они считались разными, у человека в списке было две строки: в одной
    ИНН без телефона, в другой телефон без ИНН.
    """
    s = _OPF_HEAD.sub("", (name or "").strip())
    s = _OPF_HEAD.sub("", s)
    s = re.sub(r"[«»\"\'`]", " ", s)
    s = re.sub(r"[^\w\s-]", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s if len(s) >= 4 else ""


def upsert_company(row):
    """Добавить компанию или дополнить существующую.

    Дедупликация по ИНН, а при его отсутствии — по идентификатору работодателя
    с hh. Названия для этого не годятся: «ООО Ромашка» есть в каждом регионе,
    и склейка по имени смешала бы разные юрлица в одно.
    """
    c = conn()
    inn = (row.get("inn") or "").strip()
    hh_id = (row.get("hh_id") or "").strip()
    found = None
    if inn:
        found = c.execute("SELECT * FROM companies WHERE inn=?", (inn,)).fetchone()
    if found is None and hh_id:
        found = c.execute("SELECT * FROM companies WHERE hh_id=?", (hh_id,)).fetchone()

    # Совпадение по домену и по названию с городом.
    #
    # Без этого база удваивалась на каждом повторном прогоне: у компании
    # из карты нет ни ИНН, ни идентификатора работодателя, и вчерашняя
    # запись о ней ничем не отличалась от сегодняшней. Дубли искались
    # потом, отдельной кнопкой, вручную — при том что ровно те же ключи
    # уже известны в момент записи.
    #
    # Разные ИНН при этом никогда не сливаются: один сайт и одно название
    # на две фирмы — обычное дело у групп компаний, а ИНН у них разный, и
    # это решающий довод.
    host = host_of(row.get("site"))
    if found is None and host:
        cand = c.execute(
            "SELECT * FROM companies WHERE site <> '' AND site IS NOT NULL "
            "AND (site LIKE ? OR site LIKE ?) LIMIT 5",
            ("%//" + host + "%", "%//www." + host + "%")).fetchall()
        for x in cand:
            if host_of(x["site"]) != host:
                continue
            if inn and (x["inn"] or "").strip() and x["inn"].strip() != inn:
                continue
            found = x
            break
    if found is None:
        name_key = _short_name(row.get("name"))
        region = (row.get("region") or "").strip()
        if name_key and region:
            for x in c.execute(
                    "SELECT * FROM companies WHERE region=?", (region,)):
                if _short_name(x["name"]) != name_key:
                    continue
                if inn and (x["inn"] or "").strip() and x["inn"].strip() != inn:
                    continue
                found = x
                break

    # Возвращаем признак новизны вместе с идентификатором: по нему видно,
    # сколько компаний поиск принёс впервые, а сколько уже лежало. Без
    # этого повторный прогон выглядит как холостой.
    fields = ("name", "inn", "ogrn", "director", "director_post", "okved",
              "okved_name", "region", "address", "employees", "founded",
              "status", "capital", "branches", "founders_count", "founders",
              "okveds_extra", "growth", "activity", "cms", "callcenter",
              "ai_summary", "ai_segment", "ai_fit", "ai_why", "ai_hook",
              "ai_opener", "site", "hh_id", "score", "source")
    if found is None:
        data = {k: row.get(k) for k in fields}
        data["created_at"] = data["updated_at"] = now()
        keys = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        cur = c.execute("INSERT INTO companies (%s) VALUES (%s)" % (keys, marks),
                        tuple(data.values()))
        c.commit()
        return cur.lastrowid, True

    # Обновляем только пустые поля: то, что уже найдено из более надёжного
    # источника, новый источник перетирать не должен.
    patch = {}
    for k in fields:
        new = row.get(k)
        if new in (None, "", 0):
            continue
        if found[k] in (None, "", 0):
            patch[k] = new
    if patch:
        patch["updated_at"] = now()
        sets = ", ".join("%s=?" % k for k in patch)
        c.execute("UPDATE companies SET %s WHERE id=?" % sets,
                  tuple(patch.values()) + (found["id"],))
        c.commit()
    return found["id"], False


def add_contact(company_id, kind, value, owner="unknown", confidence=50,
                verified="unchecked", source=""):
    value = (value or "").strip()
    if not value:
        return False
    c = conn()
    cur = c.execute("""INSERT OR IGNORE INTO contacts
                 (company_id, kind, value, owner, confidence, verified, source, created_at)
                 VALUES (?,?,?,?,?,?,?,?)""",
                    (company_id, kind, value, owner, confidence, verified,
                     source, now()))
    c.commit()
    # True — контакт действительно новый. Нужно тем, кто считает находки:
    # повторно встреченный телефон находкой не является.
    return cur.rowcount > 0


def add_signal(company_id, key, value=""):
    c = conn()
    c.execute("""INSERT INTO signals (company_id, key, value, created_at)
                 VALUES (?,?,?,?)
                 ON CONFLICT(company_id, key) DO UPDATE SET value=excluded.value""",
              (company_id, key, str(value), now()))
    c.commit()


def get_signal(company_id, key, default=""):
    row = conn().execute("SELECT value FROM signals WHERE company_id=? AND key=?",
                         (company_id, key)).fetchone()
    return row["value"] if row else default


def set_score(company_id, score):
    c = conn()
    c.execute("UPDATE companies SET score=?, updated_at=? WHERE id=?",
              (int(score), now(), company_id))
    c.commit()


# ── Задачи ───────────────────────────────────────────────
# ── Память об обойдённых сайтах ──────────────────────────
def host_of(url):
    return (url or "").split("//")[-1].split("/")[0].replace("www.", "").lower()


def visited_recently(url, days=7):
    """Обходили ли этот сайт недавно и успешно."""
    host = host_of(url)
    if not host:
        return False
    row = conn().execute("SELECT visited_at, ok FROM site_visits WHERE host=?",
                         (host,)).fetchone()
    if row is None or not row["ok"]:
        return False
    return (now() - (row["visited_at"] or 0)) < days * 86400


def mark_visited(url, pages=0, ok=True):
    host = host_of(url)
    if not host:
        return
    c = conn()
    c.execute("INSERT INTO site_visits (host, visited_at, pages, ok) "
              "VALUES (?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
              "visited_at=excluded.visited_at, pages=excluded.pages, ok=excluded.ok",
              (host, now(), int(pages or 0), 1 if ok else 0))
    c.commit()


# ── Заметки ──────────────────────────────────────────────
def add_note(company_id, text):
    text = (text or "").strip()[:2000]
    if not text:
        return 0
    c = conn()
    cur = c.execute("INSERT INTO notes (company_id, text, created_at) "
                    "VALUES (?,?,?)", (company_id, text, now()))
    c.commit()
    return cur.lastrowid


def notes(company_id, limit=20):
    return [dict(r) for r in conn().execute(
        "SELECT * FROM notes WHERE company_id=? ORDER BY id DESC LIMIT ?",
        (company_id, limit))]


def delete_note(note_id, company_id=None):
    c = conn()
    if company_id:
        c.execute("DELETE FROM notes WHERE id=? AND company_id=?",
                  (note_id, company_id))
    else:
        c.execute("DELETE FROM notes WHERE id=?", (note_id,))
    c.commit()


# ── Сохранённые поиски ───────────────────────────────────
def save_search(name, params, kind="hh_search", every_days=0):
    """Сохранить набор условий под именем. Повторное имя — перезапись.

    every_days — раз во сколько дней повторять сам. Ноль означает «не
    повторять»: сохранённый набор условий полезен и без расписания,
    просто чтобы не набирать то же самое заново.
    """
    name = (name or "").strip()[:80]
    if not name:
        return 0
    c = conn()
    blob = json.dumps(params or {}, ensure_ascii=False)
    every = max(0, min(365, int(every_days or 0)))
    # Первый прогон по расписанию — через положенный срок, а не сейчас:
    # человек только что искал это руками.
    nxt = (now() + every * 86400) if every else None
    row = c.execute("SELECT id FROM searches WHERE name=? AND kind=?",
                    (name, kind)).fetchone()
    if row:
        c.execute("UPDATE searches SET params=?, every_days=?, next_run=?, "
                  "enabled=1 WHERE id=?", (blob, every, nxt, row["id"]))
        c.commit()
        return row["id"]
    cur = c.execute("""INSERT INTO searches (name, kind, params, created_at,
                                             every_days, next_run, enabled)
                       VALUES (?,?,?,?,?,?,1)""",
                    (name, kind, blob, now(), every, nxt))
    c.commit()
    return cur.lastrowid


def set_search_plan(search_id, every_days=None, enabled=None):
    """Поменять расписание, не трогая сами условия."""
    c = conn()
    row = c.execute("SELECT * FROM searches WHERE id=?",
                    (search_id,)).fetchone()
    if row is None:
        return None
    every = (row["every_days"] or 0) if every_days is None \
        else max(0, min(365, int(every_days)))
    on = (1 if (row["enabled"] is None or row["enabled"]) else 0) \
        if enabled is None else (1 if enabled else 0)
    nxt = (now() + every * 86400) if (every and on) else None
    c.execute("UPDATE searches SET every_days=?, enabled=?, next_run=? "
              "WHERE id=?", (every, on, nxt, search_id))
    c.commit()
    return get_search(search_id)


def due_searches(at=None):
    """Наборы, которым пора выполниться.

    Программу выключают на неделю — это норма, и пропущенный срок не
    должен превращаться в очередь из семи прогонов. Поэтому просроченный
    набор выполняется один раз, а следующий срок считается от сейчас.
    """
    at = int(at if at is not None else now())
    rows = conn().execute(
        "SELECT * FROM searches WHERE COALESCE(enabled,1)=1 "
        "AND COALESCE(every_days,0) > 0 AND COALESCE(next_run,0) <= ? "
        "ORDER BY id", (at,)).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r["params"] or "{}")
        except Exception:
            params = {}
        out.append(dict(r, params=params))
    return out


def list_searches():
    rows = conn().execute(
        # COALESCE, а не NULLS LAST: последнее появилось в SQLite 3.30,
        # и на чужой машине с более старой библиотекой запрос упал бы.
        "SELECT * FROM searches ORDER BY COALESCE(last_run,0) DESC, id DESC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r["params"] or "{}")
        except Exception:
            params = {}
        out.append(dict(r, params=params))
    return out


def get_search(search_id):
    row = conn().execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
    if row is None:
        return None
    try:
        params = json.loads(row["params"] or "{}")
    except Exception:
        params = {}
    return dict(row, params=params)


def mark_search_run(search_id):
    """Отметить прогон и отодвинуть следующий срок.

    Срок считается от сейчас, а не от прошлого срока: иначе набор,
    просроченный за время, пока программа была закрыта, выполнялся бы
    подряд столько раз, сколько сроков прошло.
    """
    c = conn()
    row = c.execute("SELECT every_days FROM searches WHERE id=?",
                    (search_id,)).fetchone()
    every = (row["every_days"] or 0) if row else 0
    nxt = (now() + int(every) * 86400) if every else None
    c.execute("UPDATE searches SET runs=runs+1, last_run=?, next_run=? "
              "WHERE id=?", (now(), nxt, search_id))
    c.commit()


def delete_search(search_id):
    c = conn()
    c.execute("DELETE FROM searches WHERE id=?", (search_id,))
    c.commit()


# ── Разовая чистка старых записей ────────────────────────
def clean_junk_phones():
    """Выкинуть заглушки из вёрстки, попавшие в базу до проверки.

    Отсев появился позже, чем первые прогоны, и в базе остались
    +7 101 000-00-00 и +7 999 999-99-99. Они не становятся телефонами
    оттого, что лежат давно.
    """
    from .sources import site as site_src
    c = conn()
    bad = [r["id"] for r in c.execute(
        "SELECT id, value FROM contacts WHERE kind='phone'")
        if not site_src._clean_phone(r["value"])]
    if bad:
        c.executemany("DELETE FROM contacts WHERE id=?", [(i,) for i in bad])
        c.commit()
    return len(bad)


def clean_orphans():
    """Записи, оставшиеся от удалённых компаний.

    До сих пор удаление компании не трогало её заметки, и в базе у тех,
    кто чистил список, лежат чужие хвосты.
    """
    c = conn()
    n = 0
    for table in ("contacts", "signals", "notes"):
        cur = c.execute(
            "DELETE FROM %s WHERE company_id NOT IN (SELECT id FROM companies)"
            % table)
        n += cur.rowcount or 0
    c.commit()
    return n


# ── Склейка дублей ───────────────────────────────────────
def merge_companies(keep_id, drop_id):
    """Перенести всё с одной компании на другую и удалить вторую."""
    if keep_id == drop_id:
        return False
    c = conn()
    keep = c.execute("SELECT * FROM companies WHERE id=?", (keep_id,)).fetchone()
    drop = c.execute("SELECT * FROM companies WHERE id=?", (drop_id,)).fetchone()
    if keep is None or drop is None:
        return False
    # Пустые поля уцелевшей заполняем из удаляемой: у одной записи есть
    # ИНН, у другой сайт — вместе они и составляют компанию.
    patch = {}
    for field in ("inn", "ogrn", "site", "director", "director_post",
                  "address", "region", "okved", "okved_name", "activity",
                  "employees", "founded", "status", "hh_id"):
        if field in keep.keys() and not (keep[field] or "") and (drop[field] or ""):
            patch[field] = drop[field]
    for table in ("contacts", "signals", "notes"):
        try:
            c.execute("UPDATE OR IGNORE %s SET company_id=? WHERE company_id=?"
                      % table, (keep_id, drop_id))
            c.execute("DELETE FROM %s WHERE company_id=?" % table, (drop_id,))
        except Exception:
            pass
    # Удаляем раньше, чем переносим поля: ИНН и hh_id уникальны в
    # пределах таблицы, и пока вторая запись жива, тот же ИНН на первую
    # не встанет.
    c.execute("DELETE FROM companies WHERE id=?", (drop_id,))
    if patch:
        sets = ", ".join("%s=?" % k for k in patch)
        c.execute("UPDATE companies SET %s WHERE id=?" % sets,
                  tuple(patch.values()) + (keep_id,))
    c.commit()
    return True


def find_duplicates():
    """Пары компаний, которые похожи на одну и ту же.

    Сравниваем по ИНН, по домену сайта и по названию без формы
    собственности. Внутри одного прогона такие записи склеиваются сразу,
    а между прогонами — нет: сегодня компания пришла из карты без ИНН,
    завтра из ЕГРЮЛ с ИНН, и это две строки.
    """
    import re as _re
    rows = conn().execute(
        "SELECT id, name, inn, site, score, region FROM companies "
        "ORDER BY id").fetchall()
    by_id = {r["id"]: r for r in rows}
    seen, pairs = {}, []
    for r in rows:
        keys = []
        if (r["inn"] or "").strip():
            keys.append("инн:" + r["inn"].strip())
        host = host_of(r["site"])
        if host:
            keys.append("сайт:" + host)
        name = _re.sub(r"[^\w\s-]", " ", (r["name"] or ""), flags=_re.U)
        name = _re.sub(
            r"^\s*(ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО|АНО|НАО)\s+", "", name, flags=_re.I)
        name = _re.sub(r"\s+", " ", name).strip().lower()
        if len(name) >= 4:
            # Название — только вместе с городом. «Дентал» в Москве и
            # «Дентал» в Петербурге — разные компании, а программа
            # предлагала их склеить, и человек соглашался: кнопка
            # называется «Склеить», а не «Проверьте, точно ли это одно».
            keys.append("имя:%s|%s" % (name, (r["region"] or "").lower()))
        hit = next((seen[k] for k in keys if k in seen), None)
        if hit is not None and hit != r["id"]:
            # Разные ИНН — разные юрлица, и никакое совпадение названия
            # или домена этого не отменяет. Один сайт на две фирмы —
            # обычное дело у групп компаний.
            other = by_id.get(hit)
            a = (r["inn"] or "").strip()
            b = ((other["inn"] or "").strip() if other is not None else "")
            if a and b and a != b:
                hit = None
            else:
                pairs.append((hit, r["id"]))
        for k in keys:
            seen.setdefault(k, hit if hit is not None else r["id"])
    return pairs


def create_task(kind, params=None, total=0):
    c = conn()
    cur = c.execute("""INSERT INTO tasks (kind, params, status, total, created_at, updated_at)
                       VALUES (?,?,'queued',?,?,?)""",
                    (kind, json.dumps(params or {}, ensure_ascii=False),
                     total, now(), now()))
    c.commit()
    return cur.lastrowid


def queued_tasks(limit=20):
    """Что стоит в очереди, по порядку исполнения."""
    rows = conn().execute(
        "SELECT id, kind, params, created_at FROM tasks "
        "WHERE status='queued' ORDER BY id LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        try:
            params = json.loads(r["params"] or "{}")
        except Exception:
            params = {}
        out.append(dict(r, params=params))
    return out


def recent_tasks(limit=12):
    """Последние задачи — что шло, чем кончилось и сколько заняло.

    Нужно затем же, зачем журнал: задача, упавшая час назад, сейчас
    невидима совсем. Человек помнит, что «что-то запускал», а что и чем
    оно кончилось — нет.
    """
    rows = conn().execute(
        "SELECT id, kind, status, done, total, message, created_at, updated_at "
        "FROM tasks ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def cancel_task(task_id):
    """Убрать из очереди то, что ещё не началось.

    Только «в очереди»: остановкой идущей задачи занимается поток
    обхода, и трогать её строку отсюда — значит разойтись с ним во
    мнении о том, что происходит.
    """
    c = conn()
    cur = c.execute("UPDATE tasks SET status='stopped', "
                    "message='отменено до запуска', updated_at=? "
                    "WHERE id=? AND status='queued'", (now(), task_id))
    c.commit()
    return cur.rowcount > 0


def cancel_queued():
    """Очистить очередь целиком. Идущая задача не трогается."""
    c = conn()
    cur = c.execute("UPDATE tasks SET status='stopped', "
                    "message='отменено до запуска', updated_at=? "
                    "WHERE status='queued'", (now(),))
    c.commit()
    return cur.rowcount


def update_task(task_id, **patch):
    if not patch:
        return
    patch["updated_at"] = now()
    sets = ", ".join("%s=?" % k for k in patch)
    c = conn()
    c.execute("UPDATE tasks SET %s WHERE id=?" % sets,
              tuple(patch.values()) + (task_id,))
    c.commit()


def log(task_id, text, level="info"):
    c = conn()
    c.execute("INSERT INTO logs (task_id, level, text, created_at) VALUES (?,?,?,?)",
              (task_id, level, text[:2000], now()))
    c.commit()


def get_setting(key, default=""):
    c = conn()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    c = conn()
    c.execute("""INSERT INTO settings (key, value) VALUES (?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
              (key, str(value)))
    c.commit()


# Поля, которые приходят из реестров. Названия среди них нет намеренно:
# см. fill_company.
REGISTRY_FIELDS = (
    "inn", "ogrn", "director", "director_post", "okved", "okved_name",
    "address", "region", "status", "employees", "capital", "branches",
    "okveds_extra", "founders_count", "founders", "founded",
)


def fill_company(company_id, patch, over=False):
    """Дописать в известную строку то, чего в ней ещё нет.

    Почему не upsert. Компания уже выбрана — обогащение идёт именно по
    ней, — и искать её заново по содержимому ответа нельзя. Из ЕГРЮЛ
    приходит юридическое название: «ПАО ДВМП» вместо вывески «Fesco»,
    «ООО Стоматология плюс» вместо «Дента-Люкс». Ни ИНН, ни домена, ни
    номера работодателя в этом ответе может не быть, и тогда upsert не
    узнавал исходную строку и заводил вторую. Исходная оставалась
    пустой навсегда: обогащение шло по ней, а данные ложились в дубль,
    и в списке появлялась вторая компания с тем же телефоном.

    Название не трогаем совсем. Человек искал «грузоперевозки» и нашёл
    «Fesco» — под этим именем он компанию и помнит. Юридическое имя
    важно в договоре, а в списке на обзвон мешает узнаванию; ИНН и ОГРН
    рядом в карточке говорят о юрлице всё, что нужно.

    over=True перезаписывает и заполненное: так нужно, когда ответ
    пришёл по ИНН, то есть надёжнее того, что стояло раньше.
    """
    c = conn()
    row = c.execute("SELECT * FROM companies WHERE id=?",
                    (company_id,)).fetchone()
    if row is None:
        return {}
    ready = {}
    for key in REGISTRY_FIELDS:
        val = (patch or {}).get(key)
        if val in (None, "", 0):
            continue
        if not over and (row[key] not in (None, "", 0)):
            continue
        ready[key] = val
    if not ready:
        return {}
    ready["updated_at"] = now()
    sets = ", ".join("%s=?" % k for k in ready)
    c.execute("UPDATE companies SET %s WHERE id=?" % sets,
              tuple(ready.values()) + (company_id,))
    c.commit()
    ready.pop("updated_at", None)
    return ready


def update_company_fields(company_id, patch):
    """Правки из интерфейса: стадия работы и заметка."""
    allowed = {k: v for k, v in (patch or {}).items()
               if k in ("stage", "note", "activity", "cms", "callcenter",
                        "employees", "founded", "status", "capital",
                        "branches", "founders_count", "founders",
                        "okveds_extra", "growth", "ai_summary", "ai_segment",
                        "ai_fit", "ai_why", "ai_hook", "ai_opener", "ai_kp",
                        "next_step", "next_date")}
    if not allowed:
        return
    allowed["updated_at"] = now()
    sets = ", ".join("%s=?" % k for k in allowed)
    c = conn()
    c.execute("UPDATE companies SET %s WHERE id=?" % sets,
              tuple(allowed.values()) + (company_id,))
    c.commit()
