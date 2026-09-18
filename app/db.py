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
}


def init():
    c = conn()
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("PRAGMA table_info(companies)")}
    for col, kind in _LATER.items():
        if col not in have:
            c.execute("ALTER TABLE companies ADD COLUMN %s %s" % (col, kind))
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
    keys = company_keys(row)
    if not keys:
        return False
    marks = ",".join("?" for _ in keys)
    got = conn().execute(
        "SELECT 1 FROM blacklist WHERE key IN (%s) LIMIT 1" % marks, keys).fetchone()
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
    """Удалить компанию вместе со всем, что к ней относится."""
    c = conn()
    c.execute("DELETE FROM contacts WHERE company_id=?", (company_id,))
    c.execute("DELETE FROM signals WHERE company_id=?", (company_id,))
    c.execute("DELETE FROM companies WHERE id=?", (company_id,))
    c.commit()


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
        return
    c = conn()
    c.execute("""INSERT OR IGNORE INTO contacts
                 (company_id, kind, value, owner, confidence, verified, source, created_at)
                 VALUES (?,?,?,?,?,?,?,?)""",
              (company_id, kind, value, owner, confidence, verified, source, now()))
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
def create_task(kind, params=None, total=0):
    c = conn()
    cur = c.execute("""INSERT INTO tasks (kind, params, status, total, created_at, updated_at)
                       VALUES (?,?,'queued',?,?,?)""",
                    (kind, json.dumps(params or {}, ensure_ascii=False),
                     total, now(), now()))
    c.commit()
    return cur.lastrowid


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


def update_company_fields(company_id, patch):
    """Правки из интерфейса: стадия работы и заметка."""
    allowed = {k: v for k, v in (patch or {}).items()
               if k in ("stage", "note", "activity", "cms", "callcenter",
                        "employees", "founded", "status", "capital",
                        "branches", "founders_count", "founders",
                        "okveds_extra", "growth", "ai_summary", "ai_segment",
                        "ai_fit", "ai_why", "ai_hook", "ai_opener")}
    if not allowed:
        return
    allowed["updated_at"] = now()
    sets = ", ".join("%s=?" % k for k in allowed)
    c = conn()
    c.execute("UPDATE companies SET %s WHERE id=?" % sets,
              tuple(allowed.values()) + (company_id,))
    c.commit()
