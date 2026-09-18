# -*- coding: utf-8 -*-
"""ФНС, ГИР БО — выручка и численность по ИНН.

Зачем: размер компании отсеивает больше мусора, чем любой другой фильтр.
Микрофирма с оборотом в два миллиона не купит ничего, корпорация уйдёт в
полугодовые согласования. Рабочий диапазон видно только по отчётности.

Источник официальный и бесплатный, ключа не требует: бухгалтерская
отчётность юрлиц публикуется в открытом доступе по закону.
"""
import requests

from .. import settings

BASE = "https://bo.nalog.ru"

# Коды строк отчёта о финансовых результатах. Названия у них в выгрузке
# разные от года к году, а коды не меняются — по ним и берём.
REVENUE = "2110"      # выручка
PROFIT = "2400"       # чистая прибыль


def _session(session=None):
    s = session or requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT,
                      "Accept": "application/json"})
    return s


def find_org(inn, session=None, timeout=15):
    """Карточка организации в ГИР БО по ИНН."""
    inn = (inn or "").strip()
    if not inn.isdigit():
        return {}
    s = _session(session)
    try:
        r = s.get(BASE + "/nbo/organizations/search",
                  params={"query": inn, "page": 0}, timeout=timeout)
        r.raise_for_status()
        items = r.json()
    except Exception:
        return {}
    if isinstance(items, dict):
        items = items.get("content") or items.get("items") or []
    if not items:
        return {}
    o = items[0] or {}
    return {
        "gir_id": o.get("id"),
        "name": o.get("shortName") or o.get("fullName") or "",
        "inn": o.get("inn") or inn,
        "ogrn": o.get("ogrn") or "",
        "okved": (o.get("okved2") or {}).get("code") if isinstance(o.get("okved2"), dict) else "",
    }


def _pick(rows, code):
    """Достать значение строки отчёта по коду, в каком бы виде он ни лежал."""
    if not isinstance(rows, dict):
        return None
    for key, val in rows.items():
        if str(key).endswith(code) and isinstance(val, (int, float)):
            return val
    return None


def financials(gir_id, session=None, timeout=20, years=3):
    """Выручка и прибыль за несколько лет, в рублях.

    Несколько лет, а не один: цифра за прошлый год говорит о размере, а
    динамика — о том, что с компанией происходит. Растущая покупает и
    пробует новое, падающая режет бюджеты и ничего не подписывает. По
    одной точке это неразличимо.

    Разбор намеренно терпимый: структура выгрузки ГИР БО менялась уже
    дважды, и жёсткий разбор ломался бы на каждом изменении. Не нашли —
    вернули пусто, а не уронили обход тысячи компаний.
    """
    if not gir_id:
        return {}
    s = _session(session)
    try:
        r = s.get("%s/nbo/organizations/%s/bfo/" % (BASE, gir_id), timeout=timeout)
        r.raise_for_status()
        periods = r.json()
    except Exception:
        return {}
    if not isinstance(periods, list) or not periods:
        return {}

    periods.sort(key=lambda p: p.get("period") or p.get("year") or 0, reverse=True)
    series = []
    for p in periods[:years + 2]:
        fin = (p.get("correction") or p) or {}
        res = fin.get("financialResult") or fin.get("financialResult2") or {}
        revenue = _pick(res, REVENUE)
        profit = _pick(res, PROFIT)
        if revenue is None and isinstance(fin.get("financialResults"), dict):
            revenue = _pick(fin["financialResults"], REVENUE)
            profit = _pick(fin["financialResults"], PROFIT)
        if revenue is None:
            continue
        series.append({"year": p.get("period") or p.get("year"),
                       "revenue": int(revenue), "profit": int(profit or 0)})
        if len(series) >= years:
            break
    if not series:
        return {}
    out = dict(series[0])
    out["series"] = series
    return out


def by_inn(inn, session=None):
    """Всё вместе: карточка плюс финансы."""
    org = find_org(inn, session=session)
    if not org:
        return {}
    org.update(financials(org.get("gir_id"), session=session))
    return org


def growth(series):
    """Что происходит с выручкой: (ярлык, проценты за последний год).

    Пороги не круглые: до 8% в обе стороны — это шум инфляции и разовых
    контрактов, а не рост. Называть таким словом «рост» значит вводить
    продавца в заблуждение ровно там, где он строит разговор.
    """
    rows = [r for r in (series or []) if r.get("revenue")]
    if len(rows) < 2:
        return ("", 0)
    new, old = rows[0]["revenue"], rows[1]["revenue"]
    if not old:
        return ("", 0)
    pct = round((new - old) / float(old) * 100)
    if pct >= 25:
        return ("быстрый рост", pct)
    if pct >= 8:
        return ("рост", pct)
    if pct <= -25:
        return ("сильный спад", pct)
    if pct <= -8:
        return ("спад", pct)
    return ("на месте", pct)


def size_band(revenue):
    """Словесная оценка размера — по ней удобнее фильтровать, чем по рублям."""
    if not revenue:
        return ""
    million = revenue / 1_000_000.0
    if million < 10:
        return "микро"
    if million < 50:
        return "малый"
    if million < 800:
        return "средний"
    return "крупный"
