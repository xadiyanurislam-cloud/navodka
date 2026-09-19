# -*- coding: utf-8 -*-
"""ВКонтакте — контактные лица, которые компания указала сама.

Почему именно этот путь, а не поиск людей по ФИО. Искать «Иванова Ивана»
перебором профилей бессмысленно: однофамильцев в любом городе сотни,
проверить некому, и на выходе получается список «возможно, это он» —
хуже пустой клетки. Плюс ФИО рядом с личным профилем — уже персональные
данные, и собирать их наугад нельзя.

Здесь всё наоборот. У группы компании есть раздел «Контакты», куда
владелец сам ставит профили живых людей с подписью «Директор»,
«Владелец», «Руководитель». Это не догадка и не находка поисковика — это
рабочий контакт, опубликованный самой компанией для того, чтобы по нему
писали. Остаётся сверить фамилию с ЕГРЮЛ и сказать, совпало или нет.

Нужен сервисный ключ: vk.com/apps?act=manage → создать приложение →
«Сервисный ключ доступа». Выдаётся сразу, без модерации, срок не
ограничен.

Важное ограничение, из которого следует вся здешняя логика. Сервисный
ключ умеет не всё: groups.getById с ним работает, а groups.search — нет,
поиск по сообществам ВК разрешает только ключом пользователя, который
живёт час и требует входа через браузер. Для программы, которая должна
работать сама, это не годится.

Поэтому группу мы не ищем, а узнаём: ссылку на неё компания уже
опубликовала — на своём сайте или в карточке справочника, и к моменту
этого шага она лежит в базе. По ссылке достаточно getById. Поиск по
названию остался запасным путём на случай, если ключ пользователя всё же
задан, и отключается после первого отказа.
"""
import re
import time

import requests

from .. import settings

API = "https://api.vk.com/method/"
VERSION = "5.199"

# Подписи, по которым контакт группы читается как первое лицо, а не как
# менеджер по работе с клиентами.
BOSS_WORDS = ("директор", "руководител", "владел", "собственник", "основател",
              "управляющ", "president", "ceo", "founder", "главный врач",
              "заведующ")


def _call(method, params, token, session=None, timeout=15):
    """Вызов метода. Возвращает (ответ, ошибка)."""
    s = session or requests.Session()
    body = dict(params, access_token=token, v=VERSION, lang="ru")
    try:
        r = s.post(API + method, data=body, timeout=timeout,
                   headers={"User-Agent": settings.USER_AGENT})
    except Exception as e:
        return None, "ВК недоступен: %s" % str(e)[:140]
    try:
        d = r.json() or {}
    except Exception:
        return None, "ВК вернул не JSON"
    if "error" in d:
        err = d["error"]
        code = err.get("error_code")
        msg = err.get("error_msg") or ""
        if code == 5:
            return None, "ключ ВК не подошёл — проверьте сервисный ключ в «Настройках»"
        if code == 6:
            return None, "слишком часто"        # обрабатывается повтором
        if code == 29:
            return None, "у ключа ВК кончился дневной лимит"
        if code in (15, 27, 28):
            # 15 — доступ запрещён, 27/28 — метод требует ключа
            # сообщества или приложения. Для поиска по сообществам это
            # означает одно: сервисным ключом так нельзя.
            return None, ("этот метод недоступен сервисному ключу "
                          "(ВК: %s)" % msg[:90])
        return None, "ВК ответил ошибкой %s: %s" % (code, msg[:120])
    return d.get("response"), ""


SCREEN_RE = re.compile(r"(?:https?://)?(?:m\.)?vk\.com/([A-Za-z0-9_.]{2,60})", re.I)

# Адреса самого ВК, которые встречаются в ссылках, но группой не являются.
NOT_A_GROUP = {"share", "share.php", "away.php", "im", "video", "audio",
               "widget_community.php", "js", "login", "id0"}


def screen_name(url):
    """Короткое имя сообщества из ссылки. Пустая строка — не то."""
    m = SCREEN_RE.search(url or "")
    if not m:
        return ""
    name = m.group(1).strip("/.")
    if not name or name.lower() in NOT_A_GROUP:
        return ""
    return name


def by_url(url, token, session=None, on_log=None):
    """Сообщество по ссылке, которую компания опубликовала сама.

    Главный путь: работает сервисным ключом и не зависит от поиска.
    Возвращает ({id, name, url}, ошибка).
    """
    name = screen_name(url)
    if not name or not token:
        return {}, ""
    resp, err = _call("groups.getById",
                      {"group_id": name,
                       "fields": "contacts,description,site,members_count"},
                      token, session)
    if err:
        return {}, err
    groups = (resp or {}).get("groups") or (resp if isinstance(resp, list) else [])
    if not groups:
        return {}, ""
    g = groups[0]
    return {"id": g.get("id"), "name": g.get("name") or "",
            "url": "https://vk.com/" + (g.get("screen_name") or
                                        ("club%s" % g.get("id"))),
            "raw": g}, ""


def by_domain(site, token, session=None, on_log=None):
    """Сообщество по домену сайта — с обязательной сверкой.

    Компании сплошь и рядом берут для группы то же короткое имя, что и
    для домена: romashka.ru → vk.com/romashka. Это догадка, и сама по
    себе она ничего не стоит: под тем же именем может сидеть кто угодно,
    а чужая группа в карточке хуже пустой клетки — по ней напишут.

    Поэтому догадку обязательно проверяем. Засчитываем только если в
    самой группе в поле «Сайт» стоит тот же домен: это уже не совпадение
    имён, а подтверждение от самой компании. Если поля нет — отказываемся,
    даже когда группа существует и название похоже.
    """
    host = domain_of(site)
    if not host or not token:
        return {}, ""
    label = host.split(".")[0]
    if len(label) < 4 or label in ("www", "shop", "site", "home", "info"):
        return {}, ""
    got, err = by_url("https://vk.com/" + label, token, session)
    if err or not got:
        return {}, err
    raw = got.get("raw") or {}
    if domain_of(raw.get("site") or "") != host:
        if on_log:
            on_log("ВК: vk.com/%s существует, но своим сайтом называет не "
                   "%s — не засчитываю" % (label, host))
        return {}, ""
    if on_log:
        on_log("ВК: vk.com/%s подтверждена — сама группа ссылается на %s"
               % (label, host))
    return got, ""


def domain_of(url):
    """Второй уровень домена из любого вида ссылки."""
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u).split("/")[0].split("?")[0]
    if u.startswith("www."):
        u = u[4:]
    return u if "." in u else ""


def find_group(name, token, city_id=None, session=None, on_log=None):
    """Группа компании по названию. Возвращает лучшую из найденных.

    Берём именно первую: выдача ВК отсортирована по релевантности, а
    дальше идут однофамильцы бизнеса — «Ромашка» цветочная, «Ромашка»
    детский сад и «Ромашка» паблик с картинками.
    """
    clean = _clean_name(name)
    if not clean or not token:
        return {}
    params = {"q": clean, "type": "page,group", "count": 5, "sort": 0}
    if city_id:
        params["city_id"] = city_id
    resp, err = _call("groups.search", params, token, session)
    if err == "слишком часто":
        time.sleep(0.4)
        resp, err = _call("groups.search", params, token, session)
    if err:
        if on_log:
            on_log("ВК: %s" % err, "warn")
        # Отдаём ошибку наружу отдельным полем: вызывающий должен
        # отличить «не нашлось» от «этим ключом так нельзя» и во втором
        # случае перестать пробовать.
        return {"error": err}
    items = (resp or {}).get("items") or []
    for it in items:
        # Отсекаем заведомо не то: закрытые и удалённые сообщества.
        if it.get("is_closed") == 2 or it.get("deactivated"):
            continue
        if not _looks_same(clean, it.get("name") or ""):
            continue
        return {"id": it.get("id"), "name": it.get("name") or "",
                "url": "https://vk.com/" + (it.get("screen_name") or
                                            ("club%s" % it.get("id")))}
    return {}


def contacts_from_group(g, token, session=None):
    """Контактные лица из уже полученной карточки сообщества.

    Отдельно от group_contacts, чтобы не ходить в ВК второй раз за тем,
    что уже пришло в ответе by_url.
    """
    return _people(g or {}, token, session)


def group_contacts(group_id, token, session=None, on_log=None):
    """Контактные лица группы: имя, ссылка на профиль, подпись.

    Возвращает список словарей. Пустой список — нормальный исход:
    контакты заполняет далеко не каждая компания.
    """
    if not group_id or not token:
        return [], {}
    resp, err = _call("groups.getById",
                      {"group_id": group_id,
                       "fields": "contacts,description,site,members_count,city"},
                      token, session)
    if err:
        if on_log:
            on_log("ВК: %s" % err, "warn")
        return [], {}
    groups = (resp or {}).get("groups") or (resp if isinstance(resp, list) else [])
    if not groups:
        return [], {}
    return _people(groups[0], token, session)


def _people(g, token, session=None):
    """Разбор контактов сообщества. Возвращает (список, сведения)."""
    about = {"site": g.get("site") or "", "members": g.get("members_count") or 0,
             "description": (g.get("description") or "")[:600]}

    raw = g.get("contacts") or []
    user_ids = [str(c["user_id"]) for c in raw if c.get("user_id")]
    people = {}
    if user_ids:
        resp2, err2 = _call("users.get",
                            {"user_ids": ",".join(user_ids[:20]),
                             "fields": "domain,city"}, token, session)
        if not err2:
            for u in (resp2 or []):
                people[u.get("id")] = u

    out = []
    for c in raw:
        uid = c.get("user_id")
        u = people.get(uid) or {}
        fio = " ".join(x for x in [u.get("last_name"), u.get("first_name")] if x)
        out.append({
            "user_id": uid,
            "name": fio,
            "url": ("https://vk.com/" + (u.get("domain") or "id%s" % uid)) if uid else "",
            "post": (c.get("desc") or "").strip(),
            "email": (c.get("email") or "").strip(),
            "phone": (c.get("phone") or "").strip(),
            "boss": _is_boss(c.get("desc") or ""),
        })
    return out, about


def _is_boss(desc):
    low = (desc or "").lower()
    return any(w in low for w in BOSS_WORDS)


def _clean_name(name):
    """Название компании без организационной шелухи.

    Искать группу по «ООО "Стоматология Улыбка"» бесполезно: в ВК она
    называется «Стоматология Улыбка», а кавычки и ООО только сбивают
    поиск.
    """
    s = (name or "").strip()
    s = re.sub(r'^(ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО|АНО|НАО)\s+', "", s, flags=re.I)
    s = s.replace("«", " ").replace("»", " ").replace('"', " ")
    return re.sub(r"\s+", " ", s).strip()[:60]


def _looks_same(want, got):
    """Похожи ли названия. Нужно, чтобы «Ромашка» не приводила паблик с
    котиками, который просто называется «Ромашка»."""
    a = re.sub(r"[^\w]+", "", (want or "").lower())
    b = re.sub(r"[^\w]+", "", (got or "").lower())
    if not a or not b:
        return False
    return a in b or b in a
