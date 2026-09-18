# -*- coding: utf-8 -*-
"""ЕИС (госзакупки) — прямые контакты ответственных лиц.

Самый недооценённый источник. В карточке организации-заказчика ЕИС
публикует контактное лицо с телефоном и почтой — это не общий ящик с
сайта, а живой рабочий контакт конкретного человека. Данные открыты по
закону о контрактной системе.

Разбор идёт по HTML, потому что публичного JSON у ЕИС нет. Вёрстку там
меняют, поэтому все выборки написаны терпимо: не нашли — вернули пусто,
а не уронили обход. Если источник перестал отдавать контакты, это видно
по нулю находок, и чинится одним регулярным выражением здесь.
"""
import re

import requests

from .. import settings

SEARCH = "https://zakupki.gov.ru/epz/organization/search/results.html"
CARD = "https://zakupki.gov.ru/epz/organization/view/info.html"

_ORG_ID = re.compile(r"organizationId=(\d+)")
_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")

# Подписи полей в карточке. Берём значение из блока, следующего за подписью:
# конкретные классы вёрстки меняются, а сами подписи — нет.
_FIELDS = {
    "person": ("Контактное лицо", "Ответственное должностное лицо"),
    "phone": ("Телефон",),
    "email": ("Электронная почта", "Адрес электронной почты"),
}


def _session(session=None):
    s = session or requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT,
                      "Accept-Language": "ru"})
    return s


def _strip_tags(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", html)
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&amp;", "&")
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _value_after(lines, labels, max_gap=4):
    for i, line in enumerate(lines):
        for label in labels:
            if line.startswith(label):
                # Значение лежит в одной из ближайших строк после подписи.
                # Перебираем несколько: между ними попадается разметочный мусор.
                for j in range(i + 1, min(i + 1 + max_gap, len(lines))):
                    val = lines[j]
                    if val and not val.endswith(":") and len(val) < 200:
                        return val
    return ""


def find_org_id(inn, session=None, timeout=20):
    inn = (inn or "").strip()
    if not inn.isdigit():
        return ""
    s = _session(session)
    try:
        r = s.get(SEARCH, params={"searchString": inn, "pageNumber": 1}, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    m = _ORG_ID.search(r.text)
    return m.group(1) if m else ""


def contacts_by_inn(inn, session=None, timeout=20):
    """Контактное лицо заказчика: ФИО, телефон, почта.

    Пусто — нормальный ответ: в закупках участвует далеко не каждая
    компания, и отсутствие карточки ничего плохого про лид не говорит.
    """
    org_id = find_org_id(inn, session=session, timeout=timeout)
    if not org_id:
        return {}
    s = _session(session)
    try:
        r = s.get(CARD, params={"organizationId": org_id}, timeout=timeout)
        r.raise_for_status()
        html = r.text
    except Exception:
        return {}

    lines = _strip_tags(html)
    person = _value_after(lines, _FIELDS["person"])
    phone = _value_after(lines, _FIELDS["phone"])
    email = _value_after(lines, _FIELDS["email"])

    # Запасной путь: если подписи не нашлись (переверстали), берём первые
    # осмысленные почту и телефон со страницы карточки.
    if not email:
        found = [e for e in _EMAIL.findall(html) if "zakupki.gov.ru" not in e]
        email = found[0].lower() if found else ""
    if not phone:
        found = _PHONE.findall(html)
        phone = found[0] if found else ""

    if not (person or phone or email):
        return {}
    return {"org_id": org_id, "person": person[:150],
            "phone": phone[:40], "email": email.lower()[:120],
            "url": "%s?organizationId=%s" % (CARD, org_id)}
