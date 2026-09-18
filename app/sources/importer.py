# -*- coding: utf-8 -*-
"""Импорт своего списка: ИНН, домены или названия — одним текстом.

Самый частый сценарий после первой недели работы: у человека уже есть
список компаний — выгрузка с выставки, таблица от коллеги, свои клиенты.
Заставлять его искать их заново через hh было бы издевательством.

Разбираем построчно и сами определяем, что это. Смешанный список —
норма, а не ошибка ввода.
"""
import re

INN_RE = re.compile(r"^\d{10}$|^\d{12}$")
DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)/?", re.I)
# Разделители внутри строки: выгрузки из Excel приходят и через точку с
# запятой, и через табуляцию, и «ИНН — Название» одной строкой.
SPLIT_RE = re.compile(r"[;\t|]+")


def classify(line):
    """Что это: ИНН, домен или название."""
    v = (line or "").strip().strip('"').strip()
    if not v:
        return None
    if INN_RE.match(v.replace(" ", "")):
        return ("inn", v.replace(" ", ""))
    m = DOMAIN_RE.match(v)
    if m and "." in m.group(1) and " " not in v:
        return ("site", "https://" + m.group(1).lower())
    # Голые цифры, не похожие на ИНН, — это остатки нумерации строк или
    # обрезанные телефоны из выгрузки, а не названия компаний.
    if v.replace(" ", "").isdigit():
        return None
    if len(v) >= 3:
        return ("name", v[:200])
    return None


def parse(text, limit=5000):
    """Текст из формы → список записей для добавления в базу.

    Строка вида «7701234567;ООО Ромашка;romashka.ru» разбирается целиком:
    из выгрузок такое приходит постоянно, и брать из неё только первое
    поле значило бы терять уже готовые данные.
    """
    rows, seen = [], set()
    for raw in (text or "").splitlines():
        if len(rows) >= limit:
            break
        parts = [p for p in SPLIT_RE.split(raw) if p.strip()] or [raw]
        item = {}
        for part in parts:
            got = classify(part)
            if not got:
                continue
            kind, value = got
            # Первое значение каждого вида выигрывает: в выгрузках второе
            # поле того же типа — обычно юридическое дублирующее название.
            item.setdefault(kind, value)
        if not item:
            continue
        key = item.get("inn") or item.get("site") or item.get("name")
        if key in seen:
            continue
        seen.add(key)
        rows.append({"inn": item.get("inn", ""), "site": item.get("site", ""),
                     "name": item.get("name", "") or item.get("site", "")
                     or item.get("inn", "")})
    return rows
