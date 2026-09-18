# -*- coding: utf-8 -*-
"""Соцсети компании и то, что она сама опубликовала о руководителе.

Здесь проходит граница, которую стоит держать в голове.

Профиль компании в VK и ссылка на страницу директора, которую компания
сама разместила у себя на сайте в разделе «Руководство», — это деловые
контакты. Их опубликовали, чтобы по ним обращались.

Личная страница человека, найденная поиском по ФИО, — другое. Во-первых,
это профилирование частного лица, а собранная база с ФИО и ссылками на
личные аккаунты подпадает под 152-ФЗ целиком. Во-вторых, по имени
надёжно не найти: Ивановых Иванов в Москве тысячи, и ошибка здесь дорогая
— письмо уйдёт постороннему человеку.

Поэтому программа собирает только первое. Для второго она готовит ссылки
на поиск, а решение и проверку оставляет человеку: `search_links`
возвращает готовые запросы, ничего не скачивая и ничего не сохраняя.
"""
import re
from urllib.parse import quote

# Сети, ссылки на которые снимаем с сайта.
#
# Instagram здесь есть, потому что российские компании продолжают его
# указывать у себя на сайте, и как контакт он работает. Одна оговорка на
# всякий случай: размещать там рекламу по российскому закону нельзя —
# поле годится для связи и для того, чтобы посмотреть на компанию, но не
# для рекламных размещений.
NETS = [
    ("vk", "ВКонтакте", re.compile(r"(?:https?://)?(?:m\.)?vk\.com/([A-Za-z0-9_.]{2,60})", re.I)),
    ("telegram", "Telegram", re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})", re.I)),
    ("tenchat", "TenChat", re.compile(r"(?:https?://)?tenchat\.ru/([A-Za-z0-9_.\-]{2,60})", re.I)),
    ("ok", "Одноклассники", re.compile(r"(?:https?://)?ok\.ru/([A-Za-z0-9_./]{2,60})", re.I)),
    ("youtube", "YouTube", re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/((?:@|c/|channel/|user/)[A-Za-z0-9_\-]{2,60})", re.I)),
    ("dzen", "Дзен", re.compile(r"(?:https?://)?dzen\.ru/([A-Za-z0-9_\-]{2,60})", re.I)),
    ("rutube", "Rutube", re.compile(r"(?:https?://)?rutube\.ru/channel/([0-9]{2,20})", re.I)),
    ("instagram", "Instagram", re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,40})", re.I)),
    # Не соцсеть, а прямой канал связи, но собирается тем же способом и
    # лежит на сайте рядом с телефонами. Продавцу он нужнее иного профиля.
    ("whatsapp", "WhatsApp", re.compile(r"(?:https?://)?(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?([0-9]{10,15})", re.I)),
]

BASE = {"vk": "https://vk.com/", "telegram": "https://t.me/",
        "tenchat": "https://tenchat.ru/", "ok": "https://ok.ru/",
        "youtube": "https://youtube.com/", "dzen": "https://dzen.ru/",
        "rutube": "https://rutube.ru/channel/",
        "instagram": "https://instagram.com/", "whatsapp": "https://wa.me/"}

# Служебные адреса тех же доменов: кнопки «поделиться», виджеты, справка.
# Без этого списка у каждой второй компании «профилем ВКонтакте» оказывается
# vk.com/share.php с кнопки репоста.
JUNK = {"share", "share.php", "widget", "js", "im", "away.php", "login",
        "video_ext.php", "dev", "about", "help", "support", "terms",
        "privacy", "iv", "s", "joinchat", "addstickers", "proxy", "socks",
        "p", "reel", "reels", "explore", "accounts", "stories", "tv"}


def _clean(net, slug):
    slug = (slug or "").strip("/.").strip()
    if not slug or slug.split("/")[0].lower() in JUNK:
        return ""
    if net == "vk" and slug.lower().endswith(".php"):
        return ""
    return slug


def from_text(text):
    """Все профили соцсетей в куске HTML или текста."""
    found = {}
    for net, title, rx in NETS:
        for m in rx.findall(text or ""):
            slug = _clean(net, m)
            if slug:
                found.setdefault(net, [])
                if slug not in found[net]:
                    found[net].append(slug)
    return found


def as_links(found, limit=3):
    """[(сеть, название, адрес), ...] — в том виде, в каком показывать."""
    titles = {net: title for net, title, _ in NETS}
    out = []
    for net, slugs in found.items():
        for slug in slugs[:limit]:
            out.append((net, titles.get(net, net), BASE[net] + slug))
    return out


def near_person(pages_text, fio, window=6):
    """Профили, стоящие рядом с ФИО руководителя на странице.

    Компания сама опубликовала их в разделе «Руководство» — значит, по ним
    и предлагается обращаться. Окно узкое по той же причине, что и у
    поиска почт: шире — и к директору прицепится группа компании из
    подвала страницы.
    """
    from .enrich import surname_forms
    forms = set(surname_forms(fio))
    parts = [p for p in re.split(r"[\s,]+", (fio or "").strip()) if p]
    if parts:
        forms.add(parts[0].lower())
    if not forms:
        return {}

    found = {}
    for lines in pages_text or []:
        for i, line in enumerate(lines):
            if not any(f in line.lower() for f in forms):
                continue
            chunk = "\n".join(lines[max(0, i - window):i + window + 1])
            for net, slugs in from_text(chunk).items():
                found.setdefault(net, [])
                for s in slugs:
                    if s not in found[net]:
                        found[net].append(s)
    return found


def search_links(fio, company="", region=""):
    """Готовые запросы для поиска вручную.

    Программа сюда не ходит и ничего не сохраняет: по имени надёжно не
    найти — однофамильцев в любом городе сотни, — а автоматически
    собранная база личных страниц это уже профилирование частного лица.
    Человек открывает ссылку, смотрит и решает сам.
    """
    fio = (fio or "").strip()
    if not fio:
        return []
    who = quote(fio)
    both = quote("%s %s" % (fio, company)).strip()
    return [
        ("ВКонтакте", "https://vk.com/search/people?q=%s" % who),
        ("TenChat", "https://tenchat.ru/search?query=%s" % who),
        ("Яндекс", "https://yandex.ru/search/?text=%s" % both),
    ]
