# -*- coding: utf-8 -*-
"""OpenStreetMap — справочник организаций без ключей и без денег.

Зачем понадобился. 2ГИС и Яндекс отдают больше и точнее, но оба требуют
регистрации с ИНН и реквизитами, а у Яндекса поиск по организациям стоит
двадцать тысяч в месяц. Для программы, которой человек хочет
воспользоваться сегодня вечером, это стена.

OSM — открытая карта, которую ведут люди. Ключей нет, регистрации нет,
лимитов по деньгам нет. Данные беднее: где-то не указан телефон, где-то
сайт, названия бывают написаны как попало. Зато их можно взять прямо
сейчас, и по крупным городам их много.

Отдельная ценность: в OSM есть тег contact:vk — ссылка на сообщество
ВКонтакте. Это ровно то, по чему программа потом выходит на контактных
лиц компании, то есть на живого руководителя.

Запросы идут к Overpass API — открытому поисковику по данным OSM.
Зеркал несколько: они бесплатные, иногда перегружены, и при отказе
одного пробуем следующее.
"""
import re
import time

import requests

from .. import settings

# Зеркала Overpass. Порядок случайным не является: первое обычно самое
# быстрое, остальные — на случай, когда оно занято чужими запросами.
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Вид деятельности словами — в теги OSM.
#
# Список намеренно короткий: он покрывает то, что ищут чаще всего, а всё
# остальное всё равно находится поиском по названию. Гнаться за полнотой
# здесь бессмысленно — теги OSM исчисляются тысячами, а бизнес называет
# себя как хочет.
TAGS = {
    "стоматолог": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
    "клиник": ['["amenity"="clinic"]', '["amenity"="doctors"]'],
    "медицин": ['["amenity"="clinic"]', '["amenity"="doctors"]'],
    "медцентр": ['["amenity"="clinic"]', '["amenity"="doctors"]'],
    "лаборатор": ['["healthcare"="laboratory"]'],
    "автосервис": ['["shop"="car_repair"]', '["shop"="car_parts"]'],
    "автосалон": ['["shop"="car"]'],
    "автомойк": ['["amenity"="car_wash"]'],
    "автошкол": ['["amenity"="driving_school"]'],
    "шиномонтаж": ['["shop"="tyres"]'],
    "запчаст": ['["shop"="car_parts"]'],
    "азс": ['["amenity"="fuel"]'],
    "парикмахер": ['["shop"="hairdresser"]'],
    "барбершоп": ['["shop"="hairdresser"]'],
    "салон красоты": ['["shop"="beauty"]'],
    "космето": ['["shop"="beauty"]'],
    "массаж": ['["shop"="massage"]'],
    "фитнес": ['["leisure"="fitness_centre"]'],
    "спортзал": ['["leisure"="fitness_centre"]'],
    "юрид": ['["office"="lawyer"]'],
    "адвокат": ['["office"="lawyer"]'],
    "нотариус": ['["office"="notary"]'],
    "бухгалтер": ['["office"="accountant"]'],
    "аудит": ['["office"="accountant"]'],
    "страхов": ['["office"="insurance"]'],
    "турист": ['["shop"="travel_agency"]'],
    "турагент": ['["shop"="travel_agency"]'],
    "недвижимост": ['["office"="estate_agent"]'],
    "риэлт": ['["office"="estate_agent"]'],
    "кадров": ['["office"="employment_agency"]'],
    "рекрут": ['["office"="employment_agency"]'],
    "реклам": ['["office"="advertising_agency"]'],
    "маркетинг": ['["office"="advertising_agency"]'],
    "ветеринар": ['["amenity"="veterinary"]'],
    "аптек": ['["amenity"="pharmacy"]'],
    "оптик": ['["shop"="optician"]'],
    "типограф": ['["shop"="copyshop"]', '["craft"="printer"]'],
    "полиграф": ['["shop"="copyshop"]', '["craft"="printer"]'],
    "мебел": ['["shop"="furniture"]', '["craft"="carpenter"]'],
    "кух": ['["shop"="kitchen"]'],
    "окн": ['["shop"="windows"]', '["craft"="window_construction"]'],
    "двер": ['["shop"="doors"]'],
    "потолк": ['["craft"="plasterer"]'],
    "ремонт квартир": ['["craft"="builder"]', '["shop"="doityourself"]'],
    "отделк": ['["craft"="builder"]'],
    "строительн": ['["office"="construction_company"]', '["craft"="builder"]'],
    "стройматериал": ['["shop"="doityourself"]', '["shop"="hardware"]'],
    "сантехник": ['["craft"="plumber"]'],
    "электрик": ['["craft"="electrician"]'],
    "кондиционер": ['["craft"="hvac"]'],
    "вентиляц": ['["craft"="hvac"]'],
    "кафе": ['["amenity"="cafe"]'],
    "ресторан": ['["amenity"="restaurant"]'],
    "пекарн": ['["shop"="bakery"]'],
    "кондитер": ['["shop"="confectionery"]'],
    "гостиниц": ['["tourism"="hotel"]'],
    "отел": ['["tourism"="hotel"]'],
    "хостел": ['["tourism"="hostel"]'],
    "банк": ['["amenity"="bank"]'],
    "логист": ['["office"="logistics"]'],
    "грузоперевоз": ['["office"="logistics"]', '["office"="moving_company"]'],
    "перевозк": ['["office"="logistics"]', '["office"="moving_company"]'],
    "переезд": ['["office"="moving_company"]'],
    "транспортн": ['["office"="logistics"]'],
    "школ": ['["amenity"="school"]', '["amenity"="language_school"]'],
    "курсы": ['["amenity"="language_school"]', '["office"="educational_institution"]'],
    "учебный центр": ['["office"="educational_institution"]'],
    "детский сад": ['["amenity"="kindergarten"]'],
    "ит-компан": ['["office"="it"]'],
    "айти": ['["office"="it"]'],
    "разработка по": ['["office"="it"]'],
    "компьютер": ['["shop"="computer"]'],
    "химчист": ['["shop"="dry_cleaning"]'],
    "прачечн": ['["shop"="laundry"]'],
    "клининг": ['["office"="cleaning"]', '["craft"="cleaning"]'],
    "уборк": ['["office"="cleaning"]', '["craft"="cleaning"]'],
    "ритуальн": ['["shop"="funeral_directors"]'],
    "ювелир": ['["shop"="jewelry"]'],
    "цветочн": ['["shop"="florist"]'],
    "цветы": ['["shop"="florist"]'],
    "салон связи": ['["shop"="mobile_phone"]'],
    "одежд": ['["shop"="clothes"]'],
    "продукт": ['["shop"="convenience"]', '["shop"="supermarket"]'],
    "супермаркет": ['["shop"="supermarket"]'],
}

# Теги OSM по-русски. В карточку и в выгрузку должно попадать
# «Агентство недвижимости», а не estate_agent: список читает продавец, а
# не картограф.
RUBRIC_RU = {
    "dentist": "Стоматология", "clinic": "Клиника", "doctors": "Медцентр",
    "hospital": "Больница", "pharmacy": "Аптека", "veterinary": "Ветклиника",
    "car_repair": "Автосервис", "car": "Автосалон", "tyres": "Шиномонтаж",
    "car_parts": "Автозапчасти", "fuel": "АЗС",
    "hairdresser": "Парикмахерская", "beauty": "Салон красоты",
    "massage": "Массажный салон", "fitness_centre": "Фитнес-клуб",
    "lawyer": "Юридические услуги", "accountant": "Бухгалтерские услуги",
    "insurance": "Страхование", "estate_agent": "Агентство недвижимости",
    "travel_agency": "Турагентство", "employment_agency": "Кадровое агентство",
    "advertising_agency": "Рекламное агентство", "it": "ИТ-компания",
    "company": "Компания", "logistics": "Логистика", "moving_company": "Переезды",
    "bank": "Банк", "cafe": "Кафе", "restaurant": "Ресторан", "bar": "Бар",
    "fast_food": "Быстрое питание", "hotel": "Гостиница",
    "school": "Школа", "language_school": "Языковая школа",
    "driving_school": "Автошкола", "kindergarten": "Детский сад",
    "copyshop": "Типография", "printer": "Типография",
    "furniture": "Мебель", "doityourself": "Стройматериалы",
    "hardware": "Хозтовары", "supermarket": "Супермаркет",
    "convenience": "Магазин у дома", "clothes": "Одежда",
    "laundry": "Прачечная", "dry_cleaning": "Химчистка",
    "funeral_directors": "Ритуальные услуги", "optician": "Оптика",
    "florist": "Цветы", "bakery": "Пекарня", "butcher": "Мясная лавка",
    "jewelry": "Ювелирный", "mobile_phone": "Салон связи",
    "computer": "Компьютерный магазин", "electronics": "Электроника",
    "notary": "Нотариус", "construction_company": "Строительная компания",
    "educational_institution": "Учебный центр", "laboratory": "Лаборатория",
    "car_wash": "Автомойка", "windows": "Окна", "doors": "Двери",
    "kitchen": "Кухни", "confectionery": "Кондитерская", "hostel": "Хостел",
    "builder": "Строительство", "plumber": "Сантехник",
    "electrician": "Электрик", "hvac": "Вентиляция и кондиционеры",
    "carpenter": "Столярные работы", "window_construction": "Окна",
    "plasterer": "Отделочные работы",
}


def rubric_ru(tags):
    """Понятное название рубрики из тегов OSM."""
    for key in ("amenity", "shop", "office", "healthcare", "leisure",
                "tourism", "craft"):
        v = (tags.get(key) or "").strip()
        if v:
            return RUBRIC_RU.get(v, v.replace("_", " "))
    return ""


# Признаки того, что объект — организация, а не дом и не улица.
BIZ_KEYS = ("shop", "office", "amenity", "craft", "healthcare", "company")

# Теги со ссылками на соцсети. Ради contact:vk всё и затевалось.
LINK_TAGS = ("contact:vk", "contact:telegram", "contact:instagram",
             "contact:facebook", "contact:youtube", "contact:ok")


def stem(query):
    """Основа слова для поиска по названию.

    «Стоматология» должна находить и «стоматологическую клинику», и
    «стоматологию», поэтому ищем по основе, а не по слову целиком.
    Отрезаем два последних знака у слов длиннее шести — грубо, но для
    русских окончаний работает, а перемудрить здесь опаснее: слишком
    короткая основа притащит всё подряд.
    """
    q = (query or "").strip().lower()
    if len(q) > 6 and " " not in q:
        q = q[:-2]
    return re.sub(r'["\\\\\\[\\]()|]', "", q)


def bbox(city):
    """Прямоугольник города в порядке, который ждёт Overpass."""
    try:
        lon, lat = [float(x) for x in (city.get("ll") or "").split(",")]
        dlon, dlat = [float(x) for x in (city.get("spn") or "0.5,0.4").split(",")]
    except Exception:
        return ""
    return "%.4f,%.4f,%.4f,%.4f" % (lat - dlat / 2, lon - dlon / 2,
                                    lat + dlat / 2, lon + dlon / 2)


def build_query(query, city, limit=400):
    """Запрос на языке Overpass.

    Ищем двумя способами сразу: по тегам вида деятельности, если он нам
    знаком, и по названию всегда. Первое находит организации, которые
    никак не назвали себя в названии («Дента-Люкс» — стоматология),
    второе — те, у кого тег не проставлен, а в названии всё написано.
    """
    box = bbox(city)
    if not box:
        return ""
    low = (query or "").lower()
    parts = []
    # Совпавших слов может быть несколько: «медицинская клиника» — это и
    # clinic, и doctors. Раньше брали первое попавшееся и выходили, и
    # половина подходящих тегов терялась. Больше трёх групп не берём:
    # Overpass отвечает отказом на слишком широкий запрос.
    seen_tags = []
    for word, tags in TAGS.items():
        if word in low:
            for t in tags:
                if t not in seen_tags:
                    seen_tags.append(t)
        if len(seen_tags) >= 4:
            break
    for t in seen_tags[:4]:
        parts.append('nwr%s(%s);' % (t, box))
    name = stem(query)
    if name:
        # Поиск по названию — только среди организаций.
        #
        # Голое ["name"~"дизайн"] заставляет сервер просмотреть все
        # объекты города: дома, улицы, остановки. Он отвечает на такое
        # отказом 504, а если отвечает — приносит переулок Дизайнеров
        # вместо студии. Пара «название + признак организации» ищется по
        # указателю и стоит дёшево.
        for key in BIZ_KEYS:
            parts.append('nwr["name"~"%s",i]["%s"](%s);' % (name, key, box))
    if not parts:
        return ""
    return ("[out:json][timeout:50];(%s);out center tags %d;"
            % ("".join(parts), int(limit)))


def search(query, city, pages=1, session=None, on_log=None, should_stop=None,
           limit=400):
    """Организации по виду деятельности в городе. Ключ не нужен."""
    q = build_query(query, city, limit=limit)
    if not q:
        if on_log:
            on_log("OSM: для «%s» нет координат — пропускаю"
                   % (city or {}).get("name", "?"), "warn")
        return []

    s = session or requests.Session()
    data = None
    for url in MIRRORS:
        if should_stop and should_stop():
            return []
        try:
            r = s.post(url, data={"data": q}, timeout=70,
                       headers={"User-Agent": settings.USER_AGENT})
        except Exception as e:
            if on_log:
                on_log("OSM: %s не ответил (%s)" % (_host(url), str(e)[:90]), "warn")
            continue
        if r.status_code == 429 or r.status_code == 504:
            # Зеркало занято чужими запросами — это нормально, идём к
            # следующему, а не объявляем источник сломанным.
            if on_log:
                on_log("OSM: %s занят (%s), пробую другое зеркало"
                       % (_host(url), r.status_code), "warn")
            continue
        if r.status_code != 200:
            if on_log:
                on_log("OSM: %s ответил %s" % (_host(url), r.status_code), "warn")
            continue
        try:
            data = r.json()
            break
        except Exception:
            continue

    if data is None and not (should_stop and should_stop()):
        # Зеркала бесплатные и перегружаются пачками, но отпускает их
        # быстро. Один повтор через полминуты спасает большую часть
        # прогонов; без него город просто выпадал из поиска.
        if on_log:
            on_log("OSM: все зеркала заняты, жду полминуты и пробую ещё раз",
                   "warn")
        time.sleep(30)
        for url in MIRRORS:
            if should_stop and should_stop():
                break
            try:
                r = s.post(url, data={"data": q}, timeout=70,
                           headers={"User-Agent": settings.USER_AGENT})
                if r.status_code == 200:
                    data = r.json()
                    break
            except Exception:
                continue

    if data is None:
        if on_log:
            on_log("OSM: ни одно зеркало не ответило и со второго раза. "
                   "Это проходит само — попробуйте через несколько минут.",
                   "warn")
        return []

    out, seen = [], set()
    for el in (data.get("elements") or []):
        t = el.get("tags") or {}
        name = (t.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        phones = []
        for key in ("phone", "contact:phone", "contact:mobile"):
            for p in (t.get(key) or "").split(";"):
                p = p.strip()
                if p and p not in phones:
                    phones.append(p)
        site = (t.get("website") or t.get("contact:website") or "").strip()
        links = [t[k].strip() for k in LINK_TAGS if (t.get(k) or "").strip()]
        out.append({
            "name": name,
            "address": _address(t),
            "site": site,
            "phones": phones[:4],
            "links": [_full(l) for l in links][:6],
            "rubric": rubric_ru(t),
            "emails": [e.strip() for e in (t.get("email") or
                                           t.get("contact:email") or "").split(";")
                       if e.strip()][:2],
        })
    if on_log:
        on_log("OSM: найдено организаций %d" % len(out))
    return out


def _address(t):
    parts = [t.get("addr:city"), t.get("addr:street"), t.get("addr:housenumber")]
    return ", ".join(p for p in parts if p)


def _full(link):
    """В OSM ссылки пишут и полностью, и просто именем сообщества."""
    link = link.strip()
    if link.startswith("http"):
        return link
    if link.startswith("@"):
        return "https://t.me/" + link[1:]
    return "https://vk.com/" + link


def _host(url):
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except Exception:
        return url
