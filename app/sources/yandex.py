# -*- coding: utf-8 -*-
"""Яндекс.Организации — второй справочник рядом с 2ГИС.

Зачем второй, если первый уже есть. Пробелы у справочников разные:
2ГИС силён там, где у него своя съёмка города, Яндекс — там, где
компания вела карточку сама. Пересечение неполное, и одна и та же
«стоматология» в двух городах находится то там, то там. Вместе покрытие
заметно шире, а дубли отсекаются при сведении по сайту и названию.

Плюс к этому Яндекс отдаёт ссылки, которые компания сама указала в
карточке, — включая её страницы в соцсетях. У 2ГИС такого поля нет.

Нужен бесплатный ключ Геопоиска (search-maps). Регистрируется в кабинете
разработчика за пять минут, без модерации.
"""
import time

import requests


API = "https://search-maps.yandex.ru/v1/"

# Сколько организаций отдаётся за раз. Больше API не вернёт, а меньше
# запрашивать нет смысла: лимит считается в запросах, а не в записях.
PAGE = 50


def search(query, city, key, pages=2, pause=0.3, session=None, on_log=None,
           should_stop=None):
    """Организации по запросу в городе.

    city — строка из справочника geo: нужны координаты и охват, потому
    что Яндекс ищет не по номеру региона, а по прямоугольнику на карте.
    """
    if not key or not (query or "").strip():
        return []
    if not (city or {}).get("ll"):
        # Без координат искать нечего: «Россия целиком» у Яндекса
        # превращается в поиск вокруг случайной точки.
        if on_log:
            on_log("Яндекс: для «%s» нет координат — пропускаю"
                   % (city or {}).get("name", "?"), "warn")
        return []

    s = session or requests.Session()
    out, seen = [], set()
    for page in range(pages):
        if should_stop and should_stop():
            break
        params = {
            "apikey": key, "text": query, "lang": "ru_RU", "type": "biz",
            "results": PAGE, "skip": page * PAGE,
            "ll": city["ll"], "spn": city.get("spn", "0.5,0.4"),
            # rspn=1 — не выходить за пределы города. Без него Яндекс
            # охотно досыпает организации из соседних областей, и в
            # списке «стоматологий Казани» оказывается Самара.
            "rspn": 1,
        }
        try:
            r = s.get(API, params=params, timeout=20)
        except Exception as e:
            if on_log:
                on_log("Яндекс недоступен: %s" % str(e)[:160], "warn")
            break
        if r.status_code == 403:
            if on_log:
                on_log("Яндекс ответил 403: ключ не подошёл или кончился "
                       "дневной лимит (бесплатно — 500 запросов в сутки).", "warn")
            break
        if r.status_code != 200:
            if on_log:
                on_log("Яндекс ответил %s: %s"
                       % (r.status_code, (r.text or "")[:160]), "warn")
            break
        try:
            data = r.json() or {}
        except Exception:
            break

        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            props = (f.get("properties") or {})
            meta = props.get("CompanyMetaData") or {}
            name = (meta.get("name") or props.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            phones = [p.get("formatted") or "" for p in (meta.get("Phones") or [])
                      if isinstance(p, dict)]
            # Ссылки, которые компания указала сама. Здесь и соцсети, и
            # иногда прямой телеграм — то, чего нет ни в ЕГРЮЛ, ни на
            # половине сайтов.
            links = [l.get("href") or "" for l in (meta.get("Links") or [])
                     if isinstance(l, dict)]
            out.append({
                "name": name,
                "address": meta.get("address") or "",
                "site": meta.get("url") or "",
                "phones": [p for p in phones if p][:4],
                "links": [l for l in links if l][:8],
                "rubric": ", ".join(c.get("name", "")
                                    for c in (meta.get("Categories") or [])[:2]
                                    if isinstance(c, dict)),
            })
        if on_log:
            on_log("Яндекс: страница %d — организаций %d, всего %d"
                   % (page + 1, len(feats), len(out)))
        if len(feats) < PAGE:
            break
        time.sleep(pause)
    return out
