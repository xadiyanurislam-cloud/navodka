# -*- coding: utf-8 -*-
"""Обход сайта компании: почты, телефоны, телеграм и технографика.

Берём не весь сайт, а десяток страниц, где контакты лежат почти всегда.
Полный обход даёт те же адреса, но в двадцать раз медленнее и заметно
грубее по отношению к чужому серверу.

Отдельно снимаем технографику — какие счётчики и виджеты стоят в коде.
Это лучший из дешёвых признаков: компания с коллтрекингом уже платит за
звонки и уже согласилась, что их надо считать. Такой лид не нужно
убеждать в существовании проблемы.
"""
import bisect
import re
import time
from urllib.parse import urljoin, urlparse

import requests

from .. import settings

# Страницы, которые пробуем. Первая — главная, дальше самые частые адреса
# контактных разделов в русском вебе.
# Порядок здесь — это порядок обхода, и он важнее состава.
#
# Страниц в списке больше, чем программа успевает обойти, поэтому ценные
# должны стоять первыми. Однажды страницы про людей оказались в конце — и
# при лимите в десять страниц не открывались вовсе, то есть вся работа по
# поиску руководителя на сайте шла впустую.
PATHS = [
    "",                                             # главная: подвал с соцсетями
    "/contacts", "/kontakty", "/contact",           # общие контакты
    "/team", "/komanda", "/rukovodstvo",            # люди с должностями
    "/about", "/o-kompanii",                        # чем занимается
    "/management", "/nasha-komanda", "/sotrudniki",
    "/contacts/", "/kontakty/", "/about/", "/o-nas", "/company",
    "/administraciya", "/vrachi", "/specialists", "/staff",
    "/rekvizity", "/requisites", "/vacancy", "/vacancies", "/karera",
    "/privacy", "/policy",
]

# Должности первых лиц. По ним страница «Команда» превращается из списка
# имён в ответ на вопрос «кто тут главный».
# Должности, которые ищем рядом с именем на странице.
#
# Порядок здесь не значит ничего: совпадение берётся самое длинное.
# Раньше бралось первое, и «коммерческий директор» становился просто
# «директором» — в списке на обзвон это потеря той самой строки, ради
# которой страницу и читали.
BOSS_POSTS = ("генеральный директор", "коммерческий директор",
              "исполнительный директор", "финансовый директор",
              "технический директор", "директор по развитию",
              "директор", "руководитель отдела продаж", "руководитель",
              "владелец", "собственник", "основатель", "учредитель",
              "управляющий", "президент", "главный врач", "заведующий",
              "председатель", "главный инженер", "начальник отдела")

# Из них — те, кто решает сам.
#
# Названы поимённо, а не «первыми восемью в списке». Срез по номеру
# менял смысл молча: стоило дописать должность в начало — и
# «управляющий» переставал считаться первым лицом, без единого признака,
# что что-то изменилось.
# Реквизиты в подвале сайта.
#
# Зачем это здесь. Без ИНН программа не может спросить ни ЕГРЮЛ, ни ФНС:
# карточка остаётся без руководителя, без выручки, без численности и без
# года регистрации — то есть без всего, ради чего её открывают. А искать
# по названию получается не всегда: в справочнике стоит вывеска «Fesco»,
# а в ЕГРЮЛ — ПАО «ДВМП», и по бренду юрлицо не находится.
#
# При этом сам ИНН лежит на виду: российские компании публикуют
# реквизиты в подвале главной или на странице «Реквизиты», а её мы и так
# скачиваем. Оставалось прочитать.
INN_RE = re.compile(r"ИНН[\s:№\-–—/]{0,4}(\d{12}|\d{10})(?!\d)", re.I)
OGRN_RE = re.compile(r"ОГРН(?:ИП)?[\s:№\-–—/]{0,4}(\d{15}|\d{13})(?!\d)", re.I)


def inn_ok(value):
    """Проверка контрольной суммы ИНН.

    Не перестраховка. Неверный ИНН — это не пустая карточка, а чужая:
    по нему из ЕГРЮЛ придёт другая компания, с другим руководителем и
    другой выручкой, и отличить её будет нельзя. Лучше не знать ИНН,
    чем знать неправильный.
    """
    d = [int(x) for x in (value or "") if x.isdigit()]
    if len(d) == 10:
        w = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        return sum(a * b for a, b in zip(d, w)) % 11 % 10 == d[9]
    if len(d) == 12:
        w1 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        w2 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        return (sum(a * b for a, b in zip(d, w1)) % 11 % 10 == d[10]
                and sum(a * b for a, b in zip(d, w2)) % 11 % 10 == d[11])
    return False


def ogrn_ok(value):
    """Проверка контрольной суммы ОГРН: остаток от деления на 11 (для
    ОГРНИП — на 13) совпадает с последней цифрой."""
    v = "".join(x for x in (value or "") if x.isdigit())
    if len(v) == 13:
        return int(v[:12]) % 11 % 10 == int(v[12])
    if len(v) == 15:
        return int(v[:14]) % 13 % 10 == int(v[14])
    return False


def requisites(text):
    """ИНН и ОГРН из текста страницы — только прошедшие проверку."""
    out = {}
    for m in INN_RE.finditer(text):
        if inn_ok(m.group(1)):
            out["inn"] = m.group(1)
            break
    for m in OGRN_RE.finditer(text):
        if ogrn_ok(m.group(1)):
            out["ogrn"] = m.group(1)
            break
    return out


BOSS = frozenset((
    "генеральный директор", "коммерческий директор",
    "исполнительный директор", "директор", "руководитель", "владелец",
    "собственник", "основатель", "учредитель", "управляющий",
    "президент", "главный врач",
))

# Сколько разметки читать и сколько разбирать.
#
# Это разные числа, и разница важна. Читать дёшево — это сеть; разбирать
# дорого — регулярки по мегабайтам держат общую блокировку Python и
# подвешивают окно программы. Поэтому читаем с запасом, а разбираем
# начало и конец: контакты стоят в шапке, а соцсети — в подвале, то есть
# в самом конце документа. Середина страницы — товары и текст, в них
# ничего нужного нет.
PARSE_HEAD = 300 * 1024
PARSE_TAIL = 150 * 1024
# Предел на случай, когда вместо страницы отдают поток без конца.
HARD_CAP = 8 * 1024 * 1024

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
TG_RE = re.compile(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})")

# Расширения файлов, которые почтовый регэксп ловит как адреса: «logo@2x.png»
# выглядит для него совершенно нормальной почтой.
BAD_TAIL = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js",
            ".woff", ".woff2", ".ttf", ".ico", ".pdf", ".mp4")

# Домены из макетов. Их забывают убрать ровно так же, как телефон
# 8 800 555-35-35, и продавец тратит на каждый по письму, прежде чем
# понять, что писал в пустоту.
JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "example.ru",
    "domain.com", "domain.ru", "yourdomain.com", "yourdomain.ru",
    "mysite.ru", "site.com", "sitename.ru", "test.com", "test.ru",
    "mail.example.com", "company.com", "yourcompany.com",
    "email.com", "yoursite.ru", "адрес.рф",
}

# Бесплатный номер — сильнейший признак телефонных продаж. Его заводят
# только там, где звонков много и за них платят: сам номер стоит денег,
# и ради двух звонков в неделю его никто не берёт.
TOLLFREE_RE = re.compile(r"8[\s\-(]*800[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")

# Фразы, которыми обещают перезвонить. Значит, входящую заявку
# обрабатывает живой человек по телефону, а не почтовый робот.
CALLBACK_WORDS = ("перезвон", "заказать звонок", "обратный звонок",
                  "свяжемся с вами", "менеджер свяжется", "консультация по телефону",
                  "закажите звонок", "мы вам позвоним")

# Круглосуточная или расширенная работа — отдельная смена на телефоне.
HOURS_WORDS = ("круглосуточно", "24/7", "без выходных", "ежедневно с")

# Компания о себе в цифрах. Это не ЕГРЮЛ и не отчётность, а то, что она
# сама вынесла на главную, — и в разговоре ссылаться удобнее именно на
# это: «вы пишете, что работаете с 2011 года».
SELF_YEAR_RE = re.compile(r"(?:с|от|основан[аоы]?\s+в|работаем\s+с|на\s+рынке\s+с)"
                          r"\s+(19[89]\d|20[0-2]\d)\s*(?:год|г\.|г\b)?", re.I)
SELF_STAFF_RE = re.compile(r"(?:более|свыше|более\s+чем|уже)\s+(\d{2,5})\s*"
                           r"(?:сотрудник|специалист|человек|врач|мастер|инженер)", re.I)
SELF_BRANCH_RE = re.compile(r"(\d{1,3})\s*(?:филиал|офис|салон|клиник|магазин|точ)", re.I)

# Признаки того, как компания продаёт. Интернет-магазин с ценами — продажи
# идут без менеджера, и телефонный отдел там вспомогательный. Каталог без
# цен, наоборот, значит «цену узнавайте по телефону».
SHOP_WORDS = ("добавить в корзину", "в корзину", "оформить заказ", "купить в 1 клик",
              "каталог товаров", "добавлено в корзину")
PRICE_RE = re.compile(r"\d[\d\s]{2,8}\s*(?:₽|руб\.?|р\.)", re.I)
NOPRICE_WORDS = ("цена по запросу", "уточняйте цену", "цены по запросу",
                 "стоимость по запросу", "рассчитать стоимость")

# Мобильное приложение — отдельный бюджет на разработку, то есть компания
# уже тратит на цифровые каналы и разговор про софт начинается не с нуля.
APP_RE = re.compile(r"(?:apps\.apple\.com|play\.google\.com/store|rustore\.ru|appgallery)", re.I)

# Дата последней публикации: сайт с новостями трёхлетней давности и сайт,
# который вели вчера, — разные компании, даже при одинаковой выручке.
DATE_RE = re.compile(r"\b(\d{1,2})[.\s]"
                     r"(0[1-9]|1[0-2]|янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)"
                     r"[а-я.]*[.\s](20[12]\d)\b", re.I)
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7,
          "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

# Движок сайта. Косвенно говорит о бюджете: Битрикс за полмиллиона и
# конструктор за три тысячи — разные компании, даже при одной выручке.
CMS = {
    "bitrix": "1С-Битрикс", "/bitrix/": "1С-Битрикс",
    "tilda": "Tilda", "wp-content": "WordPress", "wp-includes": "WordPress",
    "joomla": "Joomla", "modx": "MODX", "opencart": "OpenCart",
    "insales": "InSales", "nethouse": "Nethouse", "craftum": "Craftum",
}

# Технографика: подстрока в коде страницы → что это значит.
TECH = {
    "calltouch":   ("calltracking", "Calltouch"),
    "cttrack":     ("calltracking", "Calltouch"),
    "roistat":     ("calltracking", "Roistat"),
    "callibri":    ("calltracking", "Callibri"),
    "comagic":     ("calltracking", "Comagic"),
    "uiscom":      ("calltracking", "UIS"),
    "mango-office": ("telephony", "Mango Office"),
    "mangosip":    ("telephony", "Mango Office"),
    "zadarma":     ("telephony", "Zadarma"),
    "telfin":      ("telephony", "Telfin"),
    "bitrix24":    ("crm", "Битрикс24"),
    "amocrm":      ("crm", "amoCRM"),
    "jivosite":    ("chat", "JivoSite"),
    "jivo.ru":     ("chat", "JivoSite"),
    "envybox":     ("chat", "EnvyBox"),
    "carrotquest": ("chat", "Carrot quest"),
}


def normalize_url(site):
    site = (site or "").strip()
    if not site:
        return ""
    if not site.startswith(("http://", "https://")):
        site = "https://" + site
    p = urlparse(site)
    if not p.netloc:
        return ""
    return "%s://%s" % (p.scheme, p.netloc)


def _self_facts(html, low_text, result):
    """Что компания говорит о себе цифрами и как она продаёт."""
    text = _squash_full(html)
    low = text.lower()

    if result["self_year"] is None:
        m = SELF_YEAR_RE.search(text)
        # Год из будущего и год до 1985 — это не «работаем с», а попавший
        # под шаблон номер или диапазон в тексте.
        if m and 1985 <= int(m.group(1)) <= 2030:
            result["self_year"] = int(m.group(1))
    if result["self_staff"] is None:
        m = SELF_STAFF_RE.search(text)
        if m:
            result["self_staff"] = int(m.group(1))
    if result["self_branches"] is None:
        m = SELF_BRANCH_RE.search(text)
        if m and 1 < int(m.group(1)) < 500:
            result["self_branches"] = int(m.group(1))

    if not result["shop"] and any(w in low for w in SHOP_WORDS):
        result["shop"] = True
    if not result["prices"] and len(PRICE_RE.findall(text)) >= 3:
        result["prices"] = True
    if not result["no_prices"] and any(w in low for w in NOPRICE_WORDS):
        result["no_prices"] = True
    if not result["app"] and APP_RE.search(html):
        result["app"] = True

    for m in DATE_RE.finditer(text):
        day, mon, year = m.groups()
        month = int(mon) if mon.isdigit() else MONTHS.get(mon[:3].lower(), 0)
        if not month:
            continue
        stamp = "%s-%02d-%02d" % (year, month, min(int(day), 31))
        if stamp > (result["last_post"] or ""):
            result["last_post"] = stamp


def _squash_full(html):
    """Весь текст страницы одной строкой — для поиска фраз о себе."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&quot;", '"')
                .replace("&amp;", "&").replace("&mdash;", "—"))
    return re.sub(r"\s+", " ", text)


def _tag(html, name):
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (name, name), html, re.S | re.I)
    return _squash(m.group(1)) if m else ""


# Тег целиком, вместе с кавычками: значение атрибута может содержать
# «>», и без учёта кавычек тег «кончится» посреди описания.
META_TAG_RE = re.compile(r'''<meta\b(?:[^>"\']|"[^"]*"|\'[^\']*\')*>''', re.I)
ATTR_RE = re.compile(r'''([\w:.-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>]+))''')


def _attrs(tag):
    out = {}
    for m in ATTR_RE.finditer(tag):
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        out[m.group(1).lower()] = val
    return out


def _meta(html, name):
    """Содержимое meta — порядок атрибутов в разметке произвольный.

    Разбираем по одному тегу, а не одним выражением на всю страницу:
    выражение «от content до name» охотно перепрыгивало через соседние
    теги и складывало в описание кодировку, заголовок и кусок разметки.
    """
    want = name.lower()
    for tag in META_TAG_RE.findall(html or ""):
        attrs = _attrs(tag)
        if attrs.get("name", "").lower() == want or \
                attrs.get("property", "").lower() == want:
            if attrs.get("content", "").strip():
                return _squash(attrs["content"])
    return ""


def _squash(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = (text.replace("&nbsp;", " ").replace("&quot;", '"')
                .replace("&amp;", "&").replace("&mdash;", "—"))
    return re.sub(r"\s+", " ", text).strip()[:400]


def _clean_tollfree(raw):
    digits = re.sub(r"\D", "", raw)
    return "8" + digits[1:] if len(digits) == 11 and digits.startswith("8") else ""


def _lines(html):
    """Текст страницы построчно — разметка выброшена."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", html)
    text = (text.replace("&nbsp;", " ").replace("&quot;", '"')
                .replace("&amp;", "&").replace("&#64;", "@"))
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _clean_email(addr):
    a = addr.strip().strip(".,;:").lower()
    if any(a.endswith(t) for t in BAD_TAIL):
        return ""
    if len(a) > 90 or a.count("@") != 1:
        return ""
    local, _, dom = a.partition("@")
    # Хэши и идентификаторы: длинная бессмысленная строка в локальной части —
    # это почти всегда не почта, а кусок минифицированного скрипта.
    if len(local) > 40 or not re.search(r"[aeiouyаеиоуыэюя]", local):
        if len(local) > 12:
            return ""
    if dom.endswith((".png", ".jpg", ".js", ".css")):
        return ""
    if dom in JUNK_DOMAINS:
        return ""
    # Последняя проверка формы. Сюда попадают не только находки
    # регулярного выражения, но и адреса из чужих ответов, где на форму
    # никто не смотрел: «a@b.c» адресом не является.
    #
    # Кириллица разрешена намеренно: домены в зоне .рф настоящие, и
    # отсечь их латинским шаблоном значит выбросить живые адреса.
    if not re.match(r"^[^\s@]+@[^\s@.]+(\.[^\s@.]+)*\.[^\W\d_]{2,}$",
                    a, re.U):
        return ""
    return a


# Первая цифра кода в России: 3 и 4 — города, 8 — особые коды вроде
# 800, 9 — мобильные. Всё остальное кодом быть не может, и номер с таким
# началом — не номер, а заглушка из вёрстки.
GOOD_CODE_HEAD = set("3489")

# Заведомые пустышки: их пишут в макетах и забывают убрать.
JUNK_PHONES = {"71234567890", "70000000000", "79999999999", "78005553535",
               "71111111111", "71231234567", "70001234567"}


def _clean_phone(raw):
    """Номер в единый вид. Пустая строка — значит, это не номер.

    Отсев не придирка. На живых сайтах попадаются +7 101 000-00-00 и
    +7 999 999-99-99 — заглушки, оставшиеся от вёрстки. Продавец тратит
    на каждую по звонку и начинает не верить всему списку.
    """
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 11 or digits[0] not in "78":
        return ""
    digits = "7" + digits[1:]
    if digits in JUNK_PHONES:
        return ""
    code, rest = digits[1:4], digits[4:]
    if code[0] not in GOOD_CODE_HEAD:
        return ""
    # Одна цифра на весь номер или на весь остаток — так номера не
    # выглядят: 8 800 000-00-00 бывает, 8 888 888-88-88 нет.
    if len(set(digits[1:])) <= 1 or len(set(rest)) <= 1 and rest[0] in "09":
        return ""
    return "+" + digits


def phone_kind(phone):
    """Мобильный, городской или бесплатный.

    Разница не косметическая: мобильный — это чей-то личный аппарат, и
    отвечает на него человек, а не приёмная.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) != 11:
        return ""
    code = digits[1:4]
    if code == "800":
        return "бесплатный"
    if code.startswith("9"):
        return "мобильный"
    return "городской"


def crawl(site, timeout=10, pause=0.4, max_pages=12, session=None, budget=25):
    """Возвращает словарь с находками. Сеть не обязана быть доступной —
    при любой ошибке возвращаем то, что успели собрать."""
    base = normalize_url(site)
    result = {"base": base, "emails": [], "phones": [], "telegram": [],
              "tech": {}, "pages": 0, "error": "", "text": [],
              "description": "", "title": "", "cms": "", "tollfree": [],
              "callback": False, "hours": "", "socials": {},
              "inn": "", "ogrn": "",
              "self_year": None, "self_staff": None, "self_branches": None,
              "shop": False, "prices": False, "no_prices": False,
              "app": False, "last_post": ""}
    if not base:
        result["error"] = "нет адреса сайта"
        return result

    s = session or requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT,
                      "Accept-Language": "ru,en;q=0.8"})
    emails, phones, tg = set(), set(), set()
    seen_html = 0
    browser = {"on": False}     # перешли ли на представление браузером

    def get(url, headers=None):
        """Забрать страницу, читая не больше разумного.

        Раньше бралось всё тело целиком. На сайте с гигантской главной
        это оборачивалось и минутой ожидания, и, что хуже, разбором
        мегабайтов разметки регулярками — а такая работа держит общую
        блокировку Python. Пока четыре потока этим заняты, потоку,
        который рисует окно, блокировка не достаётся, и Windows
        подписывает окно «Не отвечает».

        Страница читается целиком, но в руках остаётся только начало и
        конец: контакты стоят в шапке, соцсети — в подвале, а середина
        занята товарами и текстом.
        """
        r = s.get(url, timeout=timeout, allow_redirects=True,
                  headers=headers, stream=True)
        if r.status_code != 200 or "text/html" not in (r.headers.get("Content-Type") or ""):
            r.close()
            r._navodka_html = ""
            return r
        # Читаем страницу целиком, но держим только начало и конец.
        # Середина — товары и текст, в них ничего нужного нет, а вот
        # подвал с соцсетями стоит в самом конце документа, и обрезать
        # его нельзя. Память при этом ограничена: сколько бы ни весила
        # страница, в руках остаётся не больше полумегабайта.
        head, head_size, tail, size = [], 0, b"", 0
        try:
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if head_size < PARSE_HEAD:
                    head.append(chunk)
                    head_size += len(chunk)
                else:
                    tail = (tail + chunk)[-PARSE_TAIL:]
                if size >= HARD_CAP:
                    break
        except Exception:
            pass
        finally:
            r.close()
        raw = b"".join(head) + ((b"\n" + tail) if tail else b"")
        r._navodka_html = raw.decode(r.encoding or "utf-8", "replace")
        return r

    def fetch(url):
        """Забрать страницу, при отказе — ещё раз, как браузер.

        Каждый десятый сайт отвечает 403 всему, что не похоже на
        человека: так настроены Cloudflare и половина коробочных CMS. Для
        нас это выглядело как «сайт не открылся», хотя сайт жив и
        прекрасно открывается в окне браузера. Второй заход с полным
        набором браузерных заголовков снимает большую часть таких
        отказов.
        """
        from .. import net
        try:
            r = get(url, net.BROWSER_HEADERS if browser["on"] else None)
        except Exception:
            r = None
        if r is not None and r.status_code not in (403, 406, 429, 503):
            return r, ""
        if browser["on"]:
            return r, ("сайт отклонил запрос (%s)" % r.status_code) if r is not None \
                else "сайт не отвечает"
        browser["on"] = True
        try:
            return get(url, net.BROWSER_HEADERS), ""
        except Exception as e:
            return r, str(e)[:200]


    started = time.time()
    for path in PATHS[:max_pages]:
        # Предел времени на один сайт.
        #
        # Двенадцать страниц по десять секунд ожидания — это две минуты
        # на одну компанию, и живой, но медленный сайт в одиночку держал
        # всю очередь. Лучше взять с него что успели: контакты лежат на
        # первых двух страницах, а не на двенадцатой.
        if time.time() - started > budget and seen_html:
            break
        url = urljoin(base + "/", path.lstrip("/")) if path else base
        r, err = fetch(url)
        if r is None:
            if not seen_html and not path:
                result["error"] = err or "сайт не отвечает"
            continue
        if r.status_code != 200 or "text/html" not in (r.headers.get("Content-Type") or ""):
            if not seen_html and not path:
                result["error"] = "сайт ответил %s" % r.status_code
            continue
        seen_html += 1
        html = getattr(r, "_navodka_html", "") or ""

        for m in EMAIL_RE.findall(html):
            a = _clean_email(m)
            if a:
                emails.add(a)
        for m in PHONE_RE.findall(html):
            p = _clean_phone(m)
            if p:
                phones.add(p)
        for m in TG_RE.findall(html):
            if m.lower() not in ("share", "iv"):
                tg.add(m)

        # Текст страницы пригодится дальше: по нему ищутся контакты,
        # стоящие рядом с фамилией руководителя.
        result["text"].append(_lines(html))

        # Чем компания занимается — её же словами. Берём с главной:
        # на «Контактах» в описании обычно адрес, а не деятельность.
        if not path:
            result["title"] = _meta(html, "og:title") or _tag(html, "title")
            result["description"] = (_meta(html, "description")
                                     or _meta(html, "og:description"))

        for m in TOLLFREE_RE.findall(html):
            num = _clean_tollfree(m)
            if num and num not in result["tollfree"]:
                result["tollfree"].append(num)

        low = html.lower()
        for needle, (group, title) in TECH.items():
            if needle in low:
                result["tech"].setdefault(group, set()).add(title)
        if not result["cms"]:
            for needle, title in CMS.items():
                if needle in low:
                    result["cms"] = title
                    break
        if not result["callback"] and any(w in low for w in CALLBACK_WORDS):
            result["callback"] = True
        if not result["hours"]:
            for w in HOURS_WORDS:
                if w in low:
                    result["hours"] = w
                    break
        # Профили соцсетей — адресами, а не фактом наличия: «у них есть ВК»
        # для продавца бесполезно, ссылка на группу — нет.
        from .. import social as _social
        _self_facts(html, low_text=None, result=result)

        # Реквизиты: ИНН и ОГРН из подвала или со страницы «Реквизиты».
        # Первый найденный и выигрывает — на странице партнёров бывают
        # чужие, но стоят они ниже собственных.
        if not (result["inn"] and result["ogrn"]):
            for key, val in requisites(_squash_full(html)).items():
                if not result[key]:
                    result[key] = val

        for net, slugs in _social.from_text(html).items():
            bag = result["socials"].setdefault(net, [])
            for slug in slugs:
                if slug not in bag:
                    bag.append(slug)

        # Довольно. Если с трёх страниц уже собраны почты, телефоны и
        # соцсети, оставшиеся девять ничего не добавят, а времени займут
        # столько же. На полусотне компаний это разница между пятью
        # минутами и двадцатью.
        if seen_html >= 3 and len(emails) >= 2 and phones and result["socials"]:
            break

        # Пауза между страницами одного сайта. Десять запросов подряд без
        # задержки часть хостингов принимает за сканирование и банит по IP.
        time.sleep(pause)

    result["pages"] = seen_html
    result["emails"] = sorted(emails)
    result["phones"] = sorted(phones)
    result["telegram"] = sorted(tg)
    result["tech"] = {k: sorted(v) for k, v in result["tech"].items()}
    return result


# ФИО в русском написании: три слова с заглавных или два. Отчество
# необязательно — на сайтах его опускают чаще, чем пишут.
FIO_RE = re.compile(
    r"\b([А-ЯЁ][а-яё\-]{2,})\s+([А-ЯЁ][а-яё\-]{2,})(?:\s+([А-ЯЁ][а-яё\-]{3,}(?:вич|вна|ична)))?\b")


# Сколько строк со страницы вообще просматриваем и сколько людей ищем.
#
# Это не про точность, а про время. Страница на шестьдесят тысяч строк —
# это каталог товаров или лента новостей, а не «Команда»: руководство
# стоит в начале раздела, а не на пятой тысяче строки. Без предела
# разбор одной такой страницы занимал две секунды процессора, и на
# прогоне в полторы сотни компаний это восемь минут, потраченных ни на
# что — при том что нужных людей находилось столько же.
#
# Двенадцать человек — столько же, сколько функция и так отдавала в
# конце. Разница в том, что теперь она на двенадцатом останавливается, а
# не дочитывает страницу до конца ради результата, который всё равно
# будет отброшен.
MAX_LINES = 4000
MAX_PEOPLE = 12

# Сколько раз на одной странице вообще запускаем разбор окна.
#
# Цена обхода не в строках, а в этом: на каждую строку со словом
# «директор» разбираются семь соседних строк выражением для ФИО. Если
# слово стоит в каждой строке — а так выглядит лента вакансий или
# каталог курсов, — это десятки тысяч запусков выражения ради дюжины
# имён, которые всё равно найдутся в первых строках раздела.
#
# Триста окон — это заведомо больше, чем бывает на странице «Команда»
# даже у большой клиники, и в сто раз меньше, чем у ленты, притворяющейся
# такой страницей.
MAX_HITS = 300


def people(pages_text, window=3):
    """Люди с должностями, найденные на страницах сайта.

    Возвращает [{fio, post, boss}]. Ищем не «где-то есть фамилия», а пару
    «должность и имя рядом»: на странице «Команда» они всегда стоят
    вместе, а разрозненное имя в тексте новости — это не сотрудник.

    Порядок в паре любой: и «Иванов Иван, генеральный директор», и
    «Генеральный директор — Иванов Иван» встречаются одинаково часто.

    Имя достаётся ближайшей должности, а не первой попавшейся. На
    странице «Команда» должности идут подряд:

        Генеральный директор
        Иванов Иван Иванович
        Главный врач
        Сидорова Мария Сергеевна

    Окно в три строки вокруг «генерального директора» накрывает и
    Сидорову, и раньше она тоже становилась генеральным директором —
    просто потому, что её строка попала в чужое окно первой. Для списка
    на обзвон это худшая из возможных ошибок: человеку звонят, называя
    чужую должность.
    """
    out, seen = [], set()
    for lines in pages_text:
        # Строка вместо списка строк — это перебор страницы по буквам:
        # сотни тысяч холостых проверок и ноль находок. Случалось при
        # вызове не с тем, что ждали, и не давало о себе знать ничем,
        # кроме потраченного времени.
        if isinstance(lines, str):
            continue
        lines = lines[:MAX_LINES]

        # Сначала — где какие должности.
        posts = []
        for i, line in enumerate(lines):
            low = line.lower()
            # Самое длинное совпадение, а не первое: «коммерческий
            # директор» точнее «директора», и знать надо именно его.
            post = max((p for p in BOSS_POSTS if p in low), key=len,
                       default="")
            if not post:
                continue
            posts.append((i, post))
            if len(posts) >= MAX_HITS:
                break
        if not posts:
            continue
        at = [i for i, _ in posts]

        # Потом — имена в окрестностях этих строк, по одному разбору на
        # строку. Раньше строка, попавшая в несколько окон сразу,
        # разбиралась столько же раз: на плотной странице это
        # семикратная работа ради того же самого ответа.
        look = set()
        for i, _ in posts:
            look.update(range(max(0, i - window),
                              min(len(lines), i + window + 1)))
        found = {}
        for j in sorted(look):
            for m in FIO_RE.finditer(lines[j]):
                a, b, c = m.group(1), m.group(2), m.group(3) or ""
                fio = " ".join(x for x in (a, b, c) if x)
                # Должность, набранная с заглавных, сама попадает под
                # выражение для ФИО: «Генеральный Директор» — не имя.
                if any(w in fio.lower() for w in BOSS_POSTS):
                    continue
                k = bisect.bisect_left(at, j)
                near = min((x for x in (k - 1, k) if 0 <= x < len(at)),
                           key=lambda x: abs(at[x] - j), default=None)
                if near is None:
                    continue
                gap = abs(at[near] - j)
                if gap > window:
                    continue
                found.setdefault(near, []).append((gap, j, fio))

        # Одна должность — один человек, тот, кто к ней ближе.
        #
        # Окно в три строки накрывает и соседа по списку: «Менеджер
        # Пупкин», стоящий через строку от «руководителя отдела продаж»,
        # получал его должность. Берём ближайшего, а при равном
        # расстоянии — всех: «Директора: Иванов, Петров» — это одна
        # строка и два настоящих директора.
        for near in sorted(found):
            best = min(g for g, _j, _f in found[near])
            for gap, _j, fio in found[near]:
                if gap != best:
                    continue
                key = fio.lower()
                if key in seen:
                    continue
                seen.add(key)
                post = posts[near][1]
                out.append({"fio": fio, "post": post, "boss": post in BOSS})
                if len(out) >= MAX_PEOPLE:
                    return out
    return out[:MAX_PEOPLE]


def guess_owner(addr):
    """Кому принадлежит ящик — по локальной части."""
    local = addr.split("@")[0].lower()
    if local in ("hr", "job", "jobs", "career", "rabota", "vacancy"):
        return "hr"
    if local in ("sales", "zakaz", "order", "orders", "opt", "shop", "manager"):
        return "sales"
    if local in ("director", "boss", "ceo", "gd"):
        return "director"
    from ..enrich import GENERIC
    if local in GENERIC:
        return "general"
    return "unknown"


def near_person(pages_text, fio, window=6):
    """Контакты рядом с упоминанием фамилии на странице.

    Так устроены разделы «Руководство» и «Контакты»: имя, должность и тут
    же телефон с почтой. Поиск по соседству находит прямой контакт там,
    где по имени ящика его не опознать — например, gd@company.ru или
    личный мобильный в подписи.

    Окно намеренно узкое. Расширишь — и на странице «Контакты» с пятью
    отделами к фамилии директора прицепится телефон отдела продаж.
    """
    from ..enrich import surname_forms
    forms = {f for f in surname_forms(fio)}
    parts = [p for p in re.split(r"[\s,]+", (fio or "").strip()) if p]
    if parts:
        forms.add(parts[0].lower())        # кириллическая фамилия
    if not forms:
        return {"emails": [], "phones": []}

    emails, phones = [], []
    for lines in pages_text:
        if isinstance(lines, str):
            continue
        hits = 0
        for i, line in enumerate(lines[:MAX_LINES]):
            low = line.lower()
            if not any(f in low for f in forms):
                continue
            hits += 1
            if hits > MAX_HITS:
                break
            lo, hi = max(0, i - window), min(len(lines), i + window + 1)
            chunk = "\n".join(lines[lo:hi])
            for m in EMAIL_RE.findall(chunk):
                a = _clean_email(m)
                if a and a not in emails:
                    emails.append(a)
            for m in PHONE_RE.findall(chunk):
                p = _clean_phone(m)
                if p and p not in phones:
                    phones.append(p)
    return {"emails": emails[:4], "phones": phones[:4]}
