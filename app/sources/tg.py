# -*- coding: utf-8 -*-
"""Есть ли у номера аккаунт в Telegram.

Зачем. Номер из справочника — это приёмная, и там сидит секретарь. Тот
же номер в Telegram — это переписка, которую руководитель читает сам, и
у неё другая судьба: письмо в общий ящик теряется, звонок отбивают, а
сообщение доходит. Поэтому знать, какие из собранных номеров заведены в
Telegram, полезнее, чем кажется на первый взгляд.

Как. Единственный способ узнать это — спросить у самого Telegram через
contacts.importContacts: номера загружаются как контакты, в ответ
приходят те, что зарегистрированы. Библиотека — Telethon, официальный
клиент MTProto на Python.

Чем за это платят, честно и заранее:

  • нужен настоящий аккаунт. api_id и api_hash берутся на my.telegram.org
    и привязаны к номеру, вход — по коду из Telegram. Ключа «для
    разработчиков», как у 2ГИС, здесь нет вовсе;
  • Telegram считает массовый импорт контактов злоупотреблением и
    ограничивает аккаунты, которые им занимаются. Поэтому маленькие
    пачки, паузы между ними, дневной предел и остановка по первому же
    FloodWait — не вежливость, а условие того, что аккаунт доживёт до
    завтра;
  • кто закрыл «кто может найти меня по номеру телефона», не найдётся,
    даже если аккаунт у него есть. Ответ «нет» здесь означает «не
    нашёлся», а не «не существует», и подписан он именно так;
  • импортированные контакты сразу удаляются: незачем засорять адресную
    книгу живого человека сотнями чужих номеров.

Без входа модуль не делает ничего и говорит об этом словами.
"""
import asyncio
import json
import os
import re
import shutil

# Пачка маленькая намеренно. Импорт контактов — самое подозрительное,
# что можно делать с аккаунтом, и тысяча номеров одним запросом отличает
# перебор от человека вернее любого другого признака.
BATCH = 5
PAUSE = 4.0
# Предел на сутки. Точного порога Telegram не публикует, и он разный у
# старого и свежего аккаунта; двести — величина, после которой жалобы на
# ограничения начинаются массово, так что берём её с запасом вниз.
DAY_LIMIT = 150

SESSION_FILE = "telegram.session"


def available():
    """Установлена ли библиотека.

    В сборку она входит, но программу запускают и из исходников — там
    её может не быть, и падать с ImportError посреди задачи незачем.
    """
    try:
        import telethon  # noqa: F401
        return True
    except Exception:
        return False


def session_path(data_dir):
    return os.path.join(data_dir, SESSION_FILE)


def logged_in(data_dir):
    """Файл сеанса на месте — значит, вход когда-то был.

    Это не проверка по сети: она стоит запроса и секунд ожидания, а
    ответ нужен при отрисовке страницы. Действительность сеанса
    выясняется при первой же задаче.
    """
    return os.path.exists(session_path(data_dir))


def forget(data_dir):
    """Забыть аккаунт — удалить файл сеанса."""
    path = session_path(data_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except OSError:
        return False


# ── Готовый аккаунт ──────────────────────────────────────
#
# Входить по коду приходится не всем: аккаунт чаще получают уже
# заведённым — строкой сессии или связкой «JSON плюс файл .session».
# Имена полей в этих JSON не стандартизованы никем, поэтому у каждого
# значения несколько написаний, и все они встречаются вживую.
FIELD_NAMES = {
    "api_id": ("api_id", "app_id", "apiId", "appId", "api-id"),
    "api_hash": ("api_hash", "app_hash", "apiHash", "appHash", "api-hash"),
    "phone": ("phone", "phone_number", "number", "phoneNumber"),
    "password": ("twoFA", "two_fa", "2fa", "twofa", "password"),
    "session": ("session", "session_string", "string_session",
                "stringSession", "sessionString"),
}
# Признаки устройства. Аккаунт, заведённый «телефоном», а продолженный
# «компьютером с другой версией приложения», Telegram нередко
# разлогинивает — для него это выглядит как угон. Поэтому то, что
# пришло в JSON, переносится как есть, а не заменяется своим.
DEVICE_NAMES = {
    "device_model": ("device", "device_model", "deviceModel"),
    "system_version": ("sdk", "system_version", "systemVersion"),
    "app_version": ("app_version", "appVersion"),
    "lang_code": ("lang_code", "lang_pack", "langCode"),
    "system_lang_code": ("system_lang_code", "system_lang_pack",
                         "systemLangCode"),
}

# Строка сессии Telethon: версия «1» и дальше base64. Ни на JSON, ни на
# номер телефона это не похоже, так что различить их можно молча.
SESSION_RE = re.compile(r"^1[A-Za-z0-9+/=_-]{80,}$")


def _pick(data, names):
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return ""


def parse_account(text):
    """Разобрать вставленный аккаунт: JSON или строку сессии.

    Ничего не сохраняет и не ходит в сеть — только понимает, что ему
    дали. Отдельно, потому что именно здесь ошибаются: формат приходит
    от продавца, а не от нас, и проверять разбор надо без аккаунта.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Пусто — вставьте JSON или строку сессии"}

    if SESSION_RE.match(text):
        return {"ok": True, "session": text, "api_id": "", "api_hash": "",
                "phone": "", "password": "", "device": {}}

    if not text.startswith(("{", "[")):
        return {"ok": False,
                "error": "Не похоже ни на JSON, ни на строку сессии. Строка "
                         "сессии начинается с единицы и идёт одним куском "
                         "без пробелов"}
    try:
        data = json.loads(text)
    except ValueError as e:
        return {"ok": False, "error": "JSON не читается: %s" % str(e)[:120]}
    if isinstance(data, list):
        data = next((x for x in data if isinstance(x, dict)), None)
    if not isinstance(data, dict):
        return {"ok": False, "error": "В JSON нет объекта с полями аккаунта"}

    out = {"ok": True, "device": {}}
    for key, names in FIELD_NAMES.items():
        out[key] = str(_pick(data, names) or "").strip()
    for key, names in DEVICE_NAMES.items():
        value = str(_pick(data, names) or "").strip()
        if value:
            out["device"][key] = value
    if not (out["session"] or out["api_id"] or out["api_hash"]):
        return {"ok": False,
                "error": "В JSON нет ни api_id с api_hash, ни строки сессии"}
    return out


def session_fault(path):
    """Чего не хватает файлу, чтобы быть сессией Telethon.

    Подписи SQLite мало: база бывает и чужая. Внутри сессии лежит
    таблица sessions с ключом авторизации — без неё файл откроется, а
    упадёт потом, посреди проверки номеров, словами про «no such
    table». Лучше сказать это сразу и по-человечески.
    """
    import sqlite3
    try:
        con = sqlite3.connect(path)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "sessions" not in names:
                return ("внутри нет таблицы sessions — это база SQLite, но "
                        "не сессия Telethon")
            row = con.execute("SELECT auth_key FROM sessions "
                              "LIMIT 1").fetchone()
            if not row or not row[0]:
                return "сессия пустая: в ней нет ключа авторизации"
        finally:
            con.close()
    except Exception as e:
        return "файл не читается как база: %s" % str(e)[:100]
    return ""


def save_session_bytes(data_dir, raw, path=None):
    """Положить готовый файл .session на место нашего.

    Файл сессии Telethon — это база SQLite особого вида. Чужой файл
    прошёл бы молча и упал бы только при первой проверке номеров,
    поэтому смотрим внутрь сразу.
    """
    if not raw:
        return {"ok": False, "error": "Файл пустой"}
    if not raw.startswith(b"SQLite format 3"):
        return {"ok": False,
                "error": "Это не файл сессии Telethon. Он выглядит как база "
                         "SQLite; файлы tdata от настольного Telegram не "
                         "подходят"}
    path = path or session_path(data_dir)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
    except OSError as e:
        return {"ok": False, "error": "Не удалось записать файл: %s" % e}
    fault = session_fault(tmp)
    if fault:
        # Прежнюю сессию не трогаем: неудачный импорт не должен
        # отбирать рабочий аккаунт.
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "error": "Это не файл сессии Telethon: %s" % fault}
    try:
        shutil.move(tmp, path)
    except OSError as e:
        return {"ok": False, "error": "Не удалось записать файл: %s" % e}
    return {"ok": True}


def import_session(conf, session_string):
    """Превратить строку сессии в файл сессии.

    Хранить строку в базе незачем: она и есть ключ от аккаунта, а файл
    лежит рядом с остальными данными и удаляется одной кнопкой.
    """
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}
    session_string = (session_string or "").strip()
    if not SESSION_RE.match(session_string):
        return {"ok": False, "error": "Строка сессии не похожа на строку "
                                      "сессии Telethon"}

    async def go():
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(session_string),
                                *_keys(conf), **_opts(conf))
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {"ok": False,
                        "error": "Сессия не авторизована: аккаунт разлогинен "
                                 "или заблокирован"}
            who = _who(await client.get_me())
            # Переносим уже проверенную сессию в файл — ровно тот, с
            # которым потом работает проверка номеров.
            await client.disconnect()
            client2 = TelegramClient(session_file(conf),
                                     *_keys(conf), **_opts(conf))
            try:
                client2.session.set_dc(*_dc(client.session))
                client2.session.auth_key = client.session.auth_key
                client2.session.save()
            finally:
                # Закрыть обязательно: файл сессии — база SQLite, и
                # открытое соединение держит файл. На Windows это
                # означает, что «Забыть аккаунт» его не удалит, а на
                # каждый импорт остаётся лишний дескриптор.
                try:
                    client2.session.close()
                except Exception:
                    pass
            return {"ok": True, "who": who}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    return _guard(go)


def _dc(session):
    return session.dc_id, session.server_address, session.port


# ── Номера ───────────────────────────────────────────────
def to_e164(raw, country="7"):
    """Номер в том виде, в каком его понимает Telegram.

    В базе номера лежат так, как их написали на сайте: «8 (495)
    123-45-67», «+7 495 1234567», «495 123-45-67». Telegram принимает
    только цифры с кодом страны.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if len(digits) == 11 and digits[0] in ("8", "7") and country == "7":
        digits = "7" + digits[1:]
    elif len(digits) == 10 and country == "7":
        digits = "7" + digits
    elif len(digits) < 10 or len(digits) > 15:
        # Внутренний четырёхзначный, обрывок или что-то совсем чужое.
        return ""
    return "+" + digits


def kind_of(e164):
    """Мобильный, бесплатный или городской.

    Деление нужно ради экономии: дневной предел мал, а у номера 8-800
    аккаунта в Telegram не бывает по устройству — это не телефон, а
    маршрут в колл-центр. Городской изредка заведён, но редко, и тратить
    на него очередь стоит только по отдельной просьбе.
    """
    if not e164.startswith("+7") or len(e164) != 12:
        return "другой"
    code = e164[2:5]
    if code == "800":
        return "бесплатный"
    if code.startswith("9"):
        return "мобильный"
    return "городской"


def worth_checking(e164, landlines=False):
    kind = kind_of(e164)
    if kind == "бесплатный":
        return False
    if kind == "городской":
        return landlines
    # Мобильный и любой неопознанный (в том числе зарубежный) проверяем:
    # ошибиться в сторону лишнего запроса дешевле, чем пропустить.
    return True


def prepare(numbers, landlines=False, limit=DAY_LIMIT):
    """Что из списка вообще имеет смысл спрашивать.

    Возвращает [(исходный номер, номер для Telegram)] без повторов:
    один и тот же телефон встречается у нескольких компаний, и платить
    за него дважды незачем.
    """
    out, seen = [], set()
    for raw in numbers:
        e164 = to_e164(raw)
        if not e164 or e164 in seen:
            continue
        if not worth_checking(e164, landlines):
            continue
        seen.add(e164)
        out.append((raw, e164))
        if len(out) >= limit:
            break
    return out


# ── Асинхронное в синхронном ─────────────────────────────
# Общий предел на операцию. Таймаутов внутри Telethon недостаточно:
# при живом файле сессии и закрытой сети он честно соединяется,
# перебирает адреса и повторяет запрос — измерено, больше двух минут
# без единого слова на экране. Здесь стоит будильник поверх всего.
DEADLINE = 45


def _run(coro, deadline=DEADLINE):
    """Telethon живёт на asyncio, задачи программы — на потоке.

    Свой цикл на каждый вызов, а не общий на процесс: общий пришлось бы
    держать живым между задачами и чинить после каждого разрыва связи.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        if deadline:
            coro = asyncio.wait_for(coro, timeout=deadline)
        return loop.run_until_complete(coro)
    finally:
        # Telethon держит свои петли отправки и приёма отдельными
        # задачами. Закрыть цикл, пока они живы, — значит получить
        # полотно трассировок «Event loop is closed» на ровном месте,
        # особенно после срабатывания будильника. Поэтому сначала
        # отменяем и дожидаемся, потом закрываем.
        try:
            rest = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in rest:
                task.cancel()
            if rest:
                loop.run_until_complete(
                    asyncio.gather(*rest, return_exceptions=True))
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


class BadKeys(Exception):
    """api_id — число, и только число. Вставленный вместе с подписью
    или перепутанный с api_hash, он до сих пор доходил до человека
    трассировкой про int()."""


class BadProxy(Exception):
    """Адрес прокси разобрать не вышло."""


# ── Прокси ───────────────────────────────────────────────
#
# Свой, отдельный от общего прокси программы: к Telegram ходят не
# запросами HTTP, а своим протоколом на 443 и 80 порт, и провайдеры
# режут его отдельно от всего остального. К тому же аккаунт, купленный
# «под страну», с домашнего адреса разлогинивают заметно охотнее.
PROXY_KINDS = ("socks5", "socks4", "http", "https", "mtproto", "mtproxy")


def make_proxy(url):
    """Адрес прокси → то, что понимает Telethon.

    Возвращает {"kind": …, "proxy": …} или None, если прокси не задан.
    Разбор отдельно от подключения: опечатка в адресе должна быть видна
    сразу, а не через таймаут в тридцать секунд.
    """
    url = (url or "").strip()
    if not url:
        return None
    m = re.match(r"^(\w+)://(.*)$", url)
    if not m:
        raise BadProxy("Не хватает схемы: socks5://, http:// или mtproto://")
    kind, rest = m.group(1).lower(), m.group(2)
    if kind not in PROXY_KINDS:
        raise BadProxy("Неизвестный вид прокси «%s». Бывают: %s"
                       % (kind, ", ".join(PROXY_KINDS)))

    user = password = ""
    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
        user, _, password = creds.partition(":")
    host, _, tail = rest.partition(":")
    port, _, path = tail.partition("/")
    if not host or not port.isdigit():
        raise BadProxy("Нужны адрес и порт: socks5://логин:пароль@адрес:порт")

    if kind in ("mtproto", "mtproxy"):
        # У MTProto вместо логина и пароля один секрет. Его пишут то
        # после порта, то на месте логина — принимаем оба написания.
        secret = (path or user or password).strip()
        if not secret:
            raise BadProxy("У MTProto-прокси нужен секрет: "
                           "mtproto://адрес:порт/секрет")
        return {"kind": "mtproto", "proxy": (host, int(port), secret)}

    import socks
    code = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4,
            "http": socks.HTTP, "https": socks.HTTP}[kind]
    return {"kind": kind,
            "proxy": (code, host, int(port), True, user or None,
                      password or None)}


def normalize_proxy(line, kind="socks5"):
    """Строка прокси от продавца → адрес со схемой.

    Продавцы отдают прокси по-разному: «адрес:порт:логин:пароль»,
    «логин:пароль@адрес:порт», просто «адрес:порт». Схему в таких
    строках не пишут — её выбирает человек одним полем для всего списка.
    """
    line = (line or "").strip()
    if not line or "://" in line:
        return line
    kind = kind if kind in PROXY_KINDS else "socks5"
    if "@" in line:
        return "%s://%s" % (kind, line)
    parts = line.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        host, port, user, password = parts
        return "%s://%s:%s@%s:%s" % (kind, user, password, host, port)
    if len(parts) == 4 and parts[3].isdigit():
        user, password, host, port = parts
        return "%s://%s:%s@%s:%s" % (kind, user, password, host, port)
    return "%s://%s" % (kind, line)


def label_proxy(url):
    """Адрес прокси без логина и пароля — для журнала и экрана.

    Пароль от прокси в тексте ошибки — та же утечка, что и ключ: его
    видно на скриншоте, который присылают в поддержку.
    """
    url = (url or "").strip()
    if not url:
        return ""
    return re.sub(r"://[^@/]*@", "://…@", url)


# ── Подключение ──────────────────────────────────────────
def conf(api_id="", api_hash="", data_dir="", proxy="", device=None,
         session="", account_id=None, label=""):
    """Всё, что нужно для подключения, одной связкой.

    session — путь к файлу сеанса этого аккаунта. Пусто — старое место
    рядом с базой (так было, пока аккаунт был один).
    """
    return {"api_id": api_id, "api_hash": api_hash, "data_dir": data_dir,
            "proxy": proxy, "device": device or {}, "session": session,
            "account_id": account_id, "label": label}


def conf_for(acc):
    """Связка для аккаунта из таблицы tg_accounts."""
    from .. import db, settings
    acc = acc or {}
    try:
        device = json.loads(acc.get("device") or "{}")
    except ValueError:
        device = {}
    return conf(api_id=acc.get("api_id") or "", api_hash=acc.get("api_hash") or "",
                data_dir=settings.data_dir(), proxy=acc.get("proxy") or "",
                device=device if isinstance(device, dict) else {},
                session=db.tg_session_path(acc) if acc.get("file") else "",
                account_id=acc.get("id"),
                label=acc.get("label") or acc.get("phone") or "")


def conf_from_db():
    """Связка основного аккаунта — того, которым проверяются номера."""
    from .. import db
    return conf_for(db.tg_active())


def session_file(c):
    return (c or {}).get("session") or session_path((c or {}).get("data_dir", ""))


def has_session(c):
    """Есть ли у аккаунта файл сеанса. Без сети — см. logged_in."""
    return bool((c or {}).get("api_id")) and os.path.exists(session_file(c))


# Что значит ответ Telegram для аккаунта — одним словом, для списка.
STATUS_RU = {"unknown": "не проверен", "ok": "работает",
             "unauthorized": "разлогинен", "banned": "заблокирован",
             "proxy_error": "прокси не работает", "error": "ошибка"}


def status_of(res):
    """Ответ whoami → статус аккаунта."""
    if (res or {}).get("ok"):
        return "ok"
    err = str((res or {}).get("error") or "").lower()
    if "прокси" in err or "proxy" in err:
        return "proxy_error"
    if ("заблокирован" in err or "deactivated" in err or "banned" in err
            or "удалён" in err):
        return "banned"
    if ("не действует" in err or "не выполнен" in err or "разлогин" in err
            or "unauthorized" in err or "auth key" in err):
        return "unauthorized"
    return "error"


def _keys(c):
    try:
        api_id = int(str(c.get("api_id") or "").strip())
    except (TypeError, ValueError):
        raise BadKeys()
    api_hash = str(c.get("api_hash") or "").strip()
    if not api_hash:
        raise BadKeys()
    return api_id, api_hash


# Сколько ждать соединения. По умолчанию Telethon пробует пять раз с
# растущей паузой, и при неверном прокси или закрытом доступе окно
# висит минутами без единого слова на экране. Измерено: с настройками
# по умолчанию отказ приходил через минуту с лишним, с этими —
# вчетверо быстрее, и это тот срок, который успеваешь дождаться.
CONNECT = {"connection_retries": 1, "retry_delay": 1,
           "timeout": 12, "request_retries": 2}


def _opts(c):
    """Необязательные части подключения: прокси и признаки устройства."""
    out = dict(CONNECT)
    got = make_proxy(c.get("proxy") or "")
    if got:
        out["proxy"] = got["proxy"]
        if got["kind"] == "mtproto":
            from telethon.network import \
                ConnectionTcpMTProxyRandomizedIntermediate as MT
            out["connection"] = MT
    for key, value in (c.get("device") or {}).items():
        if key in DEVICE_NAMES and value:
            out[key] = value
    return out


async def _client(c, path=None):
    from telethon import TelegramClient
    client = TelegramClient(path or session_file(c),
                            *_keys(c), **_opts(c))
    await client.connect()
    return client


# Хэш кода живёт между двумя запросами страницы: код приходит в Telegram
# отдельно, и вводят его вторым шагом. Хранить его в базе незачем — он
# действителен минуты.
_code_hash = {}


def send_code(c, phone):
    """Первый шаг входа: попросить Telegram прислать код."""
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}

    async def go():
        client = await _client(c)
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {"ok": True, "done": True, "who": _who(me)}
            res = await client.send_code_request(phone)
            _code_hash[phone] = res.phone_code_hash
            return {"ok": True, "done": False}
        finally:
            await client.disconnect()

    return _guard(go)


def sign_in(c, phone, code="", password=""):
    """Второй шаг: код из Telegram, при двухэтапной проверке — пароль."""
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}

    async def go():
        from telethon.errors import SessionPasswordNeededError
        client = await _client(c)
        try:
            if password:
                await client.sign_in(password=password)
            else:
                try:
                    await client.sign_in(
                        phone=phone, code=code,
                        phone_code_hash=_code_hash.get(phone))
                except SessionPasswordNeededError:
                    return {"ok": False, "need_password": True,
                            "error": "Включена двухэтапная проверка — "
                                     "нужен пароль облака"}
            me = await client.get_me()
            return {"ok": True, "who": _who(me)}
        finally:
            await client.disconnect()

    return _guard(go)


def whoami(c):
    """Под кем мы вошли. Нужен, чтобы человек видел, чей аккаунт рискует."""
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}
    if not os.path.exists(session_file(c)):
        return {"ok": False, "error": "Вход не выполнен"}

    async def go():
        client = await _client(c)
        try:
            if not await client.is_user_authorized():
                return {"ok": False, "error": "Сеанс больше не действует"}
            return {"ok": True, "who": _who(await client.get_me())}
        finally:
            await client.disconnect()

    return _guard(go)


def _who(me):
    if not me:
        return ""
    name = " ".join(x for x in (getattr(me, "first_name", ""),
                                getattr(me, "last_name", "")) if x)
    tag = getattr(me, "username", "")
    return ("%s%s" % (name, " · @" + tag if tag else "")).strip() or "аккаунт"


def _guard(make_coro, deadline=DEADLINE):
    """Ошибки Telegram — словами, а не трассировкой.

    Их немного, и каждая означает для человека своё действие: подождать,
    ввести другой код, сменить номер. Общее «что-то пошло не так» не
    говорит ни одного из них.
    """
    try:
        return _run(make_coro(), deadline=deadline)
    except Exception as e:
        return {"ok": False, "error": explain(e)}


def explain(e):
    name = type(e).__name__
    text = str(e)
    if isinstance(e, BadProxy):
        return "Прокси: %s" % text
    if isinstance(e, BadKeys):
        return ("api_id — это число, api_hash — строка. Проверьте, что "
                "не перепутали их местами: оба лежат рядом на "
                "my.telegram.org")
    if name == "FloodWaitError":
        return ("Telegram просит подождать %s секунд — слишком частые "
                "запросы" % getattr(e, "seconds", "?"))
    if name in ("PhoneNumberBannedError", "UserDeactivatedBanError"):
        return "Этот номер заблокирован в Telegram"
    if name == "PhoneCodeInvalidError":
        return "Код неверный"
    if name == "PhoneCodeExpiredError":
        return "Код устарел — запросите новый"
    if name == "PasswordHashInvalidError":
        return "Пароль облака неверный"
    if name in ("ApiIdInvalidError", "ApiIdPublishedFloodError"):
        return "api_id или api_hash не подходят — проверьте их на my.telegram.org"
    if name == "AuthKeyUnregisteredError":
        return "Сеанс больше не действует — войдите снова"
    if name in ("OperationalError", "DatabaseError"):
        return ("Файл сессии не читается — похоже, он повреждён или не от "
                "Telethon. Попробуйте импортировать заново")
    if name in ("ConnectionError", "TimeoutError", "OSError",
                "CancelledError", "IncompleteReadError", "gaierror"):
        return ("Не удалось соединиться с Telegram за %d секунд. Проверьте "
                "связь, а если задан прокси — его адрес и доступность"
                % DEADLINE)
    return "%s: %s" % (name, text[:200]) if text else name


# ── Сама проверка ────────────────────────────────────────
def read_batch(imported, users, batch):
    """Разобрать ответ Telegram на одну пачку.

    Вынесено отдельно и не знает про сеть: разбор ответа — единственное
    место, где легко ошибиться молча, и проверять его надо без аккаунта.
    """
    by_id = {}
    for u in users or []:
        by_id[getattr(u, "id", None)] = u
    out = {}
    for imp in imported or []:
        idx = getattr(imp, "client_id", None)
        if idx is None or not (0 <= idx < len(batch)):
            continue
        user = by_id.get(getattr(imp, "user_id", None))
        out[batch[idx]] = {
            "username": getattr(user, "username", "") or "",
            "name": _who(user) if user else "",
            "user_id": getattr(user, "id", 0) or 0,
        }
    return out


def check(c, pairs, on_log=None, should_stop=None,
          batch=BATCH, pause=PAUSE):
    """Проверить подготовленные номера.

    pairs — то, что вернул prepare(): [(как в базе, как для Telegram)].
    Возвращает {номер как в базе: {...}} только для найденных, плюс
    список проверенных и причину остановки, если она была.
    """
    result = {"ok": True, "found": {}, "checked": [], "stopped": ""}
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}
    if not os.path.exists(session_file(c)):
        return {"ok": False, "error": "Вход в Telegram не выполнен"}
    if not pairs:
        return result

    async def go():
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.contacts import (DeleteContactsRequest,
                                                    ImportContactsRequest)
        from telethon.tl.types import InputPhoneContact
        client = await _client(c)
        try:
            if not await client.is_user_authorized():
                return {"ok": False, "error": "Сеанс больше не действует — "
                                              "войдите снова"}
            for start in range(0, len(pairs), batch):
                if should_stop and should_stop():
                    result["stopped"] = "остановлено"
                    break
                chunk = pairs[start:start + batch]
                phones = [e164 for _, e164 in chunk]
                contacts = [
                    InputPhoneContact(client_id=i, phone=phone,
                                      first_name="n%d" % i, last_name="")
                    for i, phone in enumerate(phones)]
                try:
                    res = await client(ImportContactsRequest(contacts))
                except FloodWaitError as e:
                    # Дальше идти нельзя: следующий запрос приблизит не
                    # результат, а блокировку аккаунта.
                    result["stopped"] = ("Telegram просит подождать %s с — "
                                         "останавливаюсь" % e.seconds)
                    break
                hits = read_batch(getattr(res, "imported", None),
                                  getattr(res, "users", None), phones)
                back = {e164: raw for raw, e164 in chunk}
                for e164, info in hits.items():
                    result["found"][back[e164]] = info
                result["checked"].extend(raw for raw, _ in chunk)
                # Чужие номера в своей адресной книге — мусор, который
                # копится молча. Убираем сразу, пачкой.
                ids = [u for u in (getattr(res, "users", None) or [])]
                if ids:
                    try:
                        await client(DeleteContactsRequest(id=ids))
                    except Exception:
                        pass
                if on_log:
                    on_log("   Telegram: проверено %d из %d, найдено %d"
                           % (min(start + batch, len(pairs)), len(pairs),
                              len(result["found"])))
                if start + batch < len(pairs):
                    await asyncio.sleep(pause)
            return result
        finally:
            await client.disconnect()

    rounds = (len(pairs) + batch - 1) // batch
    try:
        return _run(go(), deadline=DEADLINE + rounds * (pause + 20))
    except Exception as e:
        return {"ok": False, "error": explain(e)}


def wait_hint(seconds):
    """Сколько ждать — человеческими словами."""
    seconds = int(seconds or 0)
    if seconds < 90:
        return "%d секунд" % seconds
    if seconds < 5400:
        return "%d минут" % round(seconds / 60.0)
    return "%d часов" % round(seconds / 3600.0)


__all__ = ["available", "logged_in", "forget", "send_code", "sign_in",
           "whoami", "check", "prepare", "to_e164", "kind_of",
           "worth_checking", "read_batch", "explain", "wait_hint",
           "parse_account", "save_session_bytes", "import_session",
           "make_proxy", "label_proxy", "conf", "conf_from_db",
           "BATCH", "PAUSE", "DAY_LIMIT"]
