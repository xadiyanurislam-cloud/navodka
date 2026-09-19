# -*- coding: utf-8 -*-
"""Из ФИО руководителя и почтового домена компании — кандидаты в адрес.

ФИО директора лежит в ЕГРЮЛ и доступно всем, а его рабочий адрес на сайте
почти никогда не публикуют. Зато публикуют адреса менеджеров — и по ним
видно, как в компании устроены почтовые ящики. Если менеджер живёт по
a.petrov@, то директор почти наверняка по той же схеме.

Метод грубый и честен в этом: он даёт список кандидатов с оценкой, а не
готовый ответ. Проверять их всё равно надо отдельно (см. verify.py).
"""
import re

# Общие ящики. Их надо выкинуть до разбора схемы: info@ и sales@ есть у
# всех, и по ним про схему именования нельзя сказать ничего.
GENERIC = {
    "info", "mail", "office", "sales", "shop", "zakaz", "order", "orders",
    "support", "help", "admin", "hello", "contact", "contacts", "reklama",
    "marketing", "hr", "job", "jobs", "career", "buh", "buhgalteria",
    "secretary", "director", "boss", "post", "client", "clients", "manager",
    "noreply", "no-reply", "robot", "sklad", "opt", "service", "servis",
}

# Транслитерация под то, как её делают в почтовых адресах: без диакритики,
# «щ» как sch, «ё» как e. Это не ГОСТ — это то, что реально встречается.
_TRANS = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Частые фамилии на -х и -ц пишут и через kh/ts, и через h/c. Второй вариант
# даём отдельным кандидатом: половина Михайловых сидит на mihaylov@.
_TRANS_ALT = dict(_TRANS, **{"х": "h", "ц": "c", "й": "i"})


def translit(text, alt=False):
    table = _TRANS_ALT if alt else _TRANS
    out = []
    for ch in (text or "").lower():
        if ch in table:
            out.append(table[ch])
        elif ch.isalnum():
            out.append(ch)
    return "".join(out)


def split_fio(fio):
    """«Иванов Иван Иванович» → ('ivanov', 'ivan', 'ivanovich').

    В ЕГРЮЛ порядок всегда «фамилия имя отчество», поэтому разбор по позиции,
    а не по словарю имён: словарь промахнётся на первом же нерусском ФИО.
    """
    parts = [p for p in re.split(r"[\s,]+", (fio or "").strip()) if p]
    if not parts:
        return None
    last = parts[0]
    first = parts[1] if len(parts) > 1 else ""
    middle = parts[2] if len(parts) > 2 else ""
    return last, first, middle


def shapes_from(emails):
    """Какие схемы именования встречаются в известных адресах домена."""
    found = set()
    for addr in emails:
        local = (addr or "").split("@")[0].lower().strip()
        if not local or local in GENERIC:
            continue
        if re.fullmatch(r"[a-z]\.[a-z]{2,}", local):
            found.add("i.last")
        elif re.fullmatch(r"[a-z]{2,}\.[a-z]\b", local):
            found.add("last.i")
        elif re.fullmatch(r"[a-z]{2,}\.[a-z]{2,}", local):
            found.add("first.last")
        elif re.fullmatch(r"[a-z]{4,}", local):
            found.add("last")
        elif re.fullmatch(r"[a-z]{5,}", local):
            found.add("ilast")
    return found


# Порядок — это и есть частотность: по нашей выборке чаще всего встречается
# голая фамилия, затем инициал с точкой. Если схема домена распознана, она
# поднимается наверх независимо от этого порядка.
_ORDER = ["last", "i.last", "ilast", "first.last", "last.i", "first",
          "lastfi", "firstlast"]


def candidates(fio, domain, known_emails=()):
    """Список кандидатов: [(адрес, уверенность 0..100), ...]."""
    fio_parts = split_fio(fio)
    if not fio_parts or not domain:
        return []
    known = set(e.lower() for e in known_emails)
    shapes = shapes_from(known)

    out, seen = [], set()
    for alt in (False, True):
        last, first, middle = (translit(p, alt) for p in fio_parts)
        if not last:
            continue
        fi = first[:1]
        mi = middle[:1]
        variants = {
            "last":       last,
            "i.last":     "%s.%s" % (fi, last) if fi else None,
            "ilast":      "%s%s" % (fi, last) if fi else None,
            "first.last": "%s.%s" % (first, last) if first else None,
            "last.i":     "%s.%s" % (last, fi) if fi else None,
            "first":      first or None,
            "lastfi":     "%s%s%s" % (last, fi, mi) if fi and mi else None,
            "firstlast":  "%s%s" % (first, last) if first else None,
        }
        for shape in _ORDER:
            local = variants.get(shape)
            if not local:
                continue
            addr = "%s@%s" % (local, domain)
            if addr in seen or addr in known:
                continue
            seen.add(addr)
            # Схема, подтверждённая живыми адресами домена, весит заметно
            # больше: это уже не догадка, а перенос известного правила.
            conf = 75 if shape in shapes else 45 - _ORDER.index(shape) * 3
            if alt:
                conf -= 10          # запасная транслитерация всегда слабее
            out.append((addr, max(10, conf)))

    out.sort(key=lambda x: -x[1])
    return out[:12]


def surname_forms(fio):
    """Написания фамилии, по которым её можно узнать в адресе.

    Обе транслитерации плюс сама кириллица: на сайтах встречается и
    mihailov@, и mikhaylov@, а в тексте страницы — «Михайлов».
    """
    parts = split_fio(fio)
    if not parts:
        return set()
    last = parts[0]
    forms = {translit(last), translit(last, alt=True)}
    return {f for f in forms if len(f) >= 4}


def same_person(a, b):
    """Один ли это человек. Сравниваем по фамилии и имени.

    Отчество не требуем: в ЕГРЮЛ пишут «Иванов Иван Иванович», на сайте —
    «Иван Иванов», в контактах группы — «Иванов Иван». Требовать полного
    совпадения строк значит не находить никогда. Но и одной фамилии мало:
    Иванов Иван и Иванов Пётр — разные люди, и звонить второму вместо
    первого хуже, чем не звонить вовсе.
    """
    pa, pb = _name_parts(a), _name_parts(b)
    if len(pa) < 2 or len(pb) < 2:
        return False
    # Порядок слов разный, поэтому сравниваем множества: фамилия и имя
    # должны найтись оба, кто из них первый — неважно.
    return len(pa & pb) >= 2


def _name_parts(fio):
    """Слова имени в нижнем регистре, без инициалов и коротких обрывков."""
    import re as _re
    words = _re.split(r"[\s.,]+", (fio or "").strip().lower())
    return {w for w in words if len(w) >= 3}


def match_emails(fio, emails):
    """Адреса с сайта, которые принадлежат руководителю.

    Это принципиально другая находка, чем вывод по схеме: здесь адрес
    существует на сайте компании, и мы лишь опознали, чей он. Промахнуться
    можно только на однофамильце в том же домене — случай редкий и
    нестрашный, потому что письмо всё равно уйдёт нужной компании.
    """
    forms = surname_forms(fio)
    if not forms:
        return []
    out = []
    for addr in emails:
        local = (addr or "").split("@")[0].lower()
        if local in GENERIC:
            continue
        for f in forms:
            # Фамилия целиком в локальной части: ivanov@, i.ivanov@,
            # ivanov.i@, ivan.ivanov@ — все сюда попадают.
            if f in local:
                out.append(addr)
                break
    return out
