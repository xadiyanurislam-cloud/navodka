# -*- coding: utf-8 -*-
"""DaData — ФИО руководителя и реквизиты из ЕГРЮЛ.

Почему именно DaData, а не парсинг Rusprofile или СПАРК: те прямо
запрещают автоматический сбор своими правилами, стоят за антиботом и
блокируют по IP. DaData отдаёт те же данные ЕГРЮЛ официально, по API, с
бесплатным тарифом на первые тысячи запросов в сутки.

Сведения о руководителе юрлица — открытые данные ЕГРЮЛ. Это не отменяет
того, что ФИО плюс контакт — уже персональные данные: см. README.
"""
import requests

from .. import settings

SUGGEST = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
FIND_BY_ID = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


def _headers(token):
    return {"Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Token " + token,
            "User-Agent": settings.USER_AGENT}


def _unpack(item):
    # Элемент списка может прийти пустым: у ответа чужого сервера нет
    # обязательства быть таким, каким мы его ждём, а падение разбора
    # останавливает всё обогащение целиком.
    if not isinstance(item, dict):
        return {}
    d = item.get("data") or {}
    mgmt = d.get("management") or {}
    addr = d.get("address") or {}
    state = d.get("state") or {}

    okved_name, extra = "", []
    for o in (d.get("okveds") or []):
        if not isinstance(o, dict):
            continue
        if o.get("main"):
            okved_name = o.get("name") or ""
        elif o.get("name"):
            extra.append("%s %s" % (o.get("code") or "", o["name"]))

    # Учредители: сколько их и кто главный. Один владелец, он же директор —
    # это компания, где решение принимается за один разговор. Десять
    # учредителей — согласование, о котором лучше знать заранее.
    founders = d.get("founders") or []
    founder_names = [f.get("name") or (f.get("fio") or {}).get("source") or ""
                     for f in founders]
    founder_names = [f for f in founder_names if f]

    return {
        "name": (d.get("name") or {}).get("short_with_opf") or item.get("value") or "",
        "inn": d.get("inn") or "",
        "ogrn": d.get("ogrn") or "",
        "director": mgmt.get("name") or "",
        "director_post": mgmt.get("post") or "",
        "okved": d.get("okved") or "",
        "okved_name": okved_name,
        "address": (addr.get("value") or ""),
        "region": ((addr.get("data") or {}).get("region_with_type") or ""),
        "status": (state.get("status") or ""),
        "employees": d.get("employee_count") or None,
        # Уставный капитал. Сам по себе слабый признак, но 10 000 рублей
        # при выручке в триста миллионов — это компания, которая не
        # собиралась ни перед кем отчитываться, и это видно в переговорах.
        "capital": ((d.get("capital") or {}).get("value") or None),
        "branches": d.get("branch_count") or 0,
        "okveds_extra": "; ".join(extra[:6]),
        "founders_count": len(founders),
        "founders": "; ".join(founder_names[:3]),
        "type": (d.get("type") or ""),          # LEGAL или INDIVIDUAL
        "liquidation": _year(state.get("liquidation_date")),
        # Дата регистрации приходит миллисекундами. Возраст компании —
        # рабочий признак: фирма трёх месяцев от роду и та же ниша с
        # пятнадцатью годами продаются совершенно по-разному.
        "founded": _year((d.get("state") or {}).get("registration_date")),
        "opf": ((d.get("opf") or {}).get("short") or ""),
    }


def _year(ms):
    if not ms:
        return None
    try:
        import time
        return int(time.gmtime(int(ms) / 1000).tm_year)
    except Exception:
        return None


def by_name(name, token, count=1, timeout=15, session=None):
    """Поиск юрлица по названию.

    Название с hh не всегда совпадает с записью в ЕГРЮЛ («Ромашка» против
    «ООО \"Ромашка\"»), поэтому берём первое совпадение и обязательно
    проверяем статус: ликвидированные попадаются постоянно.
    """
    if not (name or "").strip() or not token:
        return {}
    s = session or requests.Session()
    try:
        r = s.post(SUGGEST, json={"query": name, "count": count,
                                  "status": ["ACTIVE"], "type": "LEGAL"},
                   headers=_headers(token), timeout=timeout)
        r.raise_for_status()
        items = (r.json() or {}).get("suggestions") or []
    except Exception:
        return {}
    if not items:
        return {}
    return _unpack(items[0])


def search_by_name(query, token, count=20, region="", timeout=20, session=None,
                   on_log=None, explain=True):
    """Действующие юрлица, у которых искомое слово стоит в названии.

    Это второй способ искать «стоматологии» — не по справочнику
    организаций, а по ЕГРЮЛ. В России вид деятельности очень часто вынесен
    прямо в название: «Стоматология Улыбка», «АвтоТрансЛогистика». Улов
    получается другой, чем у 2ГИС, и потому полезный: здесь сразу есть ИНН
    и ФИО руководителя, ради которых иначе делался бы отдельный запрос.

    Ограничение источника: за один запрос отдаётся не больше двадцати
    совпадений. Поэтому запрос повторяется по городам — двадцать на
    каждый, а не двадцать на всю страну.
    """
    if not (query or "").strip() or not token:
        return []
    s = session or requests.Session()
    body = {"query": query, "count": max(1, min(20, int(count or 20))),
            "status": ["ACTIVE"]}
    if region:
        # Фильтр по адресу регистрации. Без него «стоматология» приносит
        # двадцать компаний со всей страны — по одной из каждого города.
        #
        # Два условия, а не одно. Раньше город подставлялся в поле
        # «регион», и это совпадало только для Москвы и Петербурга: они
        # сами себе регионы. Для Новосибирска регион — «Новосибирская
        # область», и фильтр не совпадал никогда, то есть ЕГРЮЛ молча
        # возвращал ноль по всем городам, кроме двух. Снаружи это
        # выглядело как «ЕГРЮЛ не работает».
        #
        # Внутри объекта условия складываются по «и», между объектами —
        # по «или»: подходит либо регион с таким названием, либо город.
        body["locations"] = [{"region": region}, {"city": region}]
    try:
        r = s.post(SUGGEST, json=body, headers=_headers(token), timeout=timeout)
        if r.status_code != 200:
            if on_log:
                on_log("ЕГРЮЛ ответил %s: %s" % (r.status_code, (r.text or "")[:160]),
                       "warn")
            return []
        items = (r.json() or {}).get("suggestions") or []
    except Exception as e:
        if on_log:
            on_log("ЕГРЮЛ недоступен: %s" % str(e)[:160], "warn")
        return []
    out = []
    for it in items:
        row = _unpack(it)
        if row.get("name"):
            out.append(row)
    # Ноль — тоже ответ, и о нём надо сказать: молчаливый ноль человек
    # читает как поломку источника, хотя чаще это значит, что слова нет
    # в названиях. ЕГРЮЛ ищет именно по названию: «Дента-Люкс» по слову
    # «стоматология» здесь не найдётся никогда.
    # Объяснение пустого ответа нужно один раз за прогон. Поиск идёт
    # по нескольким близким словам, и повторённое пять раз подряд
    # объяснение перестаёт читаться вовсе.
    if on_log and explain and not out:
        on_log("ЕГРЮЛ: по слову «%s»%s в названиях ничего нет. Здесь ищут "
               "по названию юрлица, а не по виду деятельности."
               % (query, (" в «%s»" % region) if region else ""), "warn")
    return out


def by_inn(inn, token, timeout=15, session=None):
    if not (inn or "").strip() or not token:
        return {}
    s = session or requests.Session()
    try:
        r = s.post(FIND_BY_ID, json={"query": inn, "count": 1},
                   headers=_headers(token), timeout=timeout)
        r.raise_for_status()
        items = (r.json() or {}).get("suggestions") or []
    except Exception:
        return {}
    return _unpack(items[0]) if items else {}
