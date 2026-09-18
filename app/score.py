# -*- coding: utf-8 -*-
"""Оценка компании как лида — 0..100.

Смысл оценки не в точности, а в порядке обзвона. Список из тысячи
компаний обрабатывают сверху вниз и до середины обычно не доходят, так
что важно, кто окажется в первой сотне.

Веса подобраны по одному принципу: сильнее всего весит подтверждённая
боль (компания ищет продажников прямо сейчас) и подтверждённая
готовность платить за звонки (стоит коллтрекинг). Всё остальное —
уточнения.
"""

WEIGHTS = {
    "vacancies_sales": 25,   # ищет продажников сейчас
    "calltracking": 25,      # уже платит за подсчёт звонков
    "telephony": 10,         # своя телефония
    "crm": 10,               # есть куда встраиваться
    "chat": 3,
    "has_director": 12,      # знаем ФИО ЛПР
    "has_site": 5,
    "has_email": 5,
    "has_phone": 5,
    "size_fit": 15,          # выручка в рабочем диапазоне
    "zakupki_contact": 12,   # прямой контакт ответственного лица
    "lpr_found": 25,         # контакт первого лица НАЙДЕН, а не выведен
    "lpr_guessed": 8,        # выведен по схеме — уже что-то, но догадка
}

# Размер, при котором сделка вообще возможна. Микробизнес не платит, у
# крупного закупки идут через тендер и службу безопасности — и то и другое
# одинаково бесполезно, но по названию компании неразличимо.
GOOD_SIZE = {"малый", "средний"}


def compute(company, signals, contacts):
    """company — строка БД, signals — {key: value}, contacts — список строк."""
    score = 0
    reasons = []

    vac = int(signals.get("hh_vacancies") or 0)
    if vac:
        # Три вакансии и больше — отдел растёт, а не затыкает одну дыру.
        add = WEIGHTS["vacancies_sales"] if vac >= 3 else WEIGHTS["vacancies_sales"] // 2
        score += add
        reasons.append("открытых вакансий в продажи: %d" % vac)

    for key in ("calltracking", "telephony", "crm", "chat"):
        val = signals.get("tech_" + key)
        if val:
            score += WEIGHTS[key]
            reasons.append("%s: %s" % (key, val))

    size = signals.get("size") or ""
    if size in GOOD_SIZE:
        score += WEIGHTS["size_fit"]
        reasons.append("размер подходит: %s" % size)
    elif size:
        # Не штрафуем, а просто не даём баллов: микрофирма может оказаться
        # растущей, а данные ФНС отстают от жизни на год.
        reasons.append("размер мимо: %s" % size)

    if signals.get("zakupki_person"):
        score += WEIGHTS["zakupki_contact"]
        reasons.append("контакт из закупок: %s" % signals["zakupki_person"])

    if (company["director"] or "").strip():
        score += WEIGHTS["has_director"]
        reasons.append("известен руководитель")
    if (company["site"] or "").strip():
        score += WEIGHTS["has_site"]

    kinds = {c["kind"] for c in contacts}
    if "email" in kinds:
        score += WEIGHTS["has_email"]
    if "phone" in kinds:
        score += WEIGHTS["has_phone"]

    # Контакт первого лица — то, ради чего всё затевалось. Найденный и
    # выведенный по схеме весят по-разному: по первому можно звонить, по
    # второму — только пробовать написать.
    lpr = signals.get("lpr_contact") or ""
    if lpr == "найден":
        score += WEIGHTS["lpr_found"]
        reasons.append("контакт ГД найден в источнике")
    elif lpr == "выведен":
        score += WEIGHTS["lpr_guessed"]
        reasons.append("адрес ГД выведен по схеме домена")

    # Подтверждённый адрес руководителя — то, ради чего всё затевалось.
    for c in contacts:
        if c["kind"] == "email" and c["owner"] == "director" and c["verified"] == "ok":
            score += 15
            reasons.append("почта руководителя подтверждена")
            break

    return min(100, score), reasons
