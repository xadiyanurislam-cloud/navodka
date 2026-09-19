# -*- coding: utf-8 -*-
"""Оценка компании как лида — 0..100.

Смысл оценки не в точности, а в порядке обзвона. Список из тысячи
компаний обрабатывают сверху вниз и до середины обычно не доходят, так
что важно, кто окажется в первой сотне.

Веса подобраны по одному принципу: сильнее всего весит подтверждённая
боль (компания ищет продажников прямо сейчас) и подтверждённая
готовность платить за звонки (стоит коллтрекинг). Всё остальное —
уточнения.

Второе назначение модуля — объяснить сам балл. Число без разбора
человек либо принимает на веру, либо не верит ему вовсе, и оба исхода
одинаково бесполезны. Поэтому compute возвращает не только сумму, но и
список слагаемых: что засчитано, что нет и сколько стоит недостающее.
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
    "mail_verified": 15,     # почта руководителя отвечает на проверку
    "has_social": 8,         # есть куда написать помимо почты
}

# Размер, при котором сделка вообще возможна. Микробизнес не платит, у
# крупного закупки идут через тендер и службу безопасности — и то и другое
# одинаково бесполезно, но по названию компании неразличимо.
GOOD_SIZE = {"малый", "средний"}

# Короткое пояснение к каждому слагаемому: почему оно вообще считается.
# Живёт рядом с весами намеренно — иначе вес меняют, а объяснение к нему
# остаётся прежним и начинает врать.
WHY = {
    "vacancies_sales": "Нанимают продавцов — значит продажи буксуют "
                       "прямо сейчас. Признак живёт неделю, пока висит "
                       "вакансия, и его не купить ни в одной базе.",
    "calltracking": "Платят за подсчёт звонков — значит звонки для них "
                    "деньги, и разговор о них уже не надо начинать с нуля.",
    "telephony": "Своя телефония: звонки идут через систему, а не с мобильных.",
    "crm": "Есть CRM — есть куда встраиваться.",
    "chat": "Чат на сайте: собирают обращения не только по телефону.",
    "size_fit": "Микробизнес не платит, у крупного закупки идут через "
                "тендер и службу безопасности. По названию это неразличимо, "
                "по выручке — да.",
    "has_director": "Известно ФИО руководителя — есть кого спрашивать.",
    "has_site": "Есть сайт: с него берутся почты, телефоны и технографика.",
    "has_email": "Есть почта — есть куда написать.",
    "has_phone": "Есть телефон — есть куда позвонить.",
    "zakupki_contact": "Из карточки заказчика в госзакупках виден прямой "
                       "телефон ответственного лица.",
    "lpr_found": "Контакт первого лица найден в источнике, а не выведен "
                 "по схеме. По такому можно звонить.",
    "lpr_guessed": "Адрес руководителя выведен по схеме домена компании. "
                   "Это догадка: пробовать написать можно, рассчитывать — нет.",
    "mail_verified": "Почта руководителя прошла проверку по SMTP — ящик живой.",
    "has_social": "Есть сообщество или канал: туда пишут, когда на почту "
                  "не отвечают, а в группе ВК вдобавок видны контактные "
                  "лица, которых компания указала сама.",
}


def part(key, got, text, points=None):
    return {"key": key, "got": bool(got), "text": text,
            "points": WEIGHTS[key] if points is None else points,
            "why": WHY.get(key, "")}


def compute(company, signals, contacts):
    """company — строка БД, signals — {key: value}, contacts — список строк.

    Возвращает (балл, слагаемые). Слагаемые — и засчитанные, и нет:
    несделанное объясняет балл не хуже сделанного, а заодно показывает,
    чем его поднять.
    """
    score = 0
    parts = []

    def take(p):
        parts.append(p)
        if p["got"]:
            return p["points"]
        return 0

    vac = int(signals.get("hh_vacancies") or 0)
    if vac:
        # Три вакансии и больше — отдел растёт, а не затыкает одну дыру.
        full = vac >= 3
        pts = WEIGHTS["vacancies_sales"] if full else WEIGHTS["vacancies_sales"] // 2
        score += take(part("vacancies_sales", True,
                           "открытых вакансий в продажи: %d%s"
                           % (vac, "" if full else " — меньше трёх, половина веса"),
                           pts))
    else:
        score += take(part("vacancies_sales", False,
                           "вакансий в продажи не нашлось"))

    for key, label, nope in (
            ("calltracking", "коллтрекинг", "коллтрекинга на сайте нет"),
            ("telephony", "телефония", "своей телефонии не видно"),
            ("crm", "CRM", "следов CRM на сайте нет"),
            ("chat", "чат на сайте", "чата на сайте нет")):
        val = signals.get("tech_" + key)
        score += take(part(key, bool(val),
                           "%s: %s" % (label, val) if val else nope))

    size = signals.get("size") or ""
    if size in GOOD_SIZE:
        score += take(part("size_fit", True, "размер подходит: %s" % size))
    else:
        # Не штрафуем, а просто не даём баллов: микрофирма может оказаться
        # растущей, а данные ФНС отстают от жизни на год.
        score += take(part("size_fit", False,
                           "размер мимо: %s" % size if size
                           else "выручки в ФНС не нашлось — размер неизвестен"))

    score += take(part("zakupki_contact", bool(signals.get("zakupki_person")),
                       "контакт из закупок: %s" % signals["zakupki_person"]
                       if signals.get("zakupki_person")
                       else "в госзакупках компания не нашлась"))

    score += take(part("has_director", bool((company["director"] or "").strip()),
                       "руководитель: %s" % company["director"]
                       if (company["director"] or "").strip()
                       else "ФИО руководителя неизвестно"))
    score += take(part("has_site", bool((company["site"] or "").strip()),
                       "сайт: %s" % company["site"]
                       if (company["site"] or "").strip() else "сайта нет"))

    kinds = {c["kind"] for c in contacts}
    score += take(part("has_email", "email" in kinds,
                       "почта есть" if "email" in kinds else "почты нет"))
    score += take(part("has_phone", "phone" in kinds,
                       "телефон есть" if "phone" in kinds else "телефона нет"))
    socials = [c for c in contacts if c["kind"] == "social"]
    score += take(part("has_social", bool(socials),
                       "соцсетей: %d" % len(socials) if socials
                       else "соцсетей не нашлось"))

    # Контакт первого лица — то, ради чего всё затевалось. Найденный и
    # выведенный по схеме весят по-разному: по первому можно звонить, по
    # второму — только пробовать написать.
    lpr = signals.get("lpr_contact") or ""
    if lpr == "найден":
        score += take(part("lpr_found", True, "контакт ГД найден в источнике"))
    elif lpr == "выведен":
        score += take(part("lpr_guessed", True,
                           "адрес ГД выведен по схеме домена"))
        parts.append(part("lpr_found", False,
                          "найденного контакта ГД нет — только выведенный"))
    else:
        score += take(part("lpr_found", False, "контакт ГД не найден"))

    verified = any(c["kind"] == "email" and c["owner"] == "director"
                   and c["verified"] == "ok" for c in contacts)
    score += take(part("mail_verified", verified,
                       "почта руководителя подтверждена" if verified
                       else "почта руководителя не проверялась"))

    return min(100, score), parts


def legend():
    """Все слагаемые с весами — для справки, вне привязки к компании."""
    order = ("vacancies_sales", "calltracking", "lpr_found", "size_fit",
             "has_director", "zakupki_contact", "mail_verified", "telephony",
             "crm", "lpr_guessed", "has_social", "has_site", "has_email",
             "has_phone", "chat")
    return [{"key": k, "points": WEIGHTS[k], "why": WHY.get(k, "")}
            for k in order]
