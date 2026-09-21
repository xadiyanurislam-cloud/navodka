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
import os
import re

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
def _run(coro):
    """Telethon живёт на asyncio, задачи программы — на потоке.

    Свой цикл на каждый вызов, а не общий на процесс: общий пришлось бы
    держать живым между задачами и чинить после каждого разрыва связи.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
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


async def _client(api_id, api_hash, path):
    from telethon import TelegramClient
    try:
        api_id = int(str(api_id).strip())
    except (TypeError, ValueError):
        raise BadKeys()
    if not str(api_hash or "").strip():
        raise BadKeys()
    client = TelegramClient(path, api_id, str(api_hash).strip())
    await client.connect()
    return client


# Хэш кода живёт между двумя запросами страницы: код приходит в Telegram
# отдельно, и вводят его вторым шагом. Хранить его в базе незачем — он
# действителен минуты.
_code_hash = {}


def send_code(api_id, api_hash, data_dir, phone):
    """Первый шаг входа: попросить Telegram прислать код."""
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}

    async def go():
        client = await _client(api_id, api_hash, session_path(data_dir))
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


def sign_in(api_id, api_hash, data_dir, phone, code="", password=""):
    """Второй шаг: код из Telegram, при двухэтапной проверке — пароль."""
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}

    async def go():
        from telethon.errors import SessionPasswordNeededError
        client = await _client(api_id, api_hash, session_path(data_dir))
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


def whoami(api_id, api_hash, data_dir):
    """Под кем мы вошли. Нужен, чтобы человек видел, чей аккаунт рискует."""
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}
    if not logged_in(data_dir):
        return {"ok": False, "error": "Вход не выполнен"}

    async def go():
        client = await _client(api_id, api_hash, session_path(data_dir))
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


def _guard(make_coro):
    """Ошибки Telegram — словами, а не трассировкой.

    Их немного, и каждая означает для человека своё действие: подождать,
    ввести другой код, сменить номер. Общее «что-то пошло не так» не
    говорит ни одного из них.
    """
    try:
        return _run(make_coro())
    except Exception as e:
        return {"ok": False, "error": explain(e)}


def explain(e):
    name = type(e).__name__
    text = str(e)
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


def check(api_id, api_hash, data_dir, pairs, on_log=None, should_stop=None,
          batch=BATCH, pause=PAUSE):
    """Проверить подготовленные номера.

    pairs — то, что вернул prepare(): [(как в базе, как для Telegram)].
    Возвращает {номер как в базе: {...}} только для найденных, плюс
    список проверенных и причину остановки, если она была.
    """
    result = {"ok": True, "found": {}, "checked": [], "stopped": ""}
    if not available():
        return {"ok": False, "error": "Библиотека Telethon не установлена"}
    if not logged_in(data_dir):
        return {"ok": False, "error": "Вход в Telegram не выполнен"}
    if not pairs:
        return result

    async def go():
        from telethon.errors import FloodWaitError
        from telethon.tl.functions.contacts import (DeleteContactsRequest,
                                                    ImportContactsRequest)
        from telethon.tl.types import InputPhoneContact
        client = await _client(api_id, api_hash, session_path(data_dir))
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

    try:
        return _run(go())
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
           "BATCH", "PAUSE", "DAY_LIMIT"]
