# -*- coding: utf-8 -*-
"""2ГИС — локальный бизнес по рубрике и городу.

hh.ru находит тех, кто нанимает. Но клиники, автосервисы и салоны часто
не публикуют вакансий вовсе, а звонков у них больше, чем у иной
софтверной компании. Их берут по справочнику: рубрика плюс город.

Нужен ключ Places API (бесплатный тариф есть). Без ключа источник просто
не показывается — заглушек и демо-ключей в коде нет: чужой ключ в чужой
программе отзывают в первый же день.
"""
import time

import requests

from .. import settings

API = "https://catalog.api.2gis.com/3.0/items"

# Рубрики, где звонок решает сделку. Формулировки те же, что в поиске 2ГИС.
RUBRICS = [
    "стоматология", "медицинский центр", "автосервис", "автосалон",
    "агентство недвижимости", "фитнес-клуб", "юридические услуги",
    "страховая компания", "туристическое агентство", "школа иностранных языков",
    "окна пластиковые", "натяжные потолки", "грузоперевозки", "клининг",
]

CITIES = [
    ("Москва", 32), ("Санкт-Петербург", 38), ("Новосибирск", 1),
    ("Екатеринбург", 54), ("Казань", 33), ("Нижний Новгород", 36),
    ("Краснодар", 20), ("Ростов-на-Дону", 45), ("Самара", 46),
    ("Челябинск", 55), ("Уфа", 49), ("Красноярск", 62),
]


# Докуда искать вокруг точки, когда номера региона нет.
#
# Сорок километров — потолок, который принимает сам справочник. Он же
# примерно равен охвату миллионника с пригородами, так что запас брать
# некуда и незачем.
POINT_RADIUS = 40000


def search(query, region_id, key, pages=2, page_size=50, pause=0.4,
           session=None, on_log=None, should_stop=None, point=""):
    """Организации по рубрике в городе: название, сайт, телефоны, адрес.

    Город задаётся одним из двух способов, и это не роскошь.

    Номер региона знает только сам справочник, и в программе их
    двенадцать — те, что удалось выписать. Для остальных ста тридцати
    городов источник просто пропускался: у Перми, Тюмени и Владивостока
    2ГИС не работал вовсе, хотя в самом справочнике эти города есть.

    Поэтому второй способ — точка и радиус. Координаты у программы есть
    для всех городов: по ним и так ищет карта. Номер региона, если он
    известен, остаётся первым: он точнее очерчивает город, чем круг в
    сорок километров.
    """
    if not key:
        return []
    if not region_id and not point:
        if on_log:
            on_log("2ГИС: для этого города нет ни номера региона, ни "
                   "координат — пропускаю", "warn")
        return []
    s = session or requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT})
    out = []
    for page in range(1, pages + 1):
        if should_stop and should_stop():
            break
        params = {
            "q": query, "key": key,
            "page": page, "page_size": page_size,
            "fields": "items.contact_groups,items.address,items.external_content,"
                      "items.rubrics,items.org",
        }
        if region_id:
            params["region_id"] = region_id
        else:
            params["point"] = point
            params["radius"] = POINT_RADIUS
        try:
            r = s.get(API, params=params, timeout=20)
            data = r.json()
        except Exception as e:
            if on_log:
                on_log("2ГИС: страница %d не загрузилась — %s" % (page, e), "warn")
            break

        meta = data.get("meta") or {}
        if meta.get("code") and meta["code"] != 200:
            if on_log:
                on_log("2ГИС отказал: %s" % (meta.get("error") or {}).get("message", meta["code"]), "warn")
            break

        items = ((data.get("result") or {}).get("items")) or []
        if not items:
            break
        for it in items:
            phones, sites, emails = [], [], []
            for group in (it.get("contact_groups") or []):
                if not isinstance(group, dict):
                    continue
                for c in (group.get("contacts") or []):
                    if not isinstance(c, dict):
                        continue
                    kind, value = c.get("type"), (c.get("value") or c.get("url") or "")
                    if kind == "phone" and value:
                        phones.append(value)
                    elif kind == "website" and value:
                        sites.append(value)
                    elif kind == "email" and value:
                        emails.append(value.lower())
            out.append({
                "name": it.get("name") or "",
                "address": (it.get("address_name") or
                            (it.get("address") or {}).get("name") or ""),
                "site": sites[0] if sites else "",
                "phones": phones[:4],
                "emails": emails[:3],
                "rubric": ", ".join(r.get("name", "") for r in (it.get("rubrics") or [])[:2]
                                  if isinstance(r, dict)),
            })
        if on_log:
            on_log("2ГИС: страница %d — организаций %d" % (page, len(items)))
        time.sleep(pause)
    return out
