# -*- coding: utf-8 -*-
"""SuperJob — вторая по величине база вакансий после hh.

Официальный API, бесплатный. Нужен ключ приложения: он выдаётся сразу
после регистрации на api.superjob.ru, без проверки и без оплаты. Ключ
уходит в заголовке X-Api-App-Id.

Работодатели здесь пересекаются с hh лишь отчасти: небольшие компании
из регионов часто держат вакансии только на одной площадке. ИНН API не
отдаёт, поэтому склейка с остальными источниками идёт по сайту и по
названию с городом.

Предел API — пятьсот вакансий на один запрос. Глубже он отвечает
ошибкой, а не пустой страницей, поэтому страницы ограничены здесь.
"""
import time

import requests

from .. import net, settings
from . import hh

URL = "https://api.superjob.ru/2.0/vacancies/"
SOURCE = "SuperJob"
PER_PAGE = 100
LIMIT = 500
KEY_PAGE = "https://api.superjob.ru/register"

# Типы работодателя в ответе. «Кадровое агентство» у SuperJob — второй.
AGENCY_IDS = (2, "2")


def _session(session=None):
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT,
                      "Accept": "application/json"})
    return net.apply_proxy(s)


def period_param(days):
    """SuperJob знает только 1, 3, 7 дней или «всё время» (0)."""
    days = int(days or 0)
    for p in (1, 3, 7):
        if days <= p:
            return p
    return 0


def params_for(text, town="", period=30, page=0, in_title=True):
    p = {"count": PER_PAGE, "page": page, "period": period_param(period)}
    if in_title:
        # Поиск по названию должности: srws=1. Без этого на «оператор
        # колл-центра» приходят все, у кого слово стоит в описании.
        p.update({"keywords[0][srws]": 1, "keywords[0][skwc]": "and",
                  "keywords[0][keys]": text})
    else:
        p["keyword"] = text
    if town:
        p["town"] = town
    return p


def _phones(v):
    out = []
    for p in v.get("phones") or []:
        if isinstance(p, dict):
            num = str(p.get("number") or "").strip()
            if num and num not in out:
                out.append(num)
    one = str(v.get("phone") or "").strip()
    if one and one not in out:
        out.append(one)
    return out


def parse(data, skip_agencies=True, now=None):
    """Ответ API → вакансии в нашем виде. Возвращает (строки, есть_ещё)."""
    now = now or time.time()
    if not isinstance(data, dict):
        return [], False
    out = []
    for v in data.get("objects") or []:
        if not isinstance(v, dict):
            continue
        client = v.get("client") if isinstance(v.get("client"), dict) else {}
        name = str(client.get("title") or v.get("firm_name") or "").strip()
        if not name:
            continue
        agency = v.get("agency") if isinstance(v.get("agency"), dict) else {}
        if skip_agencies and (agency.get("id") in AGENCY_IDS
                              or hh.looks_like_agency(name)):
            continue
        town = v.get("town") if isinstance(v.get("town"), dict) else {}
        published = v.get("date_published") or 0
        try:
            fresh = max(0, int((now - float(published)) // 86400)) if published else None
        except (TypeError, ValueError):
            fresh = None
        pay = 0
        for f in ("payment_to", "payment_from"):
            try:
                pay = max(pay, int(v.get(f) or 0))
            except (TypeError, ValueError):
                pass
        email = str(v.get("email") or "").strip()
        out.append({
            "sj_id": str(client.get("id") or v.get("id_client") or ""),
            "name": name,
            "site": str(client.get("url") or "").strip(),
            "region": str(town.get("title") or "").strip(),
            "phones": _phones(v),
            "emails": [email] if "@" in email else [],
            "title": str(v.get("profession") or "").strip()[:120],
            "vac_id": str(v.get("id") or ""),
            "url": str(client.get("link") or v.get("link") or ""),
            "fresh": fresh, "salary": pay,
        })
    return out, bool(data.get("more"))


def fold(rows, found=None):
    """Вакансии → работодатели. Ключ — номер работодателя на SuperJob."""
    found = found if found is not None else {}
    for r in rows:
        key = r["sj_id"] or ("name:" + r["name"].lower())
        emp = found.get(key)
        if emp is None:
            emp = found[key] = {
                "name": r["name"], "sj_id": r["sj_id"], "inn": "", "ogrn": "",
                "site": r["site"], "region": r["region"], "phones": [],
                "emails": [], "contact_person": "", "titles": [],
                "vac_ids": [], "salaries": [], "fresh": None, "url": r["url"],
                "source": SOURCE}
        if r["vac_id"] and r["vac_id"] in emp["vac_ids"]:
            continue
        if r["vac_id"]:
            emp["vac_ids"].append(r["vac_id"])
        if r["title"] and r["title"] not in emp["titles"]:
            emp["titles"].append(r["title"])
        for f in ("phones", "emails"):
            for x in r[f]:
                if x not in emp[f]:
                    emp[f].append(x)
        if r["salary"]:
            emp["salaries"].append(r["salary"])
        if r["fresh"] is not None and (emp["fresh"] is None or r["fresh"] < emp["fresh"]):
            emp["fresh"] = r["fresh"]
        if not emp["site"] and r["site"]:
            emp["site"] = r["site"]
    for emp in found.values():
        emp["vacancies"] = len(emp["vac_ids"]) or 1
    return found


def explain(status, data):
    msg = ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        msg = str(data["error"].get("message") or "")
    if status in (401, 403) or "app" in msg.lower() or "key" in msg.lower():
        return ("SuperJob не принял ключ — проверьте «Ключ SuperJob» в "
                "настройках (Secret key со страницы приложения)")
    if status == 429:
        return "SuperJob: слишком частые запросы, попробуйте позже"
    return "SuperJob ответил %d%s" % (status, (": " + msg[:120]) if msg else "")


def search(text, key, town="", period=30, pages=5, in_title=True,
           skip_agencies=True, session=None, on_log=None, should_stop=None,
           errors=None, pause=0.4):
    """Работодатели по вакансиям. town — название города или пусто."""
    log = on_log or (lambda *_: None)
    if not key:
        return []
    s = _session(session)
    headers = {"X-Api-App-Id": key}
    found = {}
    pages = max(1, min(LIMIT // PER_PAGE, int(pages or 1)))
    for page in range(pages):
        if should_stop and should_stop():
            break
        try:
            r = s.get(URL, params=params_for(text, town, period, page, in_title),
                      headers=headers, timeout=25)
        except requests.RequestException as e:
            msg = "SuperJob не отвечает (%s)" % type(e).__name__
            log("   " + msg, "warn")
            if errors is not None:
                errors.append(msg)
            break
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code != 200 or not isinstance(data, dict):
            msg = explain(r.status_code, data)
            log("   " + msg, "warn")
            if errors is not None:
                errors.append(msg)
            break
        rows, more = parse(data, skip_agencies=skip_agencies)
        fold(rows, found)
        if page == 0:
            log("   SuperJob: вакансий по запросу %s" % (data.get("total") or len(rows)))
        if not more:
            break
        time.sleep(pause)
    return list(found.values())
