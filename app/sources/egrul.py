# -*- coding: utf-8 -*-
"""ЕГРЮЛ на сайте ФНС (egrul.nalog.ru) — тот же реестр, что у DaData, без ключа.

Поиск в два шага, как у самого сайта: сначала запрос отдаёт номер,
по номеру забирается результат. Пока ФНС собирает ответ, она отвечает
«подождите», и спрашивать приходится ещё раз.

Ищет по названию и по региону. Регион у ФНС — субъект целиком, поэтому
для города внутри области лишнее отсеивается по адресу: на запрос про
Пятигорск ФНС вернёт весь Ставропольский край.

Частые запросы ФНС встречает капчей. Программа её не обходит: говорит,
что случилось, и идёт к следующему источнику. Паузы между страницами
держат это событие редким.
"""
import re
import time

import requests

from .. import net, settings

BASE = "https://egrul.nalog.ru"
SOURCE = "ЕГРЮЛ (ФНС)"


def _session(session=None):
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT,
                      "Accept": "application/json, text/javascript, */*",
                      "X-Requested-With": "XMLHttpRequest",
                      "Referer": BASE + "/index.html"})
    return net.apply_proxy(s)


def _up(s):
    return (s or "").upper().replace("Ё", "Е")


def split_head(g):
    """«ГЕНЕРАЛЬНЫЙ ДИРЕКТОР: ИВАНОВ ИВАН ИВАНОВИЧ» → (должность, ФИО)."""
    g = (g or "").strip()
    if ":" not in g:
        return "", ""
    post, fio = g.split(":", 1)
    fio = " ".join(w[:1].upper() + w[1:].lower() for w in fio.split())
    post = post.strip().lower()
    return (post[:1].upper() + post[1:]) if post else "", fio.strip()


def parse(data, city="", filter_city=False):
    """Строки ответа ФНС → компании. Возвращает (компании, всего)."""
    if not isinstance(data, dict):
        return [], 0
    rows = data.get("rows") or []
    total = 0
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            total = max(total, int(r.get("tot") or r.get("cnt") or 0))
        except (TypeError, ValueError):
            pass
        # Ликвидированные и ИП пропускаем: первым некому продавать,
        # у вторых нет ни адреса, ни руководителя в выдаче.
        if r.get("e") or (r.get("k") and r.get("k") != "ul"):
            continue
        inn = re.sub(r"\D", "", str(r.get("i") or ""))
        name = str(r.get("c") or r.get("n") or "").strip()
        if not inn or not name:
            continue
        addr = str(r.get("a") or "").strip()
        if filter_city and city and addr and _up(city) not in _up(addr):
            continue
        post, fio = split_head(r.get("g"))
        out.append({"name": name, "inn": inn,
                    "ogrn": re.sub(r"\D", "", str(r.get("o") or "")),
                    "address": addr, "director": fio, "director_post": post,
                    "region": city or str(r.get("rn") or "").strip()})
    return out, total


def _result(s, token, tries=10, wait=1.0):
    for _ in range(tries):
        r = s.get(BASE + "/search-result/" + token,
                  params={"r": int(time.time() * 1000)}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "wait":
            time.sleep(wait)
            continue
        return data
    return None


def search(query, region="", city="", pages=3, session=None, on_log=None,
           should_stop=None, errors=None, pause=1.2, city_is_subject=True):
    """Компании по названию. region — номер субъекта, city — для отсева по адресу."""
    log = on_log or (lambda *_: None)
    s = _session(session)
    found, seen = [], set()
    for page in range(1, max(1, int(pages or 1)) + 1):
        if should_stop and should_stop():
            break
        try:
            r = s.post(BASE + "/", data={
                "vyp3CaptchaToken": "", "page": "" if page == 1 else str(page),
                "query": query, "region": region or "",
                "PreventChromeAutocomplete": ""}, timeout=20)
            r.raise_for_status()
            head = r.json()
            if head.get("captchaRequired"):
                msg = ("ФНС попросила капчу — слишком много запросов подряд. "
                       "ЕГРЮЛ ФНС пропущен, остальные источники работают")
                log("   " + msg, "warn")
                if errors is not None:
                    errors.append(msg)
                break
            token = head.get("t")
            if not token:
                break
            data = _result(s, token)
        except (requests.RequestException, ValueError) as e:
            msg = "ЕГРЮЛ ФНС не отвечает (%s)" % type(e).__name__
            log("   " + msg, "warn")
            if errors is not None:
                errors.append(msg)
            break
        rows, total = parse(data, city=city, filter_city=not city_is_subject)
        new = 0
        for row in rows:
            if row["inn"] not in seen:
                seen.add(row["inn"])
                found.append(row)
                new += 1
        if page == 1:
            log("   ЕГРЮЛ ФНС: записей по запросу %d" % total)
        got = len((data or {}).get("rows") or [])
        if not got or (total and page * 20 >= total):
            break
        time.sleep(pause)
    return found
