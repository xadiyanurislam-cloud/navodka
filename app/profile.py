# -*- coding: utf-8 -*-
"""Профиль компании: чем занимается и есть ли телефонные продажи.

Второй вопрос важнее первого. «Есть ли колл-центр» напрямую не написано
нигде, но компания, которая продаёт по телефону, оставляет следы: платит
за бесплатный номер, ставит коллтрекинг, обещает перезвонить, нанимает
операторов. По отдельности каждый след слабый, вместе — надёжный.

Поэтому здесь не булев ответ, а вердикт с перечнем того, на чём он
основан. Продавец должен видеть, почему программа так решила: «есть
8-800 и вакансия оператора» — довод, а голое «да» — нет.
"""
import re

# Признак → (вес, как показать). Веса подобраны по стоимости признака для
# самой компании: за 8-800 и коллтрекинг платят деньгами каждый месяц, и
# ради двух звонков в неделю их не заводят. Виджет чата бесплатен и
# говорит куда меньше.
SIGNS = {
    "tollfree":     (30, "бесплатный номер 8-800"),
    "calltracking": (28, "коллтрекинг"),
    "operators":    (26, "нанимают операторов"),
    "telephony":    (16, "корпоративная телефония"),
    "sales_hiring": (14, "нанимают в продажи"),
    "callback":     (12, "обещают перезвонить"),
    "many_phones":  (10, "несколько номеров"),
    "hours":        (8, "расширенный график"),
    "chat":         (4, "онлайн-чат"),
}

# Слова в названиях вакансий, которые прямо говорят о работе на телефоне.
OPERATOR_WORDS = ("колл-центр", "call-центр", "call центр", "колл центр",
                  "оператор на телефон", "телемаркет", "телефонных продаж",
                  "оператор пк со знанием", "диспетчер на телефон",
                  "специалист контакт-центра", "контакт-центр")

SALES_WORDS = ("менеджер по продажам", "продаж", "руководитель отдела продаж",
               "роп", "менеджер по работе с клиентами")


def call_signals(site_data, vacancy_titles=(), phones=()):
    """Собрать признаки телефонных продаж из того, что уже нашли."""
    site_data = site_data or {}
    tech = site_data.get("tech") or {}
    titles_low = " | ".join(t.lower() for t in (vacancy_titles or []))

    found = {}
    if site_data.get("tollfree"):
        found["tollfree"] = ", ".join(site_data["tollfree"][:2])
    if tech.get("calltracking"):
        found["calltracking"] = ", ".join(tech["calltracking"])
    if tech.get("telephony"):
        found["telephony"] = ", ".join(tech["telephony"])
    if tech.get("chat"):
        found["chat"] = ", ".join(tech["chat"])
    if site_data.get("callback"):
        found["callback"] = "форма заказа звонка"
    if site_data.get("hours"):
        found["hours"] = site_data["hours"]
    if len(phones or ()) >= 3:
        found["many_phones"] = "%d номеров" % len(phones)
    if any(w in titles_low for w in OPERATOR_WORDS):
        found["operators"] = "вакансия оператора"
    elif any(w in titles_low for w in SALES_WORDS):
        found["sales_hiring"] = "вакансии в продажи"
    return found


def verdict(found):
    """Вердикт и его обоснование.

    Пороги выставлены по смыслу, а не по круглому числу. «Да» начинается
    там, где сошлись два платных признака: 8-800 и коллтрекинг вместе — это
    компания, чей бизнес держится на входящих звонках, и платит она за это
    каждый месяц. «Вероятно» — один платный или несколько бесплатных.
    Ниже уверенно сказать нечего: сайт с онлайн-чатом и графиком работы
    есть у любой конторы, включая ту, куда никто не звонит.
    """
    score = sum(SIGNS[k][0] for k in found if k in SIGNS)
    why = [SIGNS[k][1] for k in sorted(found, key=lambda k: -SIGNS.get(k, (0,))[0])
           if k in SIGNS]
    if score >= 55:
        label = "да"
    elif score >= 35:
        label = "вероятно"
    elif score > 0:
        label = "слабые признаки"
    else:
        label = "нет данных"
    return {"label": label, "score": min(100, score), "why": why}


def activity(company, signals, site_data=None):
    """Чем занимается — одной строкой, из самого надёжного источника.

    Порядок неслучаен: описание с сайта компания писала о себе сама, ОКВЭД
    выбирали при регистрации и часто формально, отрасль с hh ставят кадровики.
    """
    site_data = site_data or {}
    for candidate in (site_data.get("description"),
                      signals.get("hh_about"),
                      company.get("okved_name") if isinstance(company, dict)
                      else company["okved_name"],
                      site_data.get("title")):
        text = (candidate or "").strip()
        if len(text) >= 25:
            return re.sub(r"\s+", " ", text)[:400]
    return ""
