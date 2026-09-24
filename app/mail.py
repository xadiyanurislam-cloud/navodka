# -*- coding: utf-8 -*-
"""Почта для рассылок: отправка по SMTP и чтение ответов по IMAP.

Только стандартная библиотека. Почтовые протоколы в ней есть целиком, а
каждая лишняя зависимость — это ещё один пакет, который надо протащить
через сборку в exe и который однажды не соберётся.

Прокси программы сюда не применяется: SOCKS для SMTP и IMAP в
стандартной библиотеке нет, а HTTP-прокси почту не пропускает вовсе.
Об этом сказано и в интерфейсе, рядом с полями ящика.

Пароль ящика не попадает ни в журнал, ни в текст ошибки: всё, что
уходит наружу из этого модуля, проходит через _scrub.
"""
import email
import imaplib
import re
import smtplib
import socket
import ssl
import time
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from . import db

SMTP_TIMEOUT = 30
IMAP_TIMEOUT = 30

# Серверы крупных почтовых служб. Человек знает свой адрес, но не обязан
# знать, что у Яндекса SMTP на 465-м порту: подставляем сами, а поля
# «вручную» остаются для своего домена.
_YANDEX = ("smtp.yandex.ru", 465, "imap.yandex.ru", 993, "yandex")
_MAILRU = ("smtp.mail.ru", 465, "imap.mail.ru", 993, "mailru")
_GMAIL = ("smtp.gmail.com", 465, "imap.gmail.com", 993, "gmail")
_RAMBLER = ("smtp.rambler.ru", 465, "imap.rambler.ru", 993, "rambler")
PRESETS = {}
for _names, _p in (
        (("yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz",
          "narod.ru"), _YANDEX),
        (("mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru"), _MAILRU),
        (("gmail.com", "googlemail.com"), _GMAIL),
        (("rambler.ru", "lenta.ru", "ro.ru", "autorambler.ru",
          "myrambler.ru"), _RAMBLER)):
    for _n in _names:
        PRESETS[_n] = _p

# Где у каждой службы заводится пароль для программ. Обычный пароль от
# входа почти везде не подходит, и без этой подсказки человек часами
# перепроверяет правильный пароль.
APP_PASSWORD_HINT = {
    "yandex": "нужен пароль приложения, а не от входа: id.yandex.ru → "
              "«Безопасность» → «Пароли приложений» → «Почта». И в "
              "настройках Почты → «Почтовые программы» включите доступ "
              "по IMAP",
    "mailru": "нужен пароль для внешнего приложения: настройки почты → "
              "«Безопасность» → «Пароли для внешних приложений»",
    "gmail": "нужен пароль приложения (он появляется после включения "
             "двухэтапной проверки): myaccount.google.com/apppasswords",
    "rambler": "в настройках почты Рамблера включите «Доступ к почтовому "
               "ящику с помощью почтовых клиентов» и войдите паролем "
               "приложения",
    "": "проверьте адрес и пароль. Большинство почтовых служб пускают "
        "программы только по отдельному паролю приложения",
}

OPT_OUT_LINE = ("Если это неактуально — ответьте «нет», и я больше не "
                "напишу.")


def domain_of(address):
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[1] if "@" in address else ""


def preset(address):
    """Серверы по домену адреса или пустой словарь, если служба не известна."""
    p = PRESETS.get(domain_of(address))
    if not p:
        return {}
    return {"smtp_host": p[0], "smtp_port": p[1], "imap_host": p[2],
            "imap_port": p[3], "service": p[4]}


def presets_for_ui():
    """Короткая таблица для подсказок в полях: домен → SMTP и IMAP."""
    return {d: {"smtp": "%s:%d" % (p[0], p[1]), "imap": "%s:%d" % (p[2], p[3])}
            for d, p in PRESETS.items()}


def _port(value, default):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if 0 < n < 65536 else default


def conf(address="", password="", name="", sign="", smtp_host="",
         smtp_port="", imap_host="", imap_port=""):
    address = (address or "").strip()
    known = preset(address)
    c = {
        "address": address,
        "password": password or "",
        "name": (name or "").strip(),
        "sign": (sign or "").strip(),
        "smtp_host": (smtp_host or "").strip() or known.get("smtp_host", ""),
        "imap_host": (imap_host or "").strip() or known.get("imap_host", ""),
        "service": known.get("service", ""),
    }
    c["smtp_port"] = _port(smtp_port, known.get("smtp_port", 465))
    c["imap_port"] = _port(imap_port, known.get("imap_port", 993))
    # Свой домен на Яндексе или Mail.ru распознаётся по серверу, а не по
    # адресу — подсказку про пароль приложения даём по нему.
    if not c["service"]:
        host = c["smtp_host"].lower()
        for mark, svc in (("yandex", "yandex"), ("mail.ru", "mailru"),
                          ("gmail", "gmail"), ("google", "gmail"),
                          ("rambler", "rambler")):
            if mark in host:
                c["service"] = svc
                break
    return c


def conf_from_db():
    g = db.get_setting
    return conf(g("mail_address", ""), g("mail_password", ""),
                g("mail_name", ""), g("mail_sign", ""),
                g("mail_smtp_host", ""), g("mail_smtp_port", ""),
                g("mail_imap_host", ""), g("mail_imap_port", ""))


ADDR_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")


def problem(c):
    """Чего не хватает, чтобы отправлять. Пустая строка — всё есть."""
    if not c.get("address"):
        return "не указан адрес ящика"
    if not ADDR_RE.match(c["address"]):
        return "адрес ящика не похож на почту"
    if not c.get("password"):
        return "не указан пароль ящика"
    if not c.get("smtp_host"):
        return ("сервер отправки для домена %s не известен — впишите его в "
                "«Сервер вручную»" % domain_of(c["address"]))
    return ""


def can_read(c):
    return bool(c.get("imap_host")) and not problem(c)


def _scrub(text, c):
    text = str(text or "")
    pwd = (c or {}).get("password") or ""
    if pwd and len(pwd) >= 3:
        text = text.replace(pwd, "•••")
    return text


def _decode(x):
    if isinstance(x, bytes):
        return x.decode("utf-8", "replace")
    return str(x)


# ── Ошибки ───────────────────────────────────────────────
# Итог отправки делится на два рода. «Адрес» — плох конкретный
# получатель: пометить его и идти дальше. «Ящик» — плохо у нас (пароль,
# сеть, сервер счёл нас спамером): продолжать бессмысленно, следующее
# письмо упадёт так же, а каждая попытка ещё и портит репутацию ящика.
RECIPIENT = "recipient"
GLOBAL = "global"


def explain(e, c=None, where="smtp"):
    """Ошибка по-человечески и её род. Возвращает (текст, род)."""
    c = c or {}
    hint = APP_PASSWORD_HINT.get(c.get("service", ""), APP_PASSWORD_HINT[""])
    host = c.get("smtp_host" if where == "smtp" else "imap_host", "")
    port = c.get("smtp_port" if where == "smtp" else "imap_port", "")
    raw = _scrub(_decode(getattr(e, "smtp_error", b"") or "") or str(e), c)
    low = raw.lower()

    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "почта не приняла пароль — %s" % hint, GLOBAL
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        bad = ", ".join(sorted(getattr(e, "recipients", {}) or {}))
        return "сервер не принял адрес получателя %s" % bad, RECIPIENT
    if isinstance(e, smtplib.SMTPSenderRefused):
        return ("сервер не принял отправителя: адрес в поле «Почта» должен "
                "совпадать с ящиком, в который входим"), GLOBAL
    if isinstance(e, smtplib.SMTPDataError):
        code = getattr(e, "smtp_code", 0) or 0
        if "spam" in low or "спам" in low:
            return ("почтовый сервер посчитал письмо спамом и не отправил "
                    "его. Отправка приостановлена: уменьшите лимит, уберите "
                    "ссылки из текста и проверьте SPF/DKIM домена "
                    "(%s)" % raw[:160]), GLOBAL
        if 500 <= code < 600 and ("recipient" in low or "user" in low
                                  or "mailbox" in low):
            return "адрес получателя не существует (%s)" % raw[:160], RECIPIENT
        return "сервер отказался принять письмо: %s" % raw[:200], GLOBAL
    if isinstance(e, imaplib.IMAP4.error):
        if "disabled" in low or "not enabled" in low or "imap" in low and "off" in low:
            return ("в настройках ящика выключен доступ по IMAP — без него "
                    "программа не видит ответов. %s" % hint), GLOBAL
        if ("auth" in low or "login" in low or "credentials" in low
                or "password" in low or "invalid" in low):
            return "почта не пустила по IMAP — %s" % hint, GLOBAL
        return "IMAP ответил ошибкой: %s" % raw[:200], GLOBAL
    if isinstance(e, ssl.SSLError):
        return ("не сложилось защищённое соединение с %s:%s. Порт 465 — это "
                "SSL, 587 — STARTTLS; для IMAP обычно 993" % (host, port)), GLOBAL
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "сервер %s:%s не ответил за %d с" % (
            host, port, SMTP_TIMEOUT), GLOBAL
    if isinstance(e, socket.gaierror):
        return "не нашёл сервер %s — проверьте его имя" % host, GLOBAL
    if isinstance(e, (ConnectionError, OSError)):
        return ("не достучался до %s:%s (%s). Некоторые провайдеры закрывают "
                "почтовые порты — попробуйте 587" % (host, port, raw[:120])), GLOBAL
    if isinstance(e, smtplib.SMTPException):
        return "почтовый сервер ответил ошибкой: %s" % raw[:200], GLOBAL
    return "%s: %s" % (type(e).__name__, raw[:200]), GLOBAL


# ── Отправка ─────────────────────────────────────────────
def build(c, to, subject, body, in_reply_to="", references=()):
    """Письмо целиком. Message-ID ставим сами — по нему узнаем ответ."""
    msg = EmailMessage()
    msg["From"] = formataddr((c.get("name") or "", c["address"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain_of(c["address"]) or None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    refs = [r for r in (references or ()) if r]
    if refs:
        msg["References"] = " ".join(refs)
    # Отписка одним нажатием в самой почте. Крупные службы смотрят на
    # этот заголовок, когда решают, во «Входящие» письмо или в спам.
    msg["List-Unsubscribe"] = "<mailto:%s?subject=unsubscribe>" % c["address"]
    msg.set_content(body)
    return msg


def _smtp(c):
    if c["smtp_port"] == 465:
        s = smtplib.SMTP_SSL(c["smtp_host"], c["smtp_port"],
                             timeout=SMTP_TIMEOUT,
                             context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(c["smtp_host"], c["smtp_port"], timeout=SMTP_TIMEOUT)
        s.ehlo()
        if s.has_extn("starttls"):
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
    try:
        s.login(c["address"], c["password"])
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise
    return s


def send(c, msg):
    """Отправить письмо. Возвращает (ошибка, род ошибки); ("", "") — ушло."""
    why = problem(c)
    if why:
        return why, GLOBAL
    try:
        s = _smtp(c)
        try:
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except Exception as e:
        return explain(e, c, "smtp")
    return "", ""


# ── Чтение ответов ───────────────────────────────────────
def _imap(c):
    if c["imap_port"] == 993:
        m = imaplib.IMAP4_SSL(c["imap_host"], c["imap_port"],
                              ssl_context=ssl.create_default_context(),
                              timeout=IMAP_TIMEOUT)
    else:
        m = imaplib.IMAP4(c["imap_host"], c["imap_port"], timeout=IMAP_TIMEOUT)
        if "STARTTLS" in m.capabilities:
            m.starttls(ssl_context=ssl.create_default_context())
    try:
        m.login(c["address"], c["password"])
    except Exception:
        try:
            m.shutdown()
        except Exception:
            pass
        raise
    return m


def check(c, send_test=False):
    """Проверить ящик: вход по SMTP, по IMAP и, по желанию, письмо себе.

    Возвращает список шагов (что, получилось ли, пояснение).
    """
    steps = []
    why = problem(c)
    if why:
        return [("Настройки", False, why)]
    try:
        s = _smtp(c)
        try:
            s.quit()
        except Exception:
            pass
        steps.append(("Отправка (SMTP)", True, "%s:%d — вход выполнен"
                      % (c["smtp_host"], c["smtp_port"])))
    except Exception as e:
        steps.append(("Отправка (SMTP)", False, explain(e, c, "smtp")[0]))
    if c.get("imap_host"):
        try:
            m = _imap(c)
            try:
                m.logout()
            except Exception:
                pass
            steps.append(("Ответы (IMAP)", True, "%s:%d — вход выполнен"
                          % (c["imap_host"], c["imap_port"])))
        except Exception as e:
            steps.append(("Ответы (IMAP)", False, explain(e, c, "imap")[0]))
    else:
        steps.append(("Ответы (IMAP)", False,
                      "сервер IMAP не указан — ответы придётся отмечать руками"))
    if send_test and steps[0][1]:
        msg = build(c, c["address"], "Наводка: проверка почты",
                    "Это проверочное письмо. Если оно пришло во «Входящие», "
                    "а не в спам, — ящик готов к рассылке.\n\n"
                    + (c.get("sign") or ""))
        err, _ = send(c, msg)
        steps.append(("Письмо себе", not err,
                      err or "отправлено на %s — проверьте, что пришло во "
                             "«Входящие», а не в спам" % c["address"]))
    return steps


def imap_since(ts):
    """Дата для IMAP SEARCH SINCE: 01-Jan-2026. Месяц — только по-английски."""
    t = time.localtime(ts)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec")
    return "%02d-%s-%d" % (t.tm_mday, months[t.tm_mon - 1], t.tm_year)


def fetch_new(c, last_uid=0, since_ts=None, limit=200):
    """Новые письма во «Входящих» после last_uid.

    Возвращает (письма, uidvalidity, наибольший uid, ошибка). Письма
    читаются с PEEK: отметка «прочитано» остаётся за человеком, иначе
    ответ клиента выглядел бы в почте уже просмотренным.
    """
    if not can_read(c):
        return [], "", last_uid, "IMAP не настроен"
    try:
        m = _imap(c)
    except Exception as e:
        return [], "", last_uid, explain(e, c, "imap")[0]
    out, top, validity = [], last_uid, ""
    try:
        typ, _ = m.select("INBOX", readonly=True)
        if typ != "OK":
            return [], "", last_uid, "не открылась папка «Входящие»"
        v = m.response("UIDVALIDITY")[1]
        validity = _decode(v[0]) if v and v[0] else ""
        if last_uid:
            typ, data = m.uid("search", None, "UID", "%d:*" % (last_uid + 1))
        else:
            typ, data = m.uid("search", None, "SINCE",
                              imap_since(since_ts or time.time() - 86400 * 14))
        uids = []
        if typ == "OK" and data and data[0]:
            uids = sorted(int(x) for x in data[0].split() if x.isdigit())
        # «n:*» возвращает последнее письмо, даже когда новых нет.
        uids = [u for u in uids if u > last_uid][-limit:]
        for uid in uids:
            typ, parts = m.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK":
                continue
            raw = next((p[1] for p in parts
                        if isinstance(p, tuple) and len(p) > 1), None)
            top = max(top, uid)
            if not raw:
                continue
            # Одно кривое письмо (битая кодировка заголовка) не должно
            # останавливать разбор остальных — иначе на нём застрянет
            # весь опрос, и ответы перестанут находиться насовсем.
            try:
                item = parse(raw)
            except Exception:
                continue
            item["uid"] = uid
            out.append(item)
    except Exception as e:
        return out, validity, top, explain(e, c, "imap")[0]
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out, validity, top, ""


MSGID_RE = re.compile(r"<[^<>\s@]+@[^<>\s]+>")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DAEMONS = ("mailer-daemon", "postmaster", "mail-daemon", "mailerdaemon")
BOUNCE_SUBJ = re.compile(
    r"undeliver|delivery status|delivery failure|returned mail|"
    r"failure notice|не доставлен|недоставлен|ошибка доставки", re.I)
AUTO_SUBJ = re.compile(
    r"^(auto|автоответ|автоматический ответ|out of office|отсутств|"
    r"нахожусь в отпуске|я в отпуске)", re.I)
QUOTE_HEAD = re.compile(
    r"^\s*(-{2,}\s*(original|исходное|пересылаемое)|"
    r".{0,120}(wrote|пишет|написал\(а\)|написала|написал):\s*$|"
    r"(от|from):\s.*@)", re.I)


def _text_of(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = (text.replace("&nbsp;", " ").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&amp;", "&"))
    return text


def fresh_part(text):
    """Только то, что написал человек, — без цитаты нашего письма."""
    keep = []
    for line in (text or "").splitlines():
        if line.lstrip().startswith(">") or QUOTE_HEAD.match(line):
            break
        keep.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def parse(raw):
    """Разобрать входящее письмо в то, что нужно рассылке."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    subject = str(msg.get("Subject", "") or "")
    refs = MSGID_RE.findall(" ".join(str(msg.get(h, "") or "")
                                     for h in ("In-Reply-To", "References")))
    local = addr.split("@")[0].lower() if "@" in addr else ""
    ctype = msg.get_content_type()
    report = (ctype == "multipart/report"
              and "delivery-status" in str(msg.get("Content-Type", "")).lower())
    bounce = report or local in DAEMONS or bool(
        BOUNCE_SUBJ.search(subject) and local in DAEMONS + ("noreply", "no-reply"))
    auto = bool(
        str(msg.get("Auto-Submitted", "no")).lower() not in ("", "no")
        or msg.get("X-Autoreply") or msg.get("X-Autorespond")
        or AUTO_SUBJ.search(subject.strip()))
    text = _text_of(msg)
    item = {"from": addr.lower(), "from_name": name, "subject": subject[:300],
            "refs": refs, "bounce": bounce, "auto": auto and not bounce,
            "text": fresh_part(text)[:1200], "failed": [], "bounce_refs": []}
    if bounce:
        # Кому не дошло и какое наше письмо вернулось. Отчёт о доставке
        # несёт исходные заголовки внутри — по ним и узнаём своё.
        whole = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        failed = re.findall(r"(?im)^\s*(?:final|original)-recipient:\s*"
                            r"rfc822;\s*<?([^\s>]+)", whole)
        item["failed"] = sorted({a.lower() for a in failed})
        item["bounce_refs"] = MSGID_RE.findall(whole)
        item["text"] = text.strip()[:600]
    return item


REFUSAL = re.compile(
    r"отпиш|не пишите|больше не пиш|удалите (нас|меня|наш|мой)|"
    r"не интересн|неинтересн|не актуальн|неактуальн|не нуждаемся|"
    r"не требуется|unsubscribe|remove me|not interested", re.I)


def is_refusal(text, subject=""):
    """Человек попросил больше не писать.

    Короткое «нет» тоже отказ — ровно так мы и предложили ответить в
    каждом письме. Длинный ответ, где «нет» стоит посреди фразы, отказом
    не считается: «нет, давайте в четверг» — это согласие.
    """
    if (subject or "").strip().lower() == "unsubscribe":
        return True
    body = (text or "").strip().lower()
    first = " ".join(re.sub(r"[^\w\s]", " ", body.split("\n", 1)[0]).split())
    if first in ("нет", "нет спасибо", "спасибо нет", "не надо", "не нужно",
                 "no", "no thanks"):
        return True
    return bool(REFUSAL.search(body[:400]))
