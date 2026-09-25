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
import contextlib
import os
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

-- Рассылки. Кампания — это шаги (первое письмо и напоминания), а
-- outreach — одна компания внутри кампании: какой шаг следующий и когда.
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    steps       TEXT,
    status      TEXT DEFAULT 'active',   -- active | paused
    created_at  INTEGER,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS outreach (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    company_id  INTEGER NOT NULL,
    contact_id  INTEGER,
    email       TEXT NOT NULL,
    step        INTEGER DEFAULT 0,       -- номер следующего шага
    status      TEXT DEFAULT 'queued',   -- queued | waiting | replied | bounced | unsub | stopped | error
    next_at     INTEGER,
    sent_at     INTEGER,
    tries       INTEGER DEFAULT 0,
    error       TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_uniq ON outreach(campaign_id, company_id);
CREATE INDEX IF NOT EXISTS idx_outreach_due ON outreach(status, next_at);

-- Каждое отправленное письмо. Отдельно от outreach, потому что ответ
-- приходит на конкретное письмо, а их у компании до трёх, и узнаём мы
-- его по Message-ID.
CREATE TABLE IF NOT EXISTS sent_mail (
    id          INTEGER PRIMARY KEY,
    outreach_id INTEGER,
    campaign_id INTEGER,
    company_id  INTEGER NOT NULL,
    step        INTEGER,
    email       TEXT,
    subject     TEXT,
    body        TEXT,
    message_id  TEXT,
    sent_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sent_msgid ON sent_mail(message_id);
CREATE INDEX IF NOT EXISTS idx_sent_email ON sent_mail(email);
CREATE INDEX IF NOT EXISTS idx_sent_time  ON sent_mail(sent_at);

-- Адреса, попросившие больше не писать. По адресу, а не по компании:
-- компанию могут удалить и найти заново, а обещание остаётся.
CREATE TABLE IF NOT EXISTS mail_optout (
    email       TEXT PRIMARY KEY,
    reason      TEXT,
    created_at  INTEGER
);
"""


# ── Проекты ──────────────────────────────────────────────
# У каждого своего бизнеса — свой проект со своей базой: компании,
# воронка, заметки, чёрный список, рассылки. Одна и та же фирма бывает
# лидом для двух проектов, и стадия у неё в каждом своя.
#
# Главная база — файл, с которого программа начиналась. В ней лежат
# список проектов и общие ключи, и она же — база первого проекта: так
# у тех, кто обновился, ничего никуда не переезжает.
PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    file        TEXT DEFAULT '',     -- пусто — главная база
    about       TEXT,                -- чем занимается наша компания
    buyer       TEXT,                -- кто конечный покупатель
    score_mode  TEXT DEFAULT 'phone_sales',   -- phone_sales | generic
    created_at  INTEGER
);

-- Аккаунты Telegram. Общие для всех проектов, как и ключи: номер
-- компании проверяется одинаково, для какого бы проекта её ни нашли.
CREATE TABLE IF NOT EXISTS tg_accounts (
    id          INTEGER PRIMARY KEY,
    label       TEXT,
    phone       TEXT,
    api_id      TEXT,
    api_hash    TEXT,
    device      TEXT,               -- JSON: признаки устройства из файла продавца
    proxy       TEXT,
    file        TEXT,               -- путь к .session относительно data_dir
    status      TEXT DEFAULT 'unknown',   -- unknown | ok | unauthorized | banned | proxy_error | error
    who         TEXT,
    note        TEXT,
    checked_at  INTEGER,
    created_at  INTEGER
);
"""

# Ключи, общие для всех проектов: источники, модель, прокси, Telegram,
# обновления. Вводятся один раз. Всё остальное — описание клиента, «что
# продаём», условия, ящик рассылок — у каждого проекта своё.
GLOBAL_KEYS = {
    "dadata_token", "gis_key", "yandex_key", "vk_token", "sj_key",
    "hh_token", "hh_ua", "ai_key", "ai_url", "ai_model", "ai_kind",
    "ai_threads", "proxy_url", "update_repo", "update_token", "update_url",
    "active_project", "trash_files",
}
GLOBAL_PREFIXES = ("tg_",)

_state = {"active": ""}      # путь базы активного проекта; пусто — главная
_prepared = set()            # базы проектов, у которых схема уже на месте
_prep_lock = threading.Lock()


def main_path():
    return settings.db_path()


def current_path():
    """База, с которой работает этот поток.

    Сначала — проект, закреплённый за потоком: фоновая задача пишет туда,
    где её запустили, как бы ни переключали окно. Потом — активный
    проект. Потом — главная база.
    """
    return getattr(_local, "pinned", None) or _state["active"] or main_path()


def _open(path):
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    c = conns.get(path)
    if c is None:
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        # WAL нужен именно здесь: воркер пишет, интерфейс читает, и без него
        # каждый опрос таблицы блокировал бы запись.
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        conns[path] = c
        if path != main_path() and path not in _prepared:
            with _prep_lock:
                if path not in _prepared:
                    _migrate(c)
                    _prepared.add(path)
    return c


def conn():
    return _open(current_path())


def main_conn():
    return _open(main_path())


def _close_here(path):
    c = (getattr(_local, "conns", None) or {}).pop(path, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


@contextlib.contextmanager
def pinned(path):
    """Закрепить базу проекта за текущим потоком на время блока."""
    old = getattr(_local, "pinned", None)
    _local.pinned = path or None
    try:
        yield
    finally:
        _local.pinned = old


def carry(fn):
    """Обернуть функцию для пула потоков так, чтобы она писала в тот же
    проект, что и поток, который её отдал. Иначе обход сайтов из пула
    записывал бы в активный проект, а не в тот, где идёт задача."""
    path = current_path()

    def run(*a, **kw):
        with pinned(path):
            return fn(*a, **kw)
    return run


def _is_global(key):
    return key in GLOBAL_KEYS or key.startswith(GLOBAL_PREFIXES)


def project_file_path(row):
    f = (row["file"] if row is not None else "") or ""
    return os.path.join(settings.data_dir(), f) if f else main_path()


def projects():
    """Все проекты по порядку создания, с путями к базам."""
    rows = main_conn().execute("SELECT * FROM projects ORDER BY id").fetchall()
    return [dict(r, path=project_file_path(r)) for r in rows]


def get_project(project_id):
    row = main_conn().execute("SELECT * FROM projects WHERE id=?",
                              (int(project_id),)).fetchone()
    return dict(row, path=project_file_path(row)) if row is not None else None


def project_for_path(path):
    for p in projects():
        if p["path"] == path:
            return p
    return None


def current_project():
    """Проект, с которым работает этот поток."""
    return project_for_path(current_path()) or (projects() or [None])[0]


def active_project():
    return project_for_path(_state["active"] or main_path()) or (projects() or [None])[0]


def score_mode():
    p = current_project()
    return (p or {}).get("score_mode") or "phone_sales"


def set_active(project_id):
    p = get_project(project_id)
    if p is None:
        return False
    _state["active"] = "" if p["path"] == main_path() else p["path"]
    set_setting("active_project", p["id"])
    return True


def create_project(name, about="", buyer="", score_mode="phone_sales"):
    """Новый проект с пустой базой. Возвращает его словарь."""
    c = main_conn()
    cur = c.execute("INSERT INTO projects (name, file, about, buyer, score_mode, "
                    "created_at) VALUES (?,?,?,?,?,?)",
                    (name.strip()[:80] or "Проект", "", about, buyer,
                     score_mode if score_mode in ("phone_sales", "generic")
                     else "generic", now()))
    pid = cur.lastrowid
    folder = os.path.join(settings.data_dir(), "projects")
    os.makedirs(folder, exist_ok=True)
    c.execute("UPDATE projects SET file=? WHERE id=?",
              ("projects/p%d.sqlite3" % pid, pid))
    c.commit()
    p = get_project(pid)
    _open(p["path"])
    return p


def update_project(project_id, **fields):
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "about", "buyer", "score_mode") and v is not None}
    if "score_mode" in allowed and allowed["score_mode"] not in ("phone_sales", "generic"):
        allowed.pop("score_mode")
    if "name" in allowed:
        allowed["name"] = str(allowed["name"]).strip()[:80] or "Проект"
    if not allowed:
        return False
    c = main_conn()
    sets = ", ".join("%s=?" % k for k in allowed)
    cur = c.execute("UPDATE projects SET %s WHERE id=?" % sets,
                    tuple(allowed.values()) + (int(project_id),))
    c.commit()
    return cur.rowcount > 0


def delete_project(project_id):
    """Удалить проект с его базой. Первый проект (главную базу) — нельзя."""
    p = get_project(project_id)
    if p is None or p["path"] == main_path():
        return False
    if (_state["active"] or main_path()) == p["path"]:
        _state["active"] = ""
        set_setting("active_project", "")
    c = main_conn()
    c.execute("DELETE FROM projects WHERE id=?", (p["id"],))
    c.commit()
    _close_here(p["path"])
    _prepared.discard(p["path"])
    # Файл может держать фоновый поток — тогда удалим при следующем
    # запуске, а не упадём сейчас.
    left = []
    for suffix in ("", "-wal", "-shm"):
        f = p["path"] + suffix
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                left.append(f)
    if left:
        have = [x for x in (get_setting("trash_files") or "").split("|") if x]
        set_setting("trash_files", "|".join(have + left))
    return True


# ── Аккаунты Telegram ────────────────────────────────────
TG_FIELDS = ("label", "phone", "api_id", "api_hash", "device", "proxy",
             "file", "status", "who", "note", "checked_at")


def tg_accounts():
    return [dict(r) for r in main_conn().execute(
        "SELECT * FROM tg_accounts ORDER BY id")]


def tg_account(account_id):
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return None
    row = main_conn().execute("SELECT * FROM tg_accounts WHERE id=?",
                              (account_id,)).fetchone()
    return dict(row) if row is not None else None


def tg_active():
    """Аккаунт, которым идёт проверка номеров. Нет выбранного — первый."""
    got = tg_account(get_setting("tg_active", ""))
    if got is None:
        rows = tg_accounts()
        got = rows[0] if rows else None
    return got


def tg_account_add(**fields):
    """Новый аккаунт. Файл сеанса ему назначается сразу: a<id>.session."""
    c = main_conn()
    data = {k: fields.get(k) for k in TG_FIELDS if k in fields}
    data.setdefault("status", "unknown")
    data["created_at"] = now()
    keys = ", ".join(data)
    cur = c.execute("INSERT INTO tg_accounts (%s) VALUES (%s)"
                    % (keys, ", ".join("?" * len(data))), tuple(data.values()))
    aid = cur.lastrowid
    if not data.get("file"):
        c.execute("UPDATE tg_accounts SET file=? WHERE id=?",
                  (os.path.join("tg_accounts", "a%d.session" % aid), aid))
    c.commit()
    os.makedirs(os.path.join(settings.data_dir(), "tg_accounts"), exist_ok=True)
    return tg_account(aid)


def tg_account_update(account_id, **fields):
    data = {k: v for k, v in fields.items() if k in TG_FIELDS}
    if not data:
        return False
    c = main_conn()
    cur = c.execute("UPDATE tg_accounts SET %s WHERE id=?"
                    % ", ".join("%s=?" % k for k in data),
                    tuple(data.values()) + (int(account_id),))
    c.commit()
    return cur.rowcount > 0


def tg_session_path(acc):
    return os.path.join(settings.data_dir(), (acc or {}).get("file") or "")


def tg_account_delete(account_id):
    acc = tg_account(account_id)
    if acc is None:
        return False
    c = main_conn()
    c.execute("DELETE FROM tg_accounts WHERE id=?", (acc["id"],))
    c.commit()
    if str(get_setting("tg_active", "")) == str(acc["id"]):
        set_setting("tg_active", "")
    left = []
    path = tg_session_path(acc)
    for suffix in ("", "-journal", "-wal", "-shm"):
        f = path + suffix
        if acc.get("file") and os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                left.append(f)
    if left:
        have = [x for x in (get_setting("trash_files") or "").split("|") if x]
        set_setting("trash_files", "|".join(have + left))
    return True


def _migrate_tg_account():
    """Аккаунт из прошлых версий — первым в списке.

    Раньше аккаунт был один: файл telegram.session рядом с базой и ключи
    в настройках. Теперь это аккаунт №1 со своим файлом, и работает он
    так же, как работал.
    """
    c = main_conn()
    if c.execute("SELECT COUNT(*) n FROM tg_accounts").fetchone()["n"]:
        return
    old = os.path.join(settings.data_dir(), "telegram.session")
    api_id = get_setting("tg_api_id", "")
    if not os.path.exists(old) and not api_id:
        return
    acc = tg_account_add(phone=get_setting("tg_phone", ""), api_id=api_id,
                         api_hash=get_setting("tg_api_hash", ""),
                         device=get_setting("tg_device", ""),
                         proxy=get_setting("tg_proxy", ""),
                         label="Основной")
    if os.path.exists(old):
        try:
            os.replace(old, tg_session_path(acc))
        except OSError:
            # Файл занят — остаёмся на старом месте, путь запишем его.
            tg_account_update(acc["id"], file="telegram.session")
    set_setting("tg_active", acc["id"])


def _empty_trash():
    files = [x for x in (get_setting("trash_files") or "").split("|") if x]
    if not files:
        return
    left = []
    for f in files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            left.append(f)
    set_setting("trash_files", "|".join(left))


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


def _migrate(c):
    c.executescript(SCHEMA)
    for table, cols in (("companies", _LATER), ("searches", _LATER_SEARCHES)):
        have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
        for col, kind in cols.items():
            if col not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s"
                          % (table, col, kind))
    c.commit()


def init():
    c = main_conn()
    _migrate(c)
    c.executescript(PROJECTS_SCHEMA)
    c.commit()
    # Починка разовая: она проходит по всей таблице, а после первого
    # раза чинить нечего — новые записи приходят уже разобранными.
    mark = c.execute("SELECT value FROM settings WHERE key=?",
                     (_REPAIR_MARK,)).fetchone()
    if mark is None or mark["value"] != "1":
        _repair(c)
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')",
                  (_REPAIR_MARK,))
        c.commit()
    # Первый проект — на главную базу. У тех, кто обновился, это их
    # нынешняя база целиком, со всеми компаниями и настройками.
    if c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"] == 0:
        offer = c.execute("SELECT value FROM settings WHERE key='ai_offer'").fetchone()
        name = "Аналитика звонков"
        if offer and (offer["value"] or "").strip():
            name = offer["value"].strip().split("\n")[0][:60]
        c.execute("INSERT INTO projects (name, file, about, score_mode, created_at) "
                  "VALUES (?, '', ?, 'phone_sales', ?)",
                  (name, offer["value"] if offer else "", now()))
        c.commit()
    _empty_trash()
    _migrate_tg_account()
    # Активный проект — тот, что был открыт в прошлый раз.
    want = get_setting("active_project")
    p = get_project(want) if str(want).isdigit() else None
    _state["active"] = "" if (p is None or p["path"] == main_path()) else p["path"]
    for q in projects():
        if q["path"] != main_path():
            _open(q["path"])


# Две ошибки успели попасть в уже собранные базы: в «чем занимается»
# заезжал кусок разметки, а сайт сохранялся с чужими метками перехода.
# Разбор и то и другое чинит для новых компаний, но старые карточки от
# этого сами собой не исправятся — поэтому чиним их один раз здесь.
_JUNK = ("http-equiv", "charset=", "content-type", "<meta")
_REPAIR_MARK = "repaired_meta_and_utm"


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
    # Рассылка по удалённой компании тоже снимается: иначе отправщик
    # писал бы тем, кого человек из базы убрал, а номер строки потом
    # достался бы новой компании вместе с чужой историей писем.
    for table in ("contacts", "signals", "notes", "outreach", "sent_mail"):
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


def norm_region(region):
    """«г. Москва», «город Москва», «Москва г» → «Москва».

    Источники пишут город по-разному, а по городу вместе с названием
    опознаётся компания без ИНН. Работа России отдаёт «г. Москва», hh —
    «Москва», и без приведения одна компания жила двумя строками.
    """
    s = re.sub(r"\s+", " ", (region or "").strip())
    s = re.sub(r"^(г\.|г|город|гор\.)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+(г\.?|город)$", "", s, flags=re.I)
    return s.strip()


def phone_key(raw):
    """Телефон как ключ компании: «+7XXXXXXXXXX» или пусто.

    Бесплатные 8-800 ключом не служат: такой номер бывает у сети с
    десятком юрлиц и у аутсорсного колл-центра, отвечающего за чужие
    компании, — склейка по нему смешала бы разных.
    """
    d = re.sub(r"\D", "", raw or "")
    if len(d) != 11 or d[0] not in "78":
        return ""
    if d[1:3] == "80":
        return ""
    if len(set(d[1:])) <= 2:
        return ""
    return "+7" + d[1:]


def upsert_company(row, phones=None):
    """Добавить компанию или дополнить существующую.

    Дедупликация по ИНН, а при его отсутствии — по идентификатору работодателя
    с hh. Названия для этого не годятся: «ООО Ромашка» есть в каждом регионе,
    и склейка по имени смешала бы разные юрлица в одно.
    """
    c = conn()
    if row.get("region"):
        row = dict(row, region=norm_region(row["region"]))
    inn = (row.get("inn") or "").strip()
    hh_id = (row.get("hh_id") or "").strip()
    ogrn = (row.get("ogrn") or "").strip()
    found = None
    if inn:
        found = c.execute("SELECT * FROM companies WHERE inn=?", (inn,)).fetchone()
    # ОГРН так же однозначен, как ИНН: у юрлица он один на всю жизнь.
    if found is None and ogrn:
        found = c.execute("SELECT * FROM companies WHERE ogrn=?", (ogrn,)).fetchone()
        if found is not None and inn and (found["inn"] or "").strip() not in ("", inn):
            found = None
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

    # Совпадение по телефону. Справочник знает вывеску и телефон, портал
    # вакансий — юрлицо и тот же телефон, и других общих признаков у них
    # может не быть вовсе. Номер, который висит на нескольких компаниях
    # сразу (приёмная бизнес-центра, общий колл-центр), ключом не служит.
    if found is None and phones:
        for ph in phones[:4]:
            key = phone_key(ph)
            if not key:
                continue
            owners = c.execute(
                "SELECT DISTINCT company_id FROM contacts WHERE kind='phone' "
                "AND value=? LIMIT 3", (key,)).fetchall()
            if len(owners) != 1:
                continue
            x = c.execute("SELECT * FROM companies WHERE id=?",
                          (owners[0]["company_id"],)).fetchone()
            if x is None:
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
        # ИНН и номер работодателя hh уникальны. Если новое значение уже
        # стоит у другой карточки — это та же компания, и вместо падения
        # на уникальности вторая карточка вливается в найденную.
        for key in ("inn", "hh_id"):
            val = str(patch.get(key) or "").strip()
            if not val:
                continue
            other = c.execute("SELECT id FROM companies WHERE %s=? AND id<>?" % key,
                              (val, found["id"])).fetchone()
            if other is not None:
                merge_companies(found["id"], other["id"])
                c = conn()
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


def phones_to_check(limit=200, redo=False, landlines=False):
    """Телефоны, про которые ещё не спрашивали Telegram.

    Отметка живёт в том же поле verified, что и у почты: у телефона оно
    до сих пор всегда было «unchecked», так что новая колонка не нужна,
    а повторный прогон по проверенным номерам не тратит дневной предел
    впустую.
    """
    if redo:
        where = ""
    else:
        # skip — номер, который спрашивать незачем никогда: 8-800 и
        # обрывки. skip_land — городской, отложенный до отдельной
        # просьбы: он вернётся, когда её попросят, и до тех пор не
        # занимает собой окно выборки.
        seen = ["verified IS NULL", "verified='unchecked'"]
        if landlines:
            seen.append("verified='skip_land'")
        where = " AND (%s)" % " OR ".join(seen)
    return [dict(r) for r in conn().execute(
        "SELECT id, company_id, value, owner FROM contacts "
        "WHERE kind='phone'%s ORDER BY confidence DESC, id LIMIT ?" % where,
        (int(limit),))]


def set_contact_verified(contact_id, verified):
    c = conn()
    c.execute("UPDATE contacts SET verified=? WHERE id=?",
              (verified, int(contact_id)))
    c.commit()


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
                  "employees", "founded", "status", "hh_id", "next_step",
                  "next_date", "note", "ai_summary", "ai_fit", "ai_why",
                  "ai_hook", "ai_opener", "ai_kp", "callcenter"):
        if field in keep.keys() and not (keep[field] or "") and (drop[field] or ""):
            patch[field] = drop[field]
    # Стадию берём ту, что дальше по работе: если по дублю уже звонили,
    # эта отметка важнее «новой» у второй записи.
    if (keep["stage"] or "new") == "new" and (drop["stage"] or "new") != "new":
        patch["stage"] = drop["stage"]
    for table in ("contacts", "signals", "notes", "outreach", "sent_mail"):
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
        "SELECT id, name, inn, ogrn, site, score, region FROM companies "
        "ORDER BY id").fetchall()
    by_id = {r["id"]: r for r in rows}
    # Телефоны — только те, что принадлежат одной-двум компаниям. Номер
    # на пяти карточках — это приёмная или общий колл-центр, а не признак
    # того, что все пять — одна фирма.
    phones_of, owners = {}, {}
    for t in conn().execute("SELECT company_id, value FROM contacts "
                            "WHERE kind='phone'"):
        key = phone_key(t["value"])
        if key:
            phones_of.setdefault(t["company_id"], set()).add(key)
            owners.setdefault(key, set()).add(t["company_id"])
    seen, pairs = {}, []
    for r in rows:
        keys = []
        if (r["inn"] or "").strip():
            keys.append("инн:" + r["inn"].strip())
        if (r["ogrn"] or "").strip():
            keys.append("огрн:" + r["ogrn"].strip())
        for key in sorted(phones_of.get(r["id"], ())):
            if len(owners.get(key, ())) == 2:
                keys.append("тел:" + key)
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
            keys.append("имя:%s|%s" % (name, norm_region(r["region"]).lower()))
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
    c = main_conn() if _is_global(key) else conn()
    row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    c = main_conn() if _is_global(key) else conn()
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
    # ИНН уже записан у другой карточки — значит, это одна и та же
    # компания, найденная дважды: вывеска «МТС» из карты и «ПАО МТС» из
    # реестра. Раньше запись падала на уникальности ИНН и роняла всё
    # обогащение посреди списка. Теперь вторая карточка вливается в эту:
    # обогащение идёт по этой, и продолжать его надо здесь.
    merged = ""
    inn = str(ready.get("inn") or "").strip()
    if inn:
        other = c.execute("SELECT id, name FROM companies WHERE inn=? AND id<>?",
                          (inn, company_id)).fetchone()
        if other is not None:
            merged = other["name"] or ""
            merge_companies(company_id, other["id"])
            c = conn()
    ready["updated_at"] = now()
    sets = ", ".join("%s=?" % k for k in ready)
    c.execute("UPDATE companies SET %s WHERE id=?" % sets,
              tuple(ready.values()) + (company_id,))
    c.commit()
    ready.pop("updated_at", None)
    if merged:
        ready["merged_with"] = merged
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
