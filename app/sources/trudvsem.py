# -*- coding: utf-8 -*-
"""Работа России (trudvsem.ru) — государственная база вакансий.

Открытый API без ключа и без регистрации. Главное его достоинство для
нас — у каждой вакансии есть ИНН и ОГРН работодателя. Компания отсюда
приходит сразу опознанной, и склейка с ЕГРЮЛ, hh и картами идёт по
ИНН, а не по похожему названию.

Второе — контакты. Работодатель на портале обязан указать, куда
откликаться, и это обычно живой телефон и почта отдела кадров или
самого руководителя, если компания небольшая.

Портал ищет по всему тексту вакансии, фильтра «только в названии» у
него нет. Поэтому название проверяется здесь, после ответа: иначе на
«оператор колл-центра» приходят курьеры, у которых в описании написано
«общение с оператором».
"""
import datetime
import re
import time

import requests

from .. import net, settings

BASES = ("https://opendata.trudvsem.ru/api/v1/vacancies",
         "http://opendata.trudvsem.ru/api/v1/vacancies")
SOURCE = "Работа России"
PER_PAGE = 100
# Глубже портал не отдаёт: смещение считается страницами, и после
# полусотни он отвечает пустым списком.
MAX_PAGES = 50

AGENCY_FLAG = ("hr-agency", "hr_agency")


def _session(session=None):
    if session is not None:
        return session
    s = requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT,
                      "Accept": "application/json"})
    return net.apply_proxy(s)


def region_param(code):
    """Код субъекта для портала — 13 цифр КЛАДР: «77» → «7700000000000»."""
    code = (code or "").strip()
    return (code + "0" * 11) if code.isdigit() and len(code) == 2 else ""


def _words(text):
    return [w for w in re.findall(r"[\wё]+", (text or "").lower()) if len(w) >= 3]


def title_matches(title, query):
    """Все слова запроса есть в названии вакансии — с точностью до окончания.

    «оператор колл-центра» находит «Оператора call-центра»? Нет, и это
    честно: латиницу и кириллицу не сводим, для этого у запросов есть
    готовые наборы. Зато «Оператор колл-центра (входящие)» и «Операторы
    колл-центров» находятся.
    """
    have = _words(title)
    for w in _words(query):
        stem = w[:max(4, len(w) - 2)]
        if not any(h.startswith(stem) for h in have):
            return False
    return True


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def _num(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _contacts(v):
    """Телефоны и почты из вакансии: и из списка контактов, и из карточки."""
    phones, emails = [], []
    items = v.get("contact_list") or []
    if isinstance(items, dict):
        items = [items]
    for c in items:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("contact_type") or "").lower()
        val = str(c.get("contact_value") or "").strip()
        if not val:
            continue
        if "@" in val or "почт" in kind or "mail" in kind:
            if val not in emails:
                emails.append(val)
        elif "тел" in kind or re.search(r"\d{5,}", re.sub(r"\D", "", val)):
            if val not in phones:
                phones.append(val)
    comp = v.get("company") or {}
    for key in ("email",):
        val = str(comp.get(key) or "").strip()
        if "@" in val and val not in emails:
            emails.append(val)
    for key in ("phone",):
        val = str(comp.get(key) or "").strip()
        if val and val not in phones:
            phones.append(val)
    return phones, emails


def parse(data, query="", in_title=False, skip_agencies=True, today=None):
    """Разобрать ответ портала в работодателей. Возвращает (строки, всего)."""
    today = today or datetime.date.today()
    if not isinstance(data, dict):
        return [], 0
    meta = data.get("meta") or {}
    total = _num(meta.get("total"))
    res = data.get("results") or {}
    items = res.get("vacancies") if isinstance(res, dict) else None
    out = []
    for it in items or []:
        v = (it or {}).get("vacancy") if isinstance(it, dict) else None
        if not isinstance(v, dict):
            continue
        comp = v.get("company") or {}
        name = str(comp.get("name") or "").strip()
        if not name:
            continue
        if skip_agencies and any(comp.get(f) in (True, "true", "1", 1)
                                 for f in AGENCY_FLAG):
            continue
        title = str(v.get("job-name") or v.get("job_name") or "").strip()
        if in_title and query and not title_matches(title, query):
            continue
        phones, emails = _contacts(v)
        created = _date(v.get("creation-date") or v.get("creation_date"))
        region = (v.get("region") or {}).get("name") if isinstance(
            v.get("region"), dict) else ""
        addr = ""
        adrs = v.get("addresses")
        if isinstance(adrs, dict):
            adrs = adrs.get("address")
        if isinstance(adrs, dict):
            adrs = [adrs]
        for a in adrs or []:
            if isinstance(a, dict) and a.get("location"):
                addr = str(a["location"]).strip()
                break
        out.append({
            "name": name,
            "inn": re.sub(r"\D", "", str(comp.get("inn") or "")),
            "ogrn": re.sub(r"\D", "", str(comp.get("ogrn") or "")),
            "site": str(comp.get("site") or comp.get("url") or "").strip(),
            "region": str(region or "").strip(),
            "address": addr[:300],
            "phones": phones, "emails": emails,
            "contact_person": str(v.get("contact_person") or "").strip()[:120],
            "title": title[:120],
            "vac_id": str(v.get("id") or ""),
            "url": str(v.get("vac_url") or ""),
            "fresh": (today - created).days if created else None,
            "salary": max(_num(v.get("salary_min")), _num(v.get("salary_max"))),
        })
    return out, total


def fold(rows, found=None):
    """Свернуть вакансии до работодателей. Ключ — ИНН, без него — название."""
    found = found if found is not None else {}
    for r in rows:
        key = ("inn:" + r["inn"]) if r["inn"] else ("name:" + r["name"].lower())
        emp = found.get(key)
        if emp is None:
            emp = found[key] = {
                "name": r["name"], "inn": r["inn"], "ogrn": r["ogrn"],
                "site": r["site"], "region": r["region"],
                "address": r["address"], "phones": [],
                "emails": [], "contact_person": r["contact_person"],
                "titles": [], "vac_ids": [], "salaries": [], "fresh": None,
                "url": r["url"], "source": SOURCE}
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
        for f in ("site", "ogrn", "contact_person", "address"):
            if not emp[f] and r[f]:
                emp[f] = r[f]
    for emp in found.values():
        emp["vacancies"] = len(emp["vac_ids"]) or 1
    return found


def search(text, region="", period=30, pages=5, in_title=True,
           skip_agencies=True, session=None, on_log=None, should_stop=None,
           errors=None, pause=0.5):
    """Работодатели по вакансиям. region — номер субъекта («77») или пусто."""
    s = _session(session)
    log = on_log or (lambda *_: None)
    params = {"text": text, "limit": PER_PAGE}
    if period:
        since = datetime.datetime.utcnow() - datetime.timedelta(days=int(period))
        params["modifiedFrom"] = since.strftime("%Y-%m-%dT00:00:00Z")
    reg = region_param(region)
    found = {}
    base_ok = None
    for page in range(max(1, min(MAX_PAGES, int(pages or 1)))):
        if should_stop and should_stop():
            break
        params["offset"] = page
        data, err = None, ""
        for base in ([base_ok] if base_ok else BASES):
            url = base + ("/region/%s" % reg if reg else "")
            try:
                r = s.get(url, params=params, timeout=25)
            except requests.RequestException as e:
                err = "не отвечает (%s)" % type(e).__name__
                continue
            if r.status_code != 200:
                err = "ответил %d" % r.status_code
                continue
            try:
                data = r.json()
            except ValueError:
                err = "ответил не JSON"
                continue
            base_ok = base
            break
        if data is None:
            msg = "Работа России %s" % err
            log("   " + msg, "warn")
            if errors is not None:
                errors.append(msg)
            break
        rows, total = parse(data, text, in_title=in_title,
                            skip_agencies=skip_agencies)
        fold(rows, found)
        got = len(((data.get("results") or {}).get("vacancies") or [])
                  if isinstance(data.get("results"), dict) else [])
        if page == 0:
            log("   Работа России: вакансий по запросу %d" % total)
        if got < PER_PAGE or (page + 1) * PER_PAGE >= total:
            break
        time.sleep(pause)
    return list(found.values())
