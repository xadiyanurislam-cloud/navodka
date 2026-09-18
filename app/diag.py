# -*- coding: utf-8 -*-
"""Проверка источников.

Смысл в том, чтобы «ничего не находит» превращалось в конкретную строку.
Причин у пустого результата с десяток: нет интернета, корпоративный
прокси, отказ hh по запросу, не задан ключ, провайдер сменил ответ. Все
они выглядят одинаково, пока каждый источник не спросить отдельно и не
показать, что именно он ответил.
"""
import time

import requests

from . import db, settings
from .sources import fns, gis2


def _get(session, url, params=None, timeout=15):
    t0 = time.time()
    try:
        r = session.get(url, params=params, timeout=timeout)
        return r, int((time.time() - t0) * 1000), ""
    except requests.exceptions.SSLError as e:
        return None, 0, "ошибка TLS: %s" % str(e)[:160]
    except requests.exceptions.ProxyError as e:
        return None, 0, "прокси отклонил запрос: %s" % str(e)[:160]
    except requests.exceptions.ConnectTimeout:
        return None, 0, "сервер не ответил за %d с" % timeout
    except Exception as e:
        return None, 0, str(e)[:200]


def _row(name, ok, note, ms=0, hint=""):
    return {"name": name, "ok": ok, "note": note, "ms": ms, "hint": hint}


def run():
    """Проверяет всё по очереди и возвращает список строк для интерфейса."""
    from . import net
    s = requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT, "Accept": "application/json"})
    out = []

    # Первыми — сведения о самой сборке. Без них после обновления нельзя
    # понять, новая программа не работает или запускается старая.
    out.append(_row("Версия программы", True,
                    "%s — %s" % (settings.VERSION, settings.BUILD)))
    out.append(_row("Подмена отпечатка", net.HAVE_CURL,
                    "библиотека curl_cffi установлена" if net.HAVE_CURL
                    else "библиотеки curl_cffi нет",
                    hint="" if net.HAVE_CURL else
                         "Без неё hh отвечает 403 на любые заголовки. "
                         "Закройте программу, удалите папку .venv рядом с "
                         "ней и запустите Navodka.bat заново."))

    # 1. Интернет вообще. Без этой строки все остальные отказы читаются
    #    как «источник сломался», хотя сети нет вовсе.
    r, ms, err = _get(s, "https://api.hh.ru/areas/113", timeout=12)
    if err:
        out.append(_row("Интернет", False, err, hint=(
            "Похоже, выхода в сеть нет или он идёт через прокси, о котором "
            "программа не знает. Проверьте браузером: откройте api.hh.ru — "
            "должен появиться текст, а не ошибка.")))
        out.append(_row("hh.ru — поиск", False, "не проверялся — нет связи"))
    else:
        # Любой ответ означает, что связь есть: 403 — это уже разговор с
        # сервером, а не его отсутствие. Помечать отказ как «нет
        # интернета» значит уводить от настоящей причины.
        out.append(_row("Интернет", True,
                        "связь есть, ответ %s" % r.status_code, ms))

        # 2. hh.ru настоящим поисковым запросом, а не пингом: отказ
        #    приходит именно на поиск. Перебираем те же способы связи,
        #    что и рабочий поиск, и называем сработавший.
        from . import net
        tried, done = [], False
        for t in net.hh_transports():
            t0 = time.time()
            try:
                r = t.get("https://api.hh.ru/vacancies",
                          {"text": "менеджер по продажам", "area": "1",
                           "per_page": 1, "period": 30}, timeout=15)
            except Exception as e:
                tried.append("%s — не дозвонился" % t.name)
                continue
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 403:
                tried.append("%s — 403" % t.name)
                continue
            if r.status_code != 200:
                out.append(_row("hh.ru — поиск", False,
                                "%s: ответ %s — %s"
                                % (t.name, r.status_code,
                                   (getattr(r, "text", "") or "")[:160]), ms,
                                hint="Отклонён сам запрос, а не программа: "
                                     "уберите из текста кавычки, NOT и OR."))
                done = True
                break
            try:
                found = (r.json() or {}).get("found", 0)
            except Exception:
                found = 0
            out.append(_row("hh.ru — поиск", found > 0,
                            "%s · вакансий по пробному запросу: %s"
                            % (t.name, found), ms))
            done = True
            break
        if not done:
            out.append(_row("hh.ru — поиск", False,
                            "не подошёл ни один способ: " + "; ".join(tried),
                            hint=net.missing_note()))

    # 3. ФНС — без ключа, по известному ИНН Сбербанка.
    r, ms, err = _get(s, "https://bo.nalog.ru/nbo/organizations/search",
                      {"query": "7707083893", "page": 0})
    if err:
        out.append(_row("ФНС (отчётность)", False, err))
    else:
        try:
            n = len(r.json() if isinstance(r.json(), list)
                    else (r.json().get("content") or []))
        except Exception:
            n = 0
        out.append(_row("ФНС (отчётность)", r.status_code == 200 and n > 0,
                        "ответ %s, записей %d" % (r.status_code, n), ms))

    # 4. DaData — только если задан ключ.
    token = db.get_setting("dadata_token", "")
    if not token:
        out.append(_row("DaData (ЕГРЮЛ)", None, "ключ не задан", hint=(
            "Без него не будет ФИО руководителей. Бесплатный тариф на "
            "dadata.ru, ключ вставляется в «Настройки»."))) 
    else:
        t0 = time.time()
        try:
            rr = s.post("https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party",
                        json={"query": "сбербанк", "count": 1},
                        headers={"Authorization": "Token " + token,
                                 "Content-Type": "application/json"},
                        timeout=15)
            ms = int((time.time() - t0) * 1000)
            n = len((rr.json() or {}).get("suggestions") or [])
            out.append(_row("DaData (ЕГРЮЛ)", rr.status_code == 200 and n > 0,
                            "ответ %s, найдено %d" % (rr.status_code, n), ms,
                            hint="" if rr.status_code == 200 else
                                 "403 обычно значит неверный ключ, "
                                 "429 — кончился дневной лимит."))
        except Exception as e:
            out.append(_row("DaData (ЕГРЮЛ)", False, str(e)[:200]))

    # 5. 2ГИС — только если задан ключ.
    key = db.get_setting("gis_key", "")
    if not key:
        out.append(_row("2ГИС", None, "ключ не задан",
                        hint="Нужен только для поиска по справочнику."))
    else:
        items = gis2.search("стоматология", 32, key, pages=1, page_size=1, session=s)
        out.append(_row("2ГИС", bool(items),
                        "организаций в пробном поиске: %d" % len(items),
                        hint="" if items else "Ключ не принят или исчерпан лимит."))

    # 6. Госзакупки — по HTML, поэтому проверяем отдельно.
    r, ms, err = _get(s, "https://zakupki.gov.ru/epz/organization/search/results.html",
                      {"searchString": "7707083893", "pageNumber": 1}, timeout=20)
    if err:
        out.append(_row("Госзакупки", False, err,
                        hint="Источник необязательный: он включается "
                             "галочкой в обогащении."))
    else:
        out.append(_row("Госзакупки", r.status_code == 200,
                        "ответ %s" % r.status_code, ms))

    return out
