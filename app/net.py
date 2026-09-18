# -*- coding: utf-8 -*-
"""Сетевой слой для источников, которые стоят за защитой от ботов.

Зачем понадобился. hh.ru отвечал 403 forbidden независимо от того, чем
программа представлялась: и своим именем, и строкой Chrome. Значит, дело
не в User-Agent — отсекают раньше, по отпечатку TLS-рукопожатия. У
библиотеки requests он свой и ни на один браузер не похож, и никакие
заголовки этого не меняют: сервер видит несоответствие между «я Chrome»
в заголовке и совсем не хромовским рукопожатием.

Отсюда три слоя, от честного к запасному:

  1. Токен hh. Официальный путь: приложение регистрируется на dev.hh.ru,
     и запросы идут с ключом. Если токен задан — используется он.
  2. curl_cffi. Та же программа, но рукопожатие как у настоящего Chrome.
     Библиотека необязательная: нет — работаем дальше без неё.
  3. requests. Как было. Для источников без защиты этого достаточно.

Слой выбирается сам и сообщает, какой сработал: без этого «не работает»
снова превратится в гадание.
"""
import requests

from . import settings

try:
    from curl_cffi import requests as curl_requests
    HAVE_CURL = True
except Exception:                                    # pragma: no cover
    curl_requests = None
    HAVE_CURL = False

# Браузер, под который маскируется рукопожатие. Версия указана намеренно
# свежая: защита сверяет отпечаток со списком известных, и отпечаток
# трёхлетнего Chrome в нём уже подозрителен.
IMPERSONATE = "chrome"

# Полный набор заголовков настоящего браузера. Сам по себе он защиту не
# обходит, но без него запрос выглядит странно даже при верном
# рукопожатии: Chrome никогда не ходит с одним только User-Agent.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
}

APP_HEADERS = {
    "User-Agent": "Navodka/%s (lead research tool)" % settings.VERSION,
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class Transport(object):
    """Один способ сходить в сеть. Знает своё имя, чтобы его назвать."""

    def __init__(self, name, session, headers, note=""):
        self.name = name
        self.session = session
        self.note = note
        try:
            self.session.headers.update(headers)
        except Exception:
            pass

    def get(self, url, params=None, timeout=20):
        return self.session.get(url, params=params, timeout=timeout)


def hh_token():
    try:
        from . import db
        return (db.get_setting("hh_token", "") or "").strip()
    except Exception:
        return ""


def hh_transports():
    """Способы достучаться до hh, по порядку.

    Порядок — от самого законного к самому упрямому: сначала официальный
    токен, потом маскировка рукопожатия, в конце обычный requests.
    """
    out = []
    token = hh_token()

    if token:
        s = requests.Session()
        h = dict(APP_HEADERS, **{"Authorization": "Bearer " + token})
        out.append(Transport("токен hh.ru", s, h,
                             "официальное приложение с dev.hh.ru"))

    if HAVE_CURL:
        try:
            s = curl_requests.Session(impersonate=IMPERSONATE)
            out.append(Transport("curl_cffi (отпечаток Chrome)", s,
                                 BROWSER_HEADERS,
                                 "рукопожатие как у браузера"))
            if token:
                s2 = curl_requests.Session(impersonate=IMPERSONATE)
                out.insert(0, Transport(
                    "токен + отпечаток Chrome", s2,
                    dict(BROWSER_HEADERS, **{"Authorization": "Bearer " + token})))
        except Exception:
            pass

    s = requests.Session()
    out.append(Transport("requests (имя программы)", s, APP_HEADERS))
    s2 = requests.Session()
    out.append(Transport("requests (строка браузера)", s2, BROWSER_HEADERS))
    return out


def plain(browser=False):
    """Обычная сессия для источников без защиты."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS if browser else APP_HEADERS)
    return s


def missing_note():
    """Что подсказать, если ни один способ не сработал."""
    if HAVE_CURL:
        return ("Ни один способ не подошёл. Остаётся официальный путь: "
                "зарегистрируйте приложение на dev.hh.ru, получите токен и "
                "вставьте его в «Настройки» → «Токен hh.ru».")
    return ("Не установлена библиотека curl_cffi — без неё программа не "
            "умеет подделывать отпечаток браузера. Закройте программу и "
            "запустите Navodka.bat ещё раз: библиотека поставится сама.")
