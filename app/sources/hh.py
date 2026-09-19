# -*- coding: utf-8 -*-
"""hh.ru — поиск компаний, которые прямо сейчас растят отдел продаж.

Это не просто список фирм, а список фирм с подтверждённой болью. Компания,
которая ищет третьего менеджера по продажам, уже уперлась в то, что
контролировать их некому. Такой признак не купишь ни в одной базе: он
живёт неделю, пока висит вакансия.

API открытый и бесплатный, ключ не нужен — только осмысленный User-Agent,
анонимные обращения hh режет.
"""
import re
import time


BASE = "https://api.hh.ru"

# Часто используемые регионы. Полный справочник — /areas, но держать его
# целиком в программе незачем: девять из десяти поисков идут по этим.
AREAS = [
    ("113", "Россия"), ("1", "Москва"), ("2", "Санкт-Петербург"),
    ("1202", "Новосибирск"), ("3", "Екатеринбург"), ("66", "Нижний Новгород"),
    ("88", "Казань"), ("76", "Ростов-на-Дону"), ("78", "Самара"),
    ("104", "Челябинск"), ("54", "Красноярск"), ("68", "Омск"),
    ("99", "Уфа"), ("1438", "Краснодар"), ("2019", "Московская область"),
]

# Запросы под тех, у кого есть телефонные продажи.
#
# Простые фразы без NOT, OR и кавычек — и это не упрощение, а исправление.
# hh разбирает язык запросов строго: на «менеджер по продажам NOT
# продавец-консультант» он отвечает 400, поиск обрывается на первой же
# странице, и человек видит «ничего не нашлось» при работающем интернете.
PRESETS = {
    "Отдел продаж": "менеджер по продажам",
    "Колл-центр": "оператор колл-центра",
    "Руководитель продаж": "руководитель отдела продаж",
    "Клиентский сервис": "менеджер по работе с клиентами",
    "Телемаркетинг": "телемаркетолог",
}


# Кадровые агентства и аутстафферы.
#
# Формально это компании с вакансиями в продажах, и в выдачу они попадают
# первыми: вакансий у них сотни. Толку от них ноль — продавать им нечего,
# а до руководителя не дойти, там своя воронка. Отсекаем по названию: это
# грубо, зато не требует лишнего запроса на каждую компанию.
AGENCY_WORDS = (
    "кадров", "рекрут", "recruit", "hr-", "hr ", "аутстаф", "аутсорс",
    "персонал", "подбор персонала", "стафф", "staff", "hh.ru", "работа.ру",
    "агентство занятости", "трудовые ресурсы", "консалтинг персонала",
)


def looks_like_agency(name):
    """Похоже ли название на кадровое агентство."""
    low = " " + (name or "").lower().replace("«", " ").replace("»", " ") + " "
    return any(w in low for w in AGENCY_WORDS)


def days_since(stamp):
    """Сколько дней назад опубликована вакансия.

    Свежесть — это и есть ценность признака. Вакансия, вывешенная вчера,
    означает, что решение о найме приняли на этой неделе; та же вакансия
    месячной давности означает, что её, скорее всего, уже закрыли.
    """
    if not stamp:
        return None
    try:
        t = time.strptime((stamp or "")[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None
    return max(0, int((time.time() - time.mktime(t)) / 86400))


# Как программа ходит в hh.
#
# Раньше здесь перебирались варианты User-Agent, и это не помогло: 403
# приходил на любую подпись. Значит, отсекают раньше — по отпечатку
# TLS-рукопожатия, который у requests свой и ни на один браузер не похож.
# Заголовки этого не меняют. Поэтому перебираются не подписи, а способы
# связи целиком — см. app/net.py.
#
# Сработавший способ запоминается на время работы программы: перебирать
# его заново на каждой из двадцати страниц выдачи бессмысленно и медленно.
_working = {"transport": None}


def _session(ua=None):
    """Сессия для точечных запросов вроде карточки работодателя."""
    t = _working["transport"]
    if t is not None:
        return t.session
    from .. import net
    return net.hh_transports()[0].session


# Параметры, без которых поиск всё равно работает.
#
# hh отвечает 400 на любой незнакомый ему параметр целиком, а не молча его
# игнорирует. Список нужен, чтобы после такого отказа повторить запрос без
# необязательного — и отдать человеку пусть менее точную, но выдачу.
OPTIONAL_PARAMS = ("search_field", "label", "order_by")


def _request(s, url, params, on_log, session_factory=None):
    """Запрос с перебором способов связи. Возвращает (ответ, ошибка).

    Перебор идёт только пока способ не найден. Дальше используется он же,
    и если он вдруг перестал работать — перебор начинается снова.
    """
    from .. import net

    if session_factory is not None:          # в тестах сеть не трогаем
        try:
            return s.get(url, params=params, timeout=20), ""
        except Exception as e:
            return None, "hh.ru недоступен: %s" % e

    tried, last = [], ""
    order = ([_working["transport"]] if _working["transport"] else []) + \
        [t for t in net.hh_transports() if t is not _working["transport"]]

    for t in order:
        try:
            r = t.get(url, params=params, timeout=20)
        except Exception as e:
            tried.append("%s — не дозвонился (%s)" % (t.name, str(e)[:70]))
            continue
        if r.status_code == 403:
            tried.append("%s — 403" % t.name)
            last = (getattr(r, "text", "") or "")[:120]
            if _working["transport"] is t:
                _working["transport"] = None      # способ протух, ищем заново
            continue
        if _working["transport"] is not t:
            _working["transport"] = t
            if on_log:
                on_log("Связь с hh установлена: %s" % t.name)
        return r, ""

    detail = "; ".join(tried)
    return None, ("hh.ru отклонил все способы обращения (%s). %s Ответ hh: %s"
                  % (detail, net.missing_note(), last))


def search_employers(text, area="113", period=30, pages=5, per_page=100,
                     pause=0.4, on_log=None, should_stop=None,
                     session_factory=None, errors=None,
                     in_title=True, skip_agencies=True):
    """Ищет вакансии и сворачивает их до работодателей.

    Возвращает список словарей: id, name, vacancies (сколько открытых
    вакансий по этому запросу), area, первая найденная вакансия.

    in_title — искать фразу в названии вакансии, а не по всему тексту.
    Разница огромная: «менеджер по продажам» встречается в описании почти
    любой вакансии («подчиняется менеджеру по продажам», «взаимодействие с
    отделом продаж»), и поиск по всему тексту приносит бухгалтеров и
    курьеров вперемешку с теми, кто действительно нужен.
    """
    # session_factory нужен тестам: проверять ограничения API, ходя в сеть,
    # значит получить красный прогон в первый же день без интернета.
    s = (session_factory or _session)()   # при переборе не используется
    found, agencies = {}, set()
    # Ограничения API, за которые нельзя выходить: период больше 30 дней
    # и выдача глубже двух тысяч позиций — это 400, а не пустой ответ.
    period = max(1, min(30, int(period or 30)))
    per_page = max(1, min(100, int(per_page or 100)))
    pages = max(1, min(2000 // per_page, int(pages or 5)))
    for page in range(pages):
        if should_stop and should_stop():
            break
        params = {"text": text, "area": area, "period": period,
                  "per_page": per_page, "page": page,
                  "order_by": "publication_time"}
        if in_title:
            params["search_field"] = "name"
        r, err = _request(s, BASE + "/vacancies", params, on_log, session_factory)
        if err:
            if on_log:
                on_log(err, "error")
            if errors is not None:
                errors.append(err)
            break
        if r.status_code == 400 and any(k in params for k in OPTIONAL_PARAMS):
            # Прежде чем сдаваться, пробуем то же самое без украшений.
            # Пустая выдача из-за параметра, без которого можно обойтись, —
            # это наша ошибка, а не отсутствие компаний.
            simple = {k: v for k, v in params.items() if k not in OPTIONAL_PARAMS}
            if on_log:
                on_log("hh не принял уточнение запроса — повторяю проще, "
                       "выдача будет шире", "warn")
            r2, err2 = _request(s, BASE + "/vacancies", simple, on_log, session_factory)
            if not err2 and r2 is not None and r2.status_code == 200:
                r, in_title = r2, False
        if r.status_code != 200:
            # Тело ответа hh объясняет отказ куда точнее кода: там прямо
            # написано, какой параметр он не принял.
            msg = "hh.ru ответил %s: %s" % (r.status_code, r.text[:300])
            if on_log:
                on_log(msg, "error")
                if r.status_code == 400:
                    on_log("Скорее всего, дело в запросе. Попробуйте простую "
                           "фразу без кавычек, NOT и OR — например, "
                           "«менеджер по продажам».", "warn")
            if errors is not None:
                errors.append(msg)
            break
        try:
            data = r.json()
        except Exception as e:
            if on_log:
                on_log("hh.ru вернул не JSON: %s" % e, "error")
            break

        if page == 0 and on_log:
            on_log("hh.ru нашёл вакансий по запросу: %s" % data.get("found", "?"))
        items = data.get("items") or []
        for v in items:
            emp = v.get("employer") or {}
            eid = str(emp.get("id") or "")
            # Вакансии без работодателя — это анонимные объявления кадровых
            # агентств. Компании за ними не видно, и лид из них не сделать.
            if not eid or not emp.get("name"):
                continue
            if skip_agencies and looks_like_agency(emp.get("name")):
                agencies.add(emp.get("name"))
                continue
            row = found.setdefault(eid, {
                "id": eid, "name": emp.get("name"), "vacancies": 0,
                "area": (v.get("area") or {}).get("name", ""),
                "vacancy_url": v.get("alternate_url", ""),
                "vacancy_name": v.get("name", ""),
                "titles": [], "salaries": [], "fresh": None,
                # Номера вакансий нужны там, где прогон идёт по
                # нескольким запросам: «Руководитель отдела продаж»
                # находится и по «отдел продаж», и по «руководитель
                # продаж», и без номеров одна вакансия считалась бы
                # дважды.
                "vac_ids": [],
            })
            # Самая свежая вакансия компании: по ней видно, насколько
            # горячий признак.
            d = days_since(v.get("published_at") or v.get("created_at"))
            if d is not None and (row["fresh"] is None or d < row["fresh"]):
                row["fresh"] = d
            vid = str(v.get("id") or "")
            if vid and vid not in row["vac_ids"]:
                row["vac_ids"].append(vid)
            row["vacancies"] += 1
            # Названия вакансий — это описание отдела своими словами.
            # «Оператор колл-центра» говорит о телефонных продажах прямее
            # любого счётчика на сайте.
            title = (v.get("name") or "").strip()
            if title and title not in row["titles"]:
                row["titles"].append(title[:120])
            # Зарплатная вилка — косвенно уровень компании и её отдела.
            # Продажник за 40 тысяч и за 150 работают в разных мирах, и
            # разговор с их руководителями строится по-разному.
            sal = v.get("salary") or {}
            if (sal.get("currency") or "RUR") == "RUR":
                for key in ("from", "to"):
                    if sal.get(key):
                        row["salaries"].append(int(sal[key]))

        if on_log:
            on_log("hh: страница %d — вакансий %d, компаний всего %d"
                   % (page + 1, len(items), len(found)))
        if page + 1 >= (data.get("pages") or 0):
            break
        time.sleep(pause)

    if agencies and on_log:
        on_log("Пропущено кадровых агентств: %d (%s)"
               % (len(agencies), ", ".join(sorted(agencies)[:3])))
    return sorted(found.values(), key=lambda x: -x["vacancies"])


def search_employers_by_text(text, area="113", pages=3, per_page=50, pause=0.4,
                             on_log=None, should_stop=None, errors=None,
                             session_factory=None, skip_agencies=True):
    """Работодатели, у которых искомое слово в названии или в сфере.

    Отдельный от вакансий способ. «Стоматология» вакансий может не
    публиковать вовсе, но карточка работодателя у неё есть, и в ней —
    сайт, отрасль и город, то есть всё, с чего начинается обогащение.
    Вакансии тут не нужны: ищем не тех, кто нанимает, а тех, кто есть.
    """
    s = (session_factory or _session)()
    pages = max(1, min(20, int(pages or 3)))
    per_page = max(1, min(100, int(per_page or 50)))
    found, agencies = {}, set()
    for page in range(pages):
        if should_stop and should_stop():
            break
        params = {"text": text, "area": area, "only_with_vacancies": "false",
                  "per_page": per_page, "page": page}
        r, err = _request(s, BASE + "/employers", params, on_log, session_factory)
        if err:
            if on_log:
                on_log(err, "error")
            if errors is not None:
                errors.append(err)
            break
        if r.status_code != 200:
            msg = "hh.ru ответил %s: %s" % (r.status_code, (r.text or "")[:300])
            if on_log:
                on_log(msg, "error")
            if errors is not None:
                errors.append(msg)
            break
        try:
            data = r.json()
        except Exception:
            break
        items = data.get("items") or []
        if page == 0 and on_log:
            on_log("hh.ru знает работодателей по запросу: %s" % data.get("found", "?"))
        for e in items:
            eid, name = str(e.get("id") or ""), (e.get("name") or "").strip()
            if not eid or not name or eid in found:
                continue
            if skip_agencies and looks_like_agency(name):
                agencies.add(name)
                continue
            found[eid] = {
                "id": eid, "name": name,
                "area": (e.get("area") or {}).get("name", ""),
                "open_vacancies": e.get("open_vacancies") or 0,
                "vacancies": 0, "titles": [], "salaries": [], "fresh": None,
            }
        if on_log:
            on_log("hh: страница %d — работодателей %d, всего %d"
                   % (page + 1, len(items), len(found)))
        if page + 1 >= (data.get("pages") or 0):
            break
        time.sleep(pause)
    if agencies and on_log:
        on_log("Пропущено кадровых агентств: %d" % len(agencies))
    return list(found.values())


def employer_details(employer_id, session=None, timeout=20, ua=None):
    """Карточка работодателя: сайт и отрасль.

    Сайт здесь — главное. В ЕГРЮЛ его нет, а без сайта не получить ни почт,
    ни технографики, то есть половина работы дальше не делается.
    """
    s = session or _session(ua)
    try:
        r = s.get("%s/employers/%s" % (BASE, employer_id), timeout=timeout)
        r.raise_for_status()
        d = r.json()
    except Exception:
        return {}
    # Описание работодателя — HTML из карточки. Разметку срезаем: в
    # выгрузку и в интерфейс нужен текст, а не вёрстка чужого редактора.
    about = re.sub(r"<[^>]+>", " ", d.get("description") or "")
    about = re.sub(r"\s+", " ", about.replace("&nbsp;", " ")).strip()
    return {
        "name": d.get("name") or "",
        "site": d.get("site_url") or "",
        "about": about[:600],
        "area": (d.get("area") or {}).get("name", ""),
        "industries": ", ".join(i.get("name", "") for i in (d.get("industries") or [])
                              if isinstance(i, dict)),
        # Все открытые вакансии компании, а не только по нашему запросу:
        # по ним видно, растёт ли она вообще или нанимает точечно.
        "open_vacancies": d.get("open_vacancies") or 0,
        "hh_url": d.get("alternate_url") or "",
    }
