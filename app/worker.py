# -*- coding: utf-8 -*-
"""Фоновый исполнитель задач.

Один поток и очередь в таблице tasks, а не пул воркеров. Причина простая:
узкое место здесь не процессор, а чужие серверы и паузы между запросами.
Десять потоков не ускорят обход, зато гарантированно приведут к блокировке
по IP — и вместо тысячи компаний мы получим ноль.

Состояние после каждого шага пишется в базу. Программу закроют посреди
обхода — это норма, и продолжить надо с того же места, а не с начала.
"""
import json
import re
import threading
import time

import requests

from . import ai, db, enrich, geo, profile, score, settings, social, verify
from .sources import (dadata, fns, gis2, hh, importer, osm,
                      site as site_src, vk, yandex, zakupki)

_thread = None
_stop = threading.Event()
_current = {"task_id": None}


# ── Запуск и остановка ───────────────────────────────────
def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="navodka-worker", daemon=True)
    _thread.start()


def stop_all():
    """Остановить текущую задачу. Уже сделанное остаётся в базе."""
    _stop.set()


def _should_stop():
    return _stop.is_set()


def _loop():
    while True:
        try:
            row = db.conn().execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
        except Exception:
            row = None
        if row is None:
            time.sleep(1.0)
            continue

        _stop.clear()
        _current["task_id"] = row["id"]
        db.update_task(row["id"], status="running", message="")
        try:
            params = json.loads(row["params"] or "{}")
            handler = HANDLERS.get(row["kind"])
            if handler is None:
                raise ValueError("неизвестная задача: %s" % row["kind"])
            handler(row["id"], params)
            status = "stopped" if _should_stop() else "done"
            db.update_task(row["id"], status=status)
        except Exception as e:
            db.log(row["id"], "Задача упала: %s" % e, "error")
            db.update_task(row["id"], status="error", message=str(e)[:500])
        finally:
            _current["task_id"] = None


# ── Задача: поиск компаний на hh.ru ──────────────────────
def task_hh_search(task_id, params):
    # Запросов может быть несколько. Один запрос за прогон — это ровно та
    # ситуация, когда человек ищет «менеджера по продажам», потом
    # «оператора колл-центра», потом РОПа, и каждый раз ждёт отдельно,
    # хотя обойти их можно одним проходом.
    queries = [q.strip() for q in (params.get("queries") or []) if q.strip()]
    if not queries:
        queries = [(params.get("text") or hh.PRESETS["Отдел продаж"]).strip()]
    areas = [str(a) for a in (params.get("areas") or []) if str(a).strip()]
    if not areas:
        areas = [str(params.get("area") or "113")]
    period = int(params.get("period") or 30)
    pages = int(params.get("pages") or 5)
    in_title = params.get("in_title", True)
    skip_agencies = params.get("skip_agencies", True)
    # Компании с сотней открытых вакансий — это сети, ритейл и заводы. Они
    # находятся первыми по любому запросу, занимают всю выдачу, а толку от
    # них меньше всего: до генерального там не дойти, закупки идут через
    # тендер. Ноль — порога нет.
    max_open = int(params.get("max_open") or 0)

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    errors = []
    queries_left = True
    employers, seen = [], set()
    for qi, text in enumerate(queries, 1):
        if not queries_left:
            break
        for area in areas:
            if _should_stop():
                break
            log("[%d/%d] Ищу: «%s», регион %s, за %d дней"
                % (qi, len(queries) * len(areas), text, area, period))
            before = len(errors)
            part = hh.search_employers(text, area=area, period=period, pages=pages,
                                       on_log=log, should_stop=_should_stop,
                                       errors=errors, in_title=in_title,
                                       skip_agencies=skip_agencies)
            if len(errors) > before and "403" in errors[-1]:
                # Отказ по отпечатку не зависит ни от запроса, ни от
                # региона: повторять его двенадцать раз — значит двенадцать
                # раз ждать впустую.
                log("hh отклоняет обращения — остальные запросы пропускаю.", "warn")
                queries_left = False
                break
            for emp in part:
                # Одна компания находится по нескольким запросам сразу —
                # это норма. Складываем вакансии, а не заводим дубль.
                if emp["id"] in seen:
                    old = next(x for x in employers if x["id"] == emp["id"])
                    old["vacancies"] += emp["vacancies"]
                    for t in emp.get("titles") or []:
                        if t not in old["titles"]:
                            old["titles"].append(t)
                    old["salaries"] = (old.get("salaries") or []) + (emp.get("salaries") or [])
                    if emp.get("fresh") is not None and \
                       (old.get("fresh") is None or emp["fresh"] < old["fresh"]):
                        old["fresh"] = emp["fresh"]
                    continue
                seen.add(emp["id"])
                employers.append(emp)
        if _should_stop():
            break
    employers.sort(key=lambda x: -x["vacancies"])
    if not employers:
        # Отказ источника — это не «готово». Раньше задача в обоих случаях
        # заканчивалась успехом, и человек видел бодрое «готово» при нуле
        # компаний, даже когда hh прямо ответил отказом. Причина при этом
        # лежала в журнале, куда никто не заглядывал.
        if errors:
            raise RuntimeError(errors[-1][:400])
        log("hh не нашёл ни одной вакансии по этому запросу. Попробуйте "
            "другую формулировку, более широкий регион или период.", "warn")
        return

    db.update_task(task_id, total=len(employers))
    log("Компаний найдено: %d. Забираю карточки работодателей." % len(employers))

    s = requests.Session()
    s.headers.update({"User-Agent": settings.USER_AGENT})
    added, known, skipped = 0, 0, 0
    for i, emp in enumerate(employers, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        # Отказавшие не возвращаются. Иначе каждый следующий поиск снова
        # приносит тех, с кем уже поговорили и получили «нет».
        if db.is_blacklisted({"hh_id": emp["id"], "name": emp["name"]}):
            skipped += 1
            db.update_task(task_id, done=i)
            continue
        detail = hh.employer_details(emp["id"], session=s)
        if max_open and (detail.get("open_vacancies") or 0) > max_open:
            log("   пропускаю %s: открытых вакансий %d — это сеть или завод"
                % (emp["name"], detail["open_vacancies"]))
            skipped += 1
            db.update_task(task_id, done=i)
            continue
        cid, is_new = db.upsert_company({
            "name": detail.get("name") or emp["name"],
            "hh_id": emp["id"],
            "site": site_src.normalize_url(detail.get("site") or ""),
            "region": detail.get("area") or emp.get("area") or "",
            "okved_name": detail.get("industries") or "",
            "source": "hh.ru",
        })
        added += 1 if is_new else 0
        known += 0 if is_new else 1
        db.add_signal(cid, "hh_vacancies", emp["vacancies"])
        db.add_signal(cid, "hh_url", detail.get("hh_url") or "")
        db.add_signal(cid, "hh_vacancy_example", emp.get("vacancy_name") or "")
        # Названия вакансий сохраняем целиком: по ним видно, кого именно
        # ищут — операторов на телефон или продавцов в зале.
        if emp.get("titles"):
            db.add_signal(cid, "hh_titles", " | ".join(emp["titles"][:8]))
        if detail.get("about"):
            db.add_signal(cid, "hh_about", detail["about"])
        if detail.get("open_vacancies"):
            db.add_signal(cid, "hh_open_all", detail["open_vacancies"])
        if emp.get("fresh") is not None:
            # Свежесть вакансии — это срок годности повода для звонка.
            db.add_signal(cid, "hh_fresh_days", emp["fresh"])
        sal = [x for x in (emp.get("salaries") or []) if 15000 < x < 1000000]
        if sal:
            db.add_signal(cid, "hh_salary", "%d–%d ₽" % (min(sal), max(sal)))
        _rescore(cid)
        db.update_task(task_id, done=i)
        time.sleep(0.35)

    total = db.conn().execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    # Повторный прогон без этих цифр выглядит холостым: компаний столько
    # же, и непонятно, нашлось ли что-нибудь новое.
    log("Готово. Новых: %d, уже было: %d%s. Всего в базе: %d"
        % (added, known,
           (", пропущено из чёрного списка: %d" % skipped) if skipped else "",
           total))
    if params.get("then_enrich") and added:
        # Поиск без обогащения — половина дела: в карточке одно название.
        # Ставим вторую задачу в очередь, чтобы не ждать у экрана.
        db.create_task("enrich", {
            "limit": min(500, added), "fns": True, "only_lpr": True,
            "zakupki": bool(params.get("then_zakupki")),
            # Цепочка идёт дальше сама: человек нажал одну кнопку и ушёл,
            # возвращаться к экрану ради второго и третьего нажатия он не
            # должен.
            "then_ai": bool(params.get("then_ai")),
        })
        log("Обогащение поставлено в очередь.")
    elif params.get("then_enrich") and not added:
        log("Новых компаний нет — обогащать нечего.", "warn")


# ── Задача: обогащение ───────────────────────────────────
def task_enrich(task_id, params):
    """ЕГРЮЛ → сайт → кандидаты в почту руководителя → проверка."""
    limit = int(params.get("limit") or 50)
    do_verify = bool(params.get("verify"))
    token = db.get_setting("dadata_token", "")

    # Строгий режим: сначала те, у кого известен руководитель. Смысл в
    # порядке — без ФИО из ЕГРЮЛ его контакт искать нечем, и тратить на
    # такие компании квоту обогащения впустую незачем.
    order = "director<>'' DESC, score DESC, id" if params.get("only_lpr") \
        else "score DESC, id"
    rows = db.conn().execute("""
        SELECT * FROM companies
        WHERE id NOT IN (SELECT company_id FROM signals WHERE key='enriched')
        ORDER BY %s LIMIT ?""" % order, (limit,)).fetchall()

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    if not rows:
        log("Все компании уже обработаны. Сначала выполните поиск.", "warn")
        return
    db.update_task(task_id, total=len(rows))
    if not token:
        log("Токен DaData не задан — ФИО руководителей взять неоткуда, "
            "соберу только контакты с сайтов.", "warn")

    http = requests.Session()
    # Сайты обходятся с опережением и в несколько потоков.
    #
    # Раньше всё шло строго по очереди: дождались ответа одного сайта —
    # пошли к следующему. Узкое место здесь не наш процессор, а чужие
    # серверы, и пока мы ждём ответа от одного, остальные простаивают.
    # Параллелить страницы одного сайта нельзя — это выглядит как
    # сканирование и кончается баном по IP; параллелить разные сайты
    # можно и нужно, каждый из них видит ровно тот же одиночный обход,
    # что и раньше.
    crawls = _Prefetch(rows, log, should_stop=_should_stop)
    for i, row in enumerate(rows, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        cid = row["id"]
        crawls.fill(i - 1)
        found_lpr = False        # контакт первого лица найден, а не выведен
        log("[%d/%d] %s" % (i, len(rows), row["name"]))

        # 1. ЕГРЮЛ: ФИО руководителя, ИНН, ОКВЭД, адрес.
        if token and not (row["director"] or "").strip():
            info = dadata.by_inn(row["inn"], token, session=http) if row["inn"] \
                else dadata.by_name(row["name"], token, session=http)
            info.pop("opf", None)
            # Ликвидируемая компания — не лид. Отметить это надо до того,
            # как продавец потратит на неё звонок.
            if info.get("status") and info["status"] != "ACTIVE":
                log("   ВНИМАНИЕ: статус в ЕГРЮЛ — %s" % info["status"], "warn")
            if info:
                db.upsert_company(dict(info, hh_id=row["hh_id"], source=row["source"]))
                row = db.conn().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
            else:
                log("   в ЕГРЮЛ по названию не нашлось", "warn")

        # 2. Сайт: почты, телефоны, телеграм, технографика.
        emails_found, res = [], {}
        if (row["site"] or "").strip():
            res = crawls.take(cid, row["site"])
            if res.get("skipped"):
                log("   сайт обходили на днях — беру, что уже есть в базе")
            if res.get("error"):
                log("   сайт не открылся: %s" % res["error"], "warn")
            for addr in res["emails"]:
                owner = site_src.guess_owner(addr)
                db.add_contact(cid, "email", addr, owner, 90, "unchecked", "сайт компании")
                emails_found.append(addr)
            for ph in res["phones"]:
                db.add_contact(cid, "phone", ph, "general", 90, "unchecked", "сайт компании")
            for tg in res["telegram"]:
                db.add_contact(cid, "telegram", "@" + tg, "general", 70, "unchecked", "сайт компании")

            # Соцсети компании — теми адресами, что она сама опубликовала.
            for net, title, url in social.as_links(res.get("socials") or {}):
                db.add_contact(cid, "social", url, "general", 85, "unchecked",
                               "сайт: %s" % title)
            for group, titles in (res.get("tech") or {}).items():
                db.add_signal(cid, "tech_" + group, ", ".join(titles))
            if res["pages"]:
                log("   страниц обойдено %d, почт %d, телефонов %d"
                    % (res["pages"], len(res["emails"]), len(res["phones"])))

            # Адрес руководителя, НАЙДЕННЫЙ на сайте, а не выведенный по
            # схеме. Это принципиально другая находка: ящик существует, мы
            # лишь опознали, чей он по фамилии из ЕГРЮЛ.
            if (row["director"] or "").strip():
                for addr in enrich.match_emails(row["director"], res["emails"]):
                    db.add_contact(cid, "email", addr, "director", 92,
                                   "unchecked", "сайт: фамилия в адресе")
                    found_lpr = True
                    log("   почта руководителя найдена на сайте: %s" % addr)

                # Контакты, стоящие рядом с фамилией на странице. Так
                # устроены разделы «Руководство»: имя, должность и тут же
                # телефон с почтой — по имени ящика такой не опознать.
                # Профили, опубликованные рядом с ФИО в разделе
                # «Руководство»: компания сама указала, как с ним связаться.
                for net, title, url in social.as_links(
                        social.near_person(res.get("text") or [], row["director"])):
                    db.add_contact(cid, "social", url, "director", 88,
                                   "unchecked", "рядом с ФИО: %s" % title)
                    found_lpr = True
                    log("   профиль руководителя на сайте: %s" % url)

                near = site_src.near_person(res.get("text") or [], row["director"])
                for addr in near["emails"]:
                    db.add_contact(cid, "email", addr, "director", 88,
                                   "unchecked", "рядом с ФИО на сайте")
                    found_lpr = True
                for ph in near["phones"]:
                    db.add_contact(cid, "phone", ph, "director", 88,
                                   "unchecked", "рядом с ФИО на сайте")
                    found_lpr = True
                if near["emails"] or near["phones"]:
                    log("   рядом с ФИО на странице: почт %d, телефонов %d"
                        % (len(near["emails"]), len(near["phones"])))

            # Кто указан на страницах «Команда» и «Руководство». Это
            # полезно и когда ФИО из ЕГРЮЛ уже есть (подтверждает, что
            # человек действующий), и особенно когда его нет: у ИП и у
            # филиалов в ЕГРЮЛ руководителя не найти, а на сайте он
            # представлен.
            crew = site_src.people(res.get("text") or [])
            bosses = [p for p in crew if p["boss"]]
            if bosses:
                db.add_signal(cid, "site_people", "; ".join(
                    "%s — %s" % (p["fio"], p["post"]) for p in bosses[:4]))
                known_fio = (row["director"] or "").strip()
                if not known_fio:
                    top = bosses[0]
                    db.update_company_fields(cid, {"director": top["fio"],
                                                   "director_post": top["post"]})
                    row = db.conn().execute("SELECT * FROM companies WHERE id=?",
                                            (cid,)).fetchone()
                    log("   руководитель со страницы сайта: %s — %s"
                        % (top["fio"], top["post"]))
                else:
                    same = [p for p in bosses
                            if enrich.same_person(known_fio, p["fio"])]
                    if same:
                        db.add_signal(cid, "director_on_site", "да")
                        log("   ФИО из ЕГРЮЛ подтверждено на сайте")

        # 3. Профиль: чем занимается и есть ли телефонные продажи.
        #    Второй вопрос важнее: компания, которая не продаёт по телефону,
        #    не купит ничего про звонки, сколько бы у неё ни было выручки.
        res = res if (row["site"] or "").strip() else {}
        titles = [t for t in (db.get_signal(cid, "hh_titles") or "").split(" | ") if t]
        found_signs = profile.call_signals(res, titles, res.get("phones") or [])
        cc = profile.verdict(found_signs)
        patch = {"callcenter": cc["label"]}
        if cc["why"]:
            db.add_signal(cid, "cc_why", ", ".join(cc["why"]))
        db.add_signal(cid, "cc_score", cc["score"])
        act = profile.activity(row, {"hh_about": db.get_signal(cid, "hh_about")}, res)
        if act:
            patch["activity"] = act
        if res.get("cms"):
            patch["cms"] = res["cms"]
        # Цифры, которые компания вынесла на главную. В разговоре на них
        # ссылаться удобнее, чем на отчётность: «вы пишете, что работаете
        # с 2011 года» звучит иначе, чем «по данным ФНС».
        for key, sig in (("self_year", "self_year"), ("self_staff", "self_staff"),
                         ("self_branches", "self_branches")):
            if res.get(key):
                db.add_signal(cid, sig, res[key])
        model = []
        if res.get("shop"):
            model.append("интернет-магазин")
        if res.get("prices"):
            model.append("цены на сайте")
        elif res.get("no_prices"):
            model.append("цена по запросу")
        if res.get("app"):
            model.append("мобильное приложение")
        if model:
            db.add_signal(cid, "sales_model", ", ".join(model))
        if res.get("last_post"):
            db.add_signal(cid, "last_post", res["last_post"])
        for num in (res.get("tollfree") or []):
            db.add_contact(cid, "phone", num, "general", 90, "unchecked",
                           "бесплатный номер с сайта")
        db.update_company_fields(cid, patch)
        row = db.conn().execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        log("   телефонные продажи: %s%s"
            % (cc["label"], (" — " + ", ".join(cc["why"][:3])) if cc["why"] else ""))

        # 4. Кандидаты в адрес руководителя по схеме домена.
        domain = ""
        if emails_found:
            domain = emails_found[0].split("@")[1]
        elif (row["site"] or ""):
            domain = row["site"].split("//")[-1].split("/")[0].replace("www.", "")

        cands = enrich.candidates(row["director"], domain, emails_found) if domain else []
        if cands:
            verdicts = {}
            if do_verify:
                verdicts = verify.check([a for a, _ in cands[:6]])
            for addr, conf in cands:
                v = verdicts.get(addr, "unchecked")
                # Домен-приёмник всего подряд: адрес не подтверждён, а просто
                # не опровергнут. Хранить его как найденный — самообман.
                if v == "bad":
                    continue
                db.add_contact(cid, "email", addr, "director",
                               conf if v != "ok" else 95, v, "выведен по схеме домена")
            log("   кандидатов в почту руководителя: %d" % len(cands))

        # 5. ФНС: выручка и размер. Отсеивает и микробизнес без бюджета,
        #    и корпорации с полугодовым согласованием — оба одинаково
        #    бесполезны, но по названию их не отличить.
        if row["inn"] and params.get("fns", True):
            fin = fns.by_inn(row["inn"], session=http)
            if fin.get("employees"):
                db.update_company_fields(cid, {"employees": fin["employees"]})
            if fin.get("revenue"):
                db.add_signal(cid, "revenue", fin["revenue"])
                db.add_signal(cid, "revenue_year", fin.get("year") or "")
                db.add_signal(cid, "profit", fin.get("profit") or 0)
                db.add_signal(cid, "size", fns.size_band(fin["revenue"]))
                # Ряд по годам — чтобы в карточке был не один столбик, а
                # линия: по ней видно, что с компанией происходит.
                series = fin.get("series") or []
                if series:
                    db.add_signal(cid, "revenue_series", ";".join(
                        "%s:%d" % (r["year"], r["revenue"]) for r in series))
                label, pct = fns.growth(series)
                if label:
                    db.update_company_fields(cid, {"growth": "%s %+d%%" % (label, pct)})
                log("   выручка %s: %.1f млн ₽ (%s)%s"
                    % (fin.get("year") or "—", fin["revenue"] / 1e6,
                       fns.size_band(fin["revenue"]),
                       (", " + label) if label else ""))

        # 5.5. ВКонтакте: контактные лица, которые компания указала сама.
        #      Это единственный честный способ выйти на личный профиль
        #      руководителя: не поиск по ФИО среди однофамильцев, а
        #      контакт, опубликованный самой компанией для связи.
        vk_token = db.get_setting("vk_token", "")
        if vk_token and params.get("vk", True):
            found_lpr = _vk_contacts(cid, row, vk_token, http, log) or found_lpr

        # 6. Госзакупки: контактное лицо с прямым телефоном и почтой. Это
        #    не общий ящик с сайта, а живой контакт конкретного человека.
        if row["inn"] and params.get("zakupki"):
            z = zakupki.contacts_by_inn(row["inn"], session=http)
            if z:
                # Если контактное лицо в закупках — тот же человек, что
                # руководитель в ЕГРЮЛ, это прямой рабочий контакт первого
                # лица. Сверяем по фамилии: отчество и инициалы в ЕИС
                # пишут как придётся.
                same = False
                forms = enrich.surname_forms(row["director"])
                person_last = (z.get("person") or "").split()
                if person_last and forms:
                    from app.enrich import translit as _tr
                    same = _tr(person_last[0]) in forms or \
                        _tr(person_last[0], alt=True) in forms
                who = "director" if same else "unknown"
                if same:
                    found_lpr = True
                    log("   контакт из закупок — это сам руководитель")
                if z.get("email"):
                    db.add_contact(cid, "email", z["email"], who, 95 if same else 92,
                                   "unchecked", "госзакупки")
                if z.get("phone"):
                    db.add_contact(cid, "phone", z["phone"], who, 95 if same else 92,
                                   "unchecked", "госзакупки")
                if z.get("person"):
                    db.add_signal(cid, "zakupki_person", z["person"])
                db.add_signal(cid, "zakupki_url", z.get("url", ""))
                log("   в закупках: %s" % (z.get("person") or "контакт найден"))

        db.add_signal(cid, "enriched", int(time.time()))
        _rescore(cid)
        db.update_task(task_id, done=i)

    crawls.close()
    got = db.conn().execute(
        "SELECT COUNT(*) n FROM signals WHERE key='lpr_contact' AND value='найден'"
    ).fetchone()["n"]
    log("Обогащение завершено. Компаний с найденным контактом ГД: %d" % got)

    if params.get("then_ai"):
        if not db.get_setting("ai_key", ""):
            log("Ключ ИИ не задан — разбор пропущен.", "warn")
        else:
            db.create_task("ai", {
                "limit": min(300, len(rows)),
                "icp": db.get_setting("ai_icp", ""),
                "offer": db.get_setting("ai_offer", ""),
            })
            log("Разбор ИИ поставлен в очередь.")


def _say(task_id, log, text):
    """Сказать и записать, чем сейчас занята задача.

    Запрос к справочнику идёт десятки секунд, и всё это время в строке
    состояния было просто «выполняется». Программа в такие минуты
    выглядит зависшей, хотя она ждёт чужой сервер.
    """
    log(text)
    db.update_task(task_id, message=text)


class _Prefetch(object):
    """Обход сайтов с опережением, в несколько потоков.

    Держит окно в несколько компаний вперёд: пока обрабатывается первая,
    сайты следующих уже загружаются. Потоков немного намеренно —
    четыре разных сайта одновременно не создают нагрузки ни одному из
    них, а два десятка уже похожи на сканирование сети.

    Память под окном ограничена: результаты обхода содержат текст
    страниц, и держать их для пятисот компаний разом незачем.
    """

    # Три, а не четыре: разбор разметки — работа процессора, а она в
    # Python держит общую блокировку. Четыре потока её выедали, и окну,
    # которое рисуется в главном потоке, времени не оставалось — Windows
    # подписывал его «Не отвечает».
    WORKERS = 3
    WINDOW = 6

    def __init__(self, rows, log, should_stop=None):
        from concurrent.futures import ThreadPoolExecutor
        self.rows = rows
        self.log = log
        self.should_stop = should_stop or (lambda: False)
        self.pool = ThreadPoolExecutor(max_workers=self.WORKERS,
                                       thread_name_prefix="navodka-crawl")
        self.jobs = {}

    def fill(self, start):
        """Поставить в очередь сайты ближайших компаний."""
        if self.should_stop():
            return
        for row in self.rows[start:start + self.WINDOW]:
            cid, url = row["id"], (row["site"] or "").strip()
            if url and cid not in self.jobs:
                self.jobs[cid] = self.pool.submit(self._one, url)

    def _one(self, url):
        # Сайт, обойдённый на этой неделе, не обходим заново: всё, что с
        # него брали, уже лежит в базе. Второй прогон по той же нише
        # иначе стучится в те же сотни сайтов ради тех же данных.
        if db.visited_recently(url):
            return {"emails": [], "phones": [], "telegram": [], "tech": {},
                    "pages": 0, "error": "", "text": [], "socials": {},
                    "skipped": True}
        # Своя сессия на поток: requests.Session не рассчитана на то,
        # чтобы из неё ходили одновременно.
        try:
            res = site_src.crawl(url, session=requests.Session())
            db.mark_visited(url, res.get("pages") or 0,
                            ok=bool(res.get("pages")))
            return res
        except Exception as e:                       # pragma: no cover
            return {"emails": [], "phones": [], "telegram": [], "tech": {},
                    "pages": 0, "error": str(e)[:200], "text": [],
                    "socials": {}}

    def take(self, cid, url):
        """Результат обхода. Если он ещё не готов — подождать его."""
        fut = self.jobs.pop(cid, None)
        if fut is None:
            fut = self.pool.submit(self._one, url)
        try:
            return fut.result(timeout=180)
        except Exception as e:
            return {"emails": [], "phones": [], "telegram": [], "tech": {},
                    "pages": 0, "error": str(e)[:200], "text": [],
                    "socials": {}}

    def close(self):
        for fut in self.jobs.values():
            fut.cancel()
        self.jobs.clear()
        self.pool.shutdown(wait=False)


# ── Задача: справочник 2ГИС ──────────────────────────────
def task_gis_search(task_id, params):
    """Локальный бизнес по рубрике и городу.

    hh.ru находит тех, кто нанимает. Клиники и автосервисы вакансий часто
    не публикуют вовсе, а звонков у них больше, чем у иной софтверной
    компании — их берут по справочнику.
    """
    key = db.get_setting("gis_key", "")
    query = (params.get("query") or "").strip()
    region = int(params.get("region") or 32)
    pages = max(1, min(10, int(params.get("pages") or 2)))

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    if not key:
        log("Не задан ключ 2ГИС. Откройте «Настройки» и вставьте ключ "
            "Places API — без него источник не работает.", "error")
        return
    if not query:
        log("Не выбрана рубрика.", "error")
        return

    log("2ГИС: ищу «%s»" % query)
    items = gis2.search(query, region, key, pages=pages, on_log=log,
                        should_stop=_should_stop)
    if not items:
        log("Ничего не нашлось.", "warn")
        return

    db.update_task(task_id, total=len(items))
    for i, it in enumerate(items, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        if db.is_blacklisted({"name": it["name"]}):
            db.update_task(task_id, done=i)
            continue
        cid, _ = db.upsert_company({
            "name": it["name"], "site": site_src.normalize_url(it["site"]),
            "address": it["address"], "okved_name": it["rubric"],
            "source": "2ГИС",
        })
        for ph in it["phones"]:
            db.add_contact(cid, "phone", ph, "general", 85, "unchecked", "2ГИС")
        for em in it["emails"]:
            db.add_contact(cid, "email", em, site_src.guess_owner(em), 85,
                           "unchecked", "2ГИС")
        db.add_signal(cid, "gis_rubric", it["rubric"])
        _rescore(cid)
        db.update_task(task_id, done=i)
    log("Готово. Добавлено организаций: %d" % len(items))


# ── Задача: импорт своего списка ─────────────────────────
def task_import(task_id, params):
    """Свой список ИНН, доменов или названий — одним текстом."""
    rows = importer.parse(params.get("text") or "")

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    if not rows:
        log("В списке не нашлось ни ИНН, ни доменов, ни названий.", "warn")
        return
    db.update_task(task_id, total=len(rows))
    for i, r in enumerate(rows, 1):
        if _should_stop():
            break
        db.upsert_company({"name": r["name"], "inn": r["inn"],
                           "site": site_src.normalize_url(r["site"]),
                           "source": "импорт"})[0]
        db.update_task(task_id, done=i)
    log("Импортировано записей: %d. Теперь запустите обогащение." % len(rows))


# ── Задача: разбор моделью ───────────────────────────────
def task_ai(task_id, params):
    """ИИ по уже собранным фактам.

    Порядок важен: сначала обогащение, потом разбор. Модели нечего
    объяснять, пока в карточке одно название — она начнёт додумывать, а
    это ровно то, чего допускать нельзя.
    """
    limit = max(1, min(300, int(params.get("limit") or 30)))
    icp = (params.get("icp") or "").strip()
    offer = (params.get("offer") or "").strip()
    redo = bool(params.get("redo"))

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    cfg = ai.config()
    if not cfg["key"]:
        log("Не задан ключ ИИ. Откройте «Настройки» и вставьте ключ "
            "любого сервиса, совместимого с OpenAI.", "error")
        return
    ok, note = ai.check(cfg)
    if not ok:
        log("Модель не отвечает: %s" % note, "error")
        return
    log("Модель на связи (%s, %s)" % (cfg["model"], note))

    c = db.conn()
    where = "" if redo else "AND (ai_summary IS NULL OR ai_summary='')"
    rows = c.execute("""
        SELECT * FROM companies
        WHERE id IN (SELECT company_id FROM signals WHERE key='enriched') %s
        ORDER BY score DESC, id LIMIT ?""" % where, (limit,)).fetchall()
    if not rows:
        log("Нет обогащённых компаний. Сначала выполните обогащение — "
            "по одному названию разбирать нечего.", "warn")
        return

    db.update_task(task_id, total=len(rows))
    http = requests.Session()
    done = 0
    for i, row in enumerate(rows, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        cid = row["id"]
        sig = {r["key"]: r["value"] for r in
               c.execute("SELECT key, value FROM signals WHERE company_id=?", (cid,))}
        cts = c.execute("SELECT kind FROM contacts WHERE company_id=?", (cid,)).fetchall()
        brief = ai.company_brief(row, sig, cts)

        data, err = ai.analyze(brief, icp=icp, offer=offer, cfg=cfg, session=http)
        if err:
            log("[%d/%d] %s — %s" % (i, len(rows), row["name"], err), "warn")
            # Лимиты и перегрузка провайдера лечатся паузой, а не
            # прекращением обхода: следующая компания обычно проходит.
            time.sleep(2.0)
            db.update_task(task_id, done=i)
            continue

        patch = {}
        if data.get("summary"):
            patch["ai_summary"] = str(data["summary"])[:400]
        if data.get("segment"):
            patch["ai_segment"] = str(data["segment"])[:40]
        if icp and data.get("fit") is not None:
            try:
                patch["ai_fit"] = max(0, min(100, int(data["fit"])))
            except Exception:
                pass
        if data.get("fit_why"):
            patch["ai_why"] = str(data["fit_why"])[:400]
        if data.get("hook"):
            patch["ai_hook"] = str(data["hook"])[:400]
        if data.get("opener"):
            patch["ai_opener"] = str(data["opener"])[:800]
        if data.get("signals"):
            db.add_signal(cid, "ai_signals", "; ".join(
                str(x)[:120] for x in list(data["signals"])[:4]))
        if patch:
            db.update_company_fields(cid, patch)
            done += 1
        log("[%d/%d] %s%s" % (i, len(rows), row["name"],
                              (" — %s" % patch.get("ai_fit", "")) if icp else ""))
        db.update_task(task_id, done=i)
        time.sleep(0.2)

    log("Разобрано компаний: %d" % done)


# Поиск сообществ по названию сервисным ключом запрещён. Узнав это один
# раз за прогон, перестаём пробовать: ответ будет тот же, а запрос —
# лишняя секунда на каждой компании.
_vk_search_off = {"off": False}


# Организационная шелуха в названиях. «ООО "АН АЛТАЙ"» из ЕГРЮЛ и «АН
# Алтай» из карты — одна компания, и пока они считались разными, карточка
# оставалась без телефона при том, что телефон пришёл.
_OPF_RE = re.compile(
    r"^\s*(ООО|ОАО|ЗАО|ПАО|АО|ИП|НКО|АНО|НАО|ГБУ|МБУ|ФГУП|МУП|ТСЖ|СНТ)\s+",
    re.I)


def norm_name(name):
    """Название без формы собственности, кавычек и лишних знаков."""
    s = (name or "").strip()
    for _ in range(2):                      # «ООО НПО Ромашка»
        s = _OPF_RE.sub("", s)
    s = re.sub(r"[«»\"'`]", " ", s)
    s = re.sub(r"[^\w\s-]", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _known_vk_link(cid):
    """Ссылка на сообщество, которую компания уже опубликовала.

    К этому шагу она обычно лежит в базе: её приносит обход сайта или
    карточка Яндекса. Это и есть главный путь — getById по ссылке
    работает сервисным ключом, в отличие от поиска.
    """
    rows = db.conn().execute(
        "SELECT value FROM contacts WHERE company_id=? AND kind='social'",
        (cid,)).fetchall()
    for r in rows:
        if vk.screen_name(r["value"]):
            return r["value"]
    return ""


def _vk_contacts(cid, row, token, http, log):
    """Сообщество компании во ВКонтакте и его контактные лица.

    Возвращает True, если нашёлся контакт первого лица. Молчит, когда не
    нашлось: у большинства компаний группы либо нет, либо контакты в ней
    не заполнены, и писать об этом в журнал по каждой строке — значит
    засыпать его пустотой.
    """
    g, contacts, about = {}, [], {}

    link = _known_vk_link(cid)
    if link:
        g, err = vk.by_url(link, token, session=http, on_log=log)
        if err:
            log("   ВК: %s" % err, "warn")
            return False
        if g:
            contacts, about = vk.contacts_from_group(g.get("raw"), token, http)

    if not g and not _vk_search_off["off"]:
        # Запасной путь — поиск по названию. Работает только с ключом
        # пользователя; сервисный получает отказ, и тогда выключаем.
        found = vk.find_group(row["name"], token, session=http, on_log=log)
        if found.get("error"):
            _vk_search_off["off"] = True
            log("   ВК: поиск по названию этому ключу недоступен — дальше "
                "работаем только по ссылкам с сайтов и из справочников.", "warn")
            return False
        g = found or {}
        if g:
            contacts, about = vk.group_contacts(g["id"], token, session=http,
                                                on_log=log)
    if not g:
        return False

    db.add_signal(cid, "vk_group", g["url"])
    db.add_contact(cid, "social", g["url"], "general", 80, "unchecked",
                   "группа ВКонтакте")
    if about.get("members"):
        db.add_signal(cid, "vk_members", about["members"])
    if not contacts:
        return False

    director = (row["director"] or "").strip()
    got_lpr = False
    for c in contacts:
        # Совпало с ЕГРЮЛ — это руководитель, и сомнений нет.
        # Подпись «директор» без совпадения ФИО — тоже первое лицо, но
        # уверенность ниже: в ЕГРЮЛ может стоять другой человек.
        same = director and c["name"] and enrich.same_person(director, c["name"])
        who = "director" if (same or c["boss"]) else "unknown"
        conf = 95 if same else (85 if c["boss"] else 70)
        note = ("ВК: контакт группы, ФИО совпало с ЕГРЮЛ" if same else
                "ВК: контакт группы%s" % ((" — " + c["post"]) if c["post"] else ""))
        if c["url"]:
            db.add_contact(cid, "social", c["url"], who, conf, "unchecked", note)
        if c["email"]:
            db.add_contact(cid, "email", c["email"], who, conf, "unchecked", note)
        if c["phone"]:
            db.add_contact(cid, "phone", c["phone"], who, conf, "unchecked", note)
        if same or c["boss"]:
            got_lpr = True
            log("   ВК: %s%s — %s"
                % (c["name"] or "контакт группы",
                   (" (" + c["post"] + ")") if c["post"] else "",
                   "ФИО совпало с ЕГРЮЛ" if same else "по подписи"))
        # Руководителя из ЕГРЮЛ нет, а в контактах группы человек с
        # должностью первого лица — берём его: у ИП и филиалов иначе
        # руководителя не узнать вовсе.
        if not director and c["boss"] and c["name"]:
            db.update_company_fields(cid, {"director": c["name"],
                                           "director_post": c["post"] or "руководитель"})
            director = c["name"]
    return got_lpr


def _rescore(company_id):
    c = db.conn()
    row = c.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if row is None:
        return
    sig = {r["key"]: r["value"] for r in
           c.execute("SELECT key, value FROM signals WHERE company_id=?", (company_id,))}
    cts = c.execute("SELECT kind, owner, verified FROM contacts WHERE company_id=?",
                    (company_id,)).fetchall()
    value, _ = score.compute(row, sig, cts)
    db.set_score(company_id, value)


# ── Задача: поиск по виду деятельности ───────────────────
def task_find(task_id, params):
    """Один запрос — «стоматология», «грузоперевозки», «АТИ» — по всем
    источникам сразу.

    Это другой вопрос, чем у поиска по вакансиям. Там мы искали компании
    с подтверждённой болью: нанимают продавцов, значит продажи буксуют.
    Здесь — все компании нужного вида, независимо от того, нанимают они
    кого-нибудь или нет. Ни один справочник в одиночку на такой вопрос
    не отвечает: 2ГИС знает вывески, но не знает ИНН; ЕГРЮЛ знает
    юрлица, но только те, у кого вид деятельности попал в название; hh
    знает работодателей, но только тех, кто хоть раз нанимал. Поэтому
    спрашиваем все три и сводим ответы в один список.
    """
    query = (params.get("query") or "").strip()
    cities = geo.pick(params.get("cities") or [])
    use = params.get("sources") or {}
    pages = max(1, min(10, int(params.get("pages") or 3)))
    # Предел на прогон. Без него запрос вроде «магазин» по десяти городам
    # выгребает десятки тысяч записей, обход которых идёт сутки, а руки
    # доходят до первой сотни. Лучше набрать двести и посмотреть, те ли
    # это компании, чем ждать до вечера и выяснить, что запрос был не тот.
    limit = max(10, min(5000, int(params.get("limit") or 200)))

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    if not query:
        raise RuntimeError("не задано, кого искать")

    gis_key = db.get_setting("gis_key", "")
    dadata_token = db.get_setting("dadata_token", "")
    yandex_key = db.get_setting("yandex_key", "")
    want_osm = use.get("osm", True)
    want_gis = use.get("gis", True) and bool(gis_key)
    want_yandex = use.get("yandex", True) and bool(yandex_key)
    want_egrul = use.get("dadata", True) and bool(dadata_token)
    want_hh = use.get("hh", True)

    if use.get("gis", True) and not gis_key:
        log("2ГИС пропущен: не задан ключ Places API.", "warn")
    if use.get("yandex", True) and not yandex_key:
        log("Яндекс пропущен: не задан ключ Геопоиска.", "warn")
    if use.get("dadata", True) and not dadata_token:
        log("ЕГРЮЛ пропущен: не задан токен DaData.", "warn")
    if not (want_osm or want_gis or want_yandex or want_egrul or want_hh):
        raise RuntimeError("не включён ни один источник — задайте ключи в «Настройках»")

    log("Ищу «%s» по городам: %s"
        % (query, ", ".join(c["name"] for c in cities)))

    http = requests.Session()
    http.headers.update({"User-Agent": settings.USER_AGENT})
    errors = []
    # Одна и та же компания приходит из разных источников по-разному, и
    # ценность у каждого своя: в карте есть телефон и сайт, но нет ИНН; в
    # ЕГРЮЛ есть ИНН и руководитель, но нет ни одного способа позвонить.
    # Поэтому повторную запись мы не выбрасываем, а сливаем с первой —
    # иначе половина карточек остаётся пустой при том, что данные
    # пришли, просто в разных ответах.
    rows, by_key = [], {}

    def keys_of(row):
        """Чем эту компанию можно опознать. Порядок не важен: совпадения
        по любому ключу достаточно."""
        out = []
        inn = (row.get("inn") or "").strip()
        if inn:
            out.append("инн:" + inn)
        site = (row.get("site") or "").strip().lower()
        site = site.split("//")[-1].split("/")[0].replace("www.", "")
        if site:
            out.append("сайт:" + site)
        name = norm_name(row.get("name"))
        if name:
            out.append("имя:" + name)
        return out

    def full():
        return len(rows) >= limit

    def add(row, source):
        ks = keys_of(row)
        if not ks:
            return False
        # Уже набранной компании дополнение не мешает: предел считается
        # по числу компаний, а не по числу ответов источников.
        if full() and not any(k in by_key for k in ks):
            return False
        old = next((by_key[k] for k in ks if k in by_key), None)
        if old is not None:
            merge_into(old, row, source)
            return False
        row["source"] = source
        rows.append(row)
        for k in ks:
            by_key[k] = row
        return True

    def merge_into(old, new, source):
        """Дополнить уже найденную компанию тем, чего у неё не было."""
        for field in ("inn", "ogrn", "site", "director", "director_post",
                      "address", "okved", "okved_name", "region", "status",
                      "founded", "employees"):
            if not (old.get(field) or "") and new.get(field):
                old[field] = new[field]
        for field in ("phones", "emails", "links"):
            have = old.setdefault(field, []) or []
            for v in (new.get(field) or []):
                if v not in have:
                    have.append(v)
            old[field] = have
        # Источники копим списком: по нему видно, откуда что взялось, и
        # это единственный способ потом понять, какой источник полезен.
        src = old.get("source") or ""
        if source not in src.split(" + "):
            old["source"] = (src + " + " + source) if src else source
        # Ключи новой записи теперь тоже ведут к объединённой.
        for k in keys_of(new):
            by_key.setdefault(k, old)

    for city in cities:
        if _should_stop():
            break
        if full():
            log("Набрано %d компаний — это предел на один прогон. Остальные "
                "города пропускаю: поднимите предел или сузьте запрос." % limit,
                "warn")
            break

        if not city.get("ll") and (want_osm or want_gis or want_yandex):
            # Ловушка, в которую попадают первым делом: «Россия целиком»
            # выглядит как «искать везде», а на деле отключает все карты
            # разом — они ищут по прямоугольнику на карте, а не по стране.
            # Остаются ЕГРЮЛ и hh, и карточки выходят без единого телефона.
            log("«%s»: справочники ищут по городу, поэтому здесь работают "
                "только ЕГРЮЛ и hh — без телефонов и сайтов. Выберите "
                "города, чтобы получить контакты." % city["name"], "warn")

        if want_osm and city.get("ll") and not full():
            _say(task_id, log, "OpenStreetMap · %s" % city["name"])
            for it in osm.search(query, city, session=http, on_log=log,
                                 should_stop=_should_stop):
                add({"name": it["name"], "site": it["site"],
                     "address": it["address"], "okved_name": it["rubric"],
                     "region": city["name"], "phones": it["phones"],
                     "emails": it["emails"], "links": it["links"]},
                    "OpenStreetMap")

        if want_gis and city["gis"] and not full():
            _say(task_id, log, "2ГИС · %s" % city["name"])
            for it in gis2.search(query, city["gis"], gis_key, pages=pages,
                                  session=http, on_log=log,
                                  should_stop=_should_stop):
                add({"name": it["name"], "site": it["site"],
                     "address": it["address"], "okved_name": it["rubric"],
                     "region": city["name"], "phones": it["phones"],
                     "emails": it["emails"]}, "2ГИС")

        if want_yandex and city.get("ll") and not full():
            _say(task_id, log, "Яндекс · %s" % city["name"])
            for it in yandex.search(query, city, yandex_key, pages=min(pages, 4),
                                    session=http, on_log=log,
                                    should_stop=_should_stop):
                add({"name": it["name"], "site": it["site"],
                     "address": it["address"], "okved_name": it["rubric"],
                     "region": city["name"], "phones": it["phones"],
                     "emails": [], "links": it["links"]}, "Яндекс")

        if want_egrul and not full():
            _say(task_id, log, "ЕГРЮЛ · %s" % city["name"])
            region = "" if city["name"] == "Россия целиком" else city["name"]
            for it in dadata.search_by_name(query, dadata_token, region=region,
                                            session=http, on_log=log):
                add(dict(it, phones=[], emails=[]), "ЕГРЮЛ")

        if want_hh and city["hh"] and not full():
            _say(task_id, log, "hh.ru · %s" % city["name"])
            before = len(errors)
            part = hh.search_employers_by_text(query, area=city["hh"],
                                               pages=min(pages, 5), on_log=log,
                                               should_stop=_should_stop,
                                               errors=errors)
            if len(errors) > before:
                # hh отказал. Повторять перебор способов связи на каждом
                # следующем городе бессмысленно: ответ будет тот же, а
                # ждать придётся по минуте на город.
                want_hh = False
                log("hh отключён до конца прогона — остальные источники "
                    "продолжают работу.", "warn")
            for e in part:
                detail = hh.employer_details(e["id"], session=http)
                add({"name": detail.get("name") or e["name"],
                     "hh_id": e["id"],
                     "site": site_src.normalize_url(detail.get("site") or ""),
                     "region": detail.get("area") or e.get("area") or city["name"],
                     "okved_name": detail.get("industries") or "",
                     "about": detail.get("about") or "",
                     "open_vacancies": detail.get("open_vacancies") or 0,
                     "phones": [], "emails": []}, "hh.ru")
                time.sleep(0.3)

    if not rows:
        # Молчаливый ноль — худший исход: непонятно, то ли таких компаний
        # нет, то ли источник отказал.
        if errors:
            raise RuntimeError(errors[-1][:400])
        raise RuntimeError(
            "ничего не нашлось. Проверьте слово (попробуйте короче: "
            "«стоматология» вместо «стоматологическая клиника») и убедитесь, "
            "что задан ключ 2ГИС — без него ищут только ЕГРЮЛ и hh.")

    db.update_task(task_id, total=len(rows), message="")
    log("Найдено записей: %d%s. Раскладываю по базе."
        % (len(rows), " (упёрлось в предел)" if full() else ""))

    skip_empty = params.get("skip_empty", True)
    added = known = skipped = empty = 0
    for i, row in enumerate(rows, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        if db.is_blacklisted({"inn": row.get("inn"), "hh_id": row.get("hh_id"),
                              "name": row.get("name")}):
            skipped += 1
            db.update_task(task_id, done=i)
            continue
        # Компания, о которой не известно ничего, кроме названия, — это не
        # лид, а строка в реестре. Обогащать её нечем: без сайта не с чего
        # брать почты, без ИНН не спросить ЕГРЮЛ. В списке она только
        # мешает искать те, с которыми можно работать.
        if skip_empty and not (row.get("site") or row.get("phones")
                               or row.get("emails") or row.get("links")
                               or row.get("inn")):
            empty += 1
            db.update_task(task_id, done=i)
            continue
        phones = row.pop("phones", []) or []
        emails = row.pop("emails", []) or []
        links = row.pop("links", []) or []
        about = row.pop("about", "")
        open_vac = row.pop("open_vacancies", 0)
        cid, is_new = db.upsert_company(row)
        added += 1 if is_new else 0
        known += 0 if is_new else 1
        # Откуда контакт — видно в карточке, и это не украшение: телефон
        # из карты и телефон с сайта проверяются по-разному.
        origin = (row.get("source") or "справочник").split(" + ")[0]
        for ph in phones[:4]:
            # Заглушки из вёрстки попадают и в карты: +7 999 999-99-99
            # там встречается ничуть не реже, чем на сайтах.
            ok_phone = site_src._clean_phone(ph)
            if ok_phone:
                db.add_contact(cid, "phone", ok_phone, "general", 85,
                               "unchecked", origin)
        for addr in emails[:3]:
            db.add_contact(cid, "email", addr, site_src.guess_owner(addr), 85,
                           "unchecked", origin)
        # Ссылки, которые компания указала в карточке Яндекса: среди них
        # её страницы в соцсетях. Сама компания их и опубликовала.
        for url in links[:6]:
            net_name = social.which(url)
            if net_name:
                db.add_contact(cid, "social", url, "general", 82, "unchecked",
                               "%s: %s" % (origin, net_name))
        # По какому слову компания попала в список. Через неделю это
        # единственный способ вспомнить, зачем она здесь.
        db.add_signal(cid, "found_by", query)
        if about:
            db.add_signal(cid, "hh_about", about)
        if open_vac:
            db.add_signal(cid, "hh_open_all", open_vac)
        _rescore(cid)
        db.update_task(task_id, done=i)

    total = db.conn().execute("SELECT COUNT(*) c FROM companies").fetchone()["c"]
    log("Готово. Новых: %d, уже было: %d%s%s. Всего в базе: %d"
        % (added, known,
           (", пустых пропущено: %d" % empty) if empty else "",
           (", из чёрного списка: %d" % skipped) if skipped else "",
           total))
    if empty and not added:
        log("Все находки оказались без контактов. Так бывает, когда "
            "выбрана «Россия целиком» или не подключён ни один справочник.",
            "warn")

    if params.get("then_enrich") and added:
        db.create_task("enrich", {
            "limit": min(500, added), "fns": True, "only_lpr": False,
            "zakupki": bool(params.get("then_zakupki")),
            "then_ai": bool(params.get("then_ai")),
        })
        log("Обогащение поставлено в очередь.")
    elif params.get("then_enrich"):
        log("Новых компаний нет — обогащать нечего.", "warn")


HANDLERS = {
    "ai": task_ai,
    "find": task_find,
    "hh_search": task_hh_search,
    "gis_search": task_gis_search,
    "import": task_import,
    "enrich": task_enrich,
}
