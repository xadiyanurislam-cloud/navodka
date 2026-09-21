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
from concurrent import futures

import requests

from . import (ai, db, enrich, geo, net, profile, score, settings, social,
               trades, verify)
from .sources import (dadata, fns, gis2, hh, importer, osm,
                      site as site_src, vk, yandex, zakupki)

_thread = None
_plans = None
_stop = threading.Event()
_current = {"task_id": None}


# ── Запуск и остановка ───────────────────────────────────
def start():
    global _thread, _watch, _plans
    if _thread and _thread.is_alive():
        return
    recover()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="navodka-worker", daemon=True)
    _thread.start()
    if not (_watch and _watch.is_alive()):
        _watch = threading.Thread(target=_watchdog, name="navodka-watch",
                                  daemon=True)
        _watch.start()
    if not (_plans and _plans.is_alive()):
        _plans = threading.Thread(target=_scheduler, name="navodka-plans",
                                  daemon=True)
        _plans.start()


def _scheduler():
    """Повторяет сохранённые поиски по расписанию.

    Отдельным потоком, а не внутри сторожа: сторож меряет собственное
    опоздание, чтобы поймать зависание, и работа с базой внутри него
    выглядела бы как то самое зависание, которое он ищет.

    Задача не выполняется здесь, а ставится в общую очередь — ту же,
    куда попадают нажатия кнопок. Иначе два обхода пошли бы разом и
    подрались за чужие серверы и за базу.

    Честно про условие: расписание работает, только пока программа
    открыта. Служб в системе она не заводит и по будильнику не
    просыпается — об этом сказано и в интерфейсе, рядом с выбором
    срока.
    """
    while True:
        time.sleep(30)
        try:
            run_due_plans()
        except Exception:
            pass


def run_due_plans():
    """Поставить в очередь один набор, которому пора. Возвращает его id.

    Один, а не все: обход всё равно идёт по одному, а очередь из пяти
    одинаковых задач человек читает как сбой. Остальные дождутся
    следующего круга — он через полминуты.
    """
    due = db.due_searches()
    if not due:
        return None
    busy = db.conn().execute(
        "SELECT COUNT(*) n FROM tasks "
        "WHERE status IN ('queued','running')").fetchone()["n"]
    # Очередь не пуста — подождём. Повтор по расписанию не настолько
    # срочен, чтобы лезть вперёд того, что человек запустил руками.
    if busy:
        return None
    row = due[0]
    tid = db.create_task(row["kind"] or "find", row["params"] or {})
    db.mark_search_run(row["id"])
    db.log(tid, "Это повтор по расписанию: набор «%s», %s. Выключить можно "
                "там же, где сохраняли."
           % (row["name"], "раз в %d дн." % (row["every_days"] or 0)))
    return tid


_watch = None
_stall = {"worst": 0.0}


def _watchdog():
    """Сторож: замечает, когда программа перестаёт отвечать, и говорит
    об этом в журнале.

    Нужен потому, что «зависло» снаружи выглядит одинаково, а причин
    две, и лечатся они по-разному. Либо все потоки Python стоят — тогда
    и этот поток проснётся с опозданием, и опоздание попадёт в журнал.
    Либо стоит только окно, а Python работает — тогда в журнале будет
    пусто, и значит дело не в обходе, а в отрисовке.

    Сам сторож почти ничего не стоит: просыпается раз в секунду и
    смотрит на часы. Без него разбор зависания — гадание, а гадать здесь
    уже приходилось, причём мимо.
    """
    last = time.time()
    while True:
        time.sleep(1.0)
        now = time.time()
        late = now - last - 1.0
        last = now
        if late < 2.0:
            continue
        _stall["worst"] = max(_stall["worst"], late)
        tid = _current.get("task_id")
        if tid:
            try:
                db.log(tid, "Программа не отвечала %.0f с — потоки стояли. "
                            "Если окно в этот момент побелело, причина здесь."
                       % late, "warn")
            except Exception:
                pass


def recover():
    """Разобраться с задачами, оставшимися с прошлого запуска.

    Программу закрывают посреди обхода — это норма. Но запись о задаче
    остаётся в базе со статусом «выполняется», и при следующем запуске
    её никто не подхватывает: очередь берёт только «в очереди». Такая
    запись становится вечной: интерфейс показывает именно её, счётчик
    стоит на нуле, а все новые задачи ждут за ней невидимой очередью.
    Снаружи это выглядит как намертво зависшая программа — час, сутки,
    сколько угодно.

    Поэтому при старте все «выполняется» честно помечаются прерванными.
    Не перезапускаются: половина работы уже сделана и лежит в базе, а
    повторять обход за человека, которого нет у экрана, незачем.
    """
    # Заодно подметаем хвосты от удалённых компаний: до недавнего
    # времени удаление не трогало заметки, и в базе у тех, кто чистил
    # список, лежат записи, привязанные к несуществующим номерам.
    try:
        db.clean_orphans()
    except Exception:
        pass
    try:
        c = db.conn()
        rows = c.execute("SELECT id, kind, done, total FROM tasks "
                         "WHERE status='running'").fetchall()
        for r in rows:
            db.log(r["id"], "Прервано закрытием программы: сделано %s из %s. "
                            "Запустите заново, уже собранное сохранилось."
                   % (r["done"], r["total"]), "warn")
        if rows:
            c.execute("UPDATE tasks SET status='stopped', "
                      "message='прервано закрытием программы' "
                      "WHERE status='running'")
            c.commit()
        return len(rows)
    except Exception:
        return 0


def stop_all():
    """Остановить текущую задачу. Уже сделанное остаётся в базе."""
    _stop.set()


def _should_stop():
    return _stop.is_set()


def alive():
    """Жив ли поток обхода. Без этой проверки его смерть выглядит как
    вечно выполняющаяся задача."""
    return bool(_thread and _thread.is_alive())


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
        try:
            # Отметка «взял в работу» вынесена внутрь try намеренно: база
            # на секунду занята соседним потоком — и раньше поток обхода
            # тихо умирал прямо здесь, а все задачи оставались висеть.
            db.update_task(row["id"], status="running", message="")
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


def _chain_stopped(task_id, what):
    """Ставить ли следующую задачу цепочки.

    «Остановить» должно останавливать. Раньше проверки здесь не было:
    человек нажимал стоп посреди поиска, обход прерывался — и тут же
    ставил в очередь обогащение, которое идёт втрое дольше. Снаружи это
    выглядело как кнопка, которая ничего не делает, и единственным
    способом действительно остановить программу было закрыть окно.
    """
    if not _should_stop():
        return False
    db.log(task_id, "Остановлено — %s в очередь не ставлю." % what, "warn")
    return True


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
    # Поиск по номеру вместо перебора списка: на тысяче работодателей
    # перебор превращался в миллион сравнений.
    by_id = {}
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
                    old = by_id[emp["id"]]
                    # Считаем вакансии по номерам, а не складываем итоги.
                    # Одна и та же вакансия находится по двум запросам
                    # сразу — «Руководитель отдела продаж» отвечает и на
                    # «отдел продаж», и на «руководитель продаж», — и
                    # сложение удваивало её. Число вакансий весит в
                    # оценке четверть, так что удвоение поднимало
                    # компанию в списке ни за что.
                    for vid in (emp.get("vac_ids") or []):
                        if vid not in old["vac_ids"]:
                            old["vac_ids"].append(vid)
                    old["vacancies"] = (len(old["vac_ids"]) or
                                        old["vacancies"] + emp["vacancies"])
                    for t in emp.get("titles") or []:
                        if t not in old["titles"]:
                            old["titles"].append(t)
                    old["salaries"] = (old.get("salaries") or []) + (emp.get("salaries") or [])
                    if emp.get("fresh") is not None and \
                       (old.get("fresh") is None or emp["fresh"] < old["fresh"]):
                        old["fresh"] = emp["fresh"]
                    continue
                seen.add(emp["id"])
                emp.setdefault("vac_ids", [])
                by_id[emp["id"]] = emp
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
    # Ставим обогащение по факту находок, а не только новых.
    #
    # Обогащение и так берёт только тех, у кого его ещё не было, поэтому
    # условие «есть новые» ничего не защищало, а вредило: прогон,
    # прерванный на середине, оставлял компании без обогащения навсегда —
    # повторный поиск находил их же, новых не было, и очередь пустовала.
    if params.get("then_enrich") and (added or known) \
            and not _chain_stopped(task_id, "обогащение"):
        # Поиск без обогащения — половина дела: в карточке одно название.
        # Ставим вторую задачу в очередь, чтобы не ждать у экрана.
        db.create_task("enrich", {
            "limit": min(500, max(added + known, 1)), "fns": True,
            "only_lpr": True,
            "zakupki": bool(params.get("then_zakupki")),
            # Цепочка идёт дальше сама: человек нажал одну кнопку и ушёл,
            # возвращаться к экрану ради второго и третьего нажатия он не
            # должен.
            "then_ai": bool(params.get("then_ai")),
        })
        log("Обогащение поставлено в очередь.")
    elif params.get("then_enrich"):
        log("Обогащать нечего: ничего не найдено.", "warn")


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
    lpr_now = lpr_guessed_now = 0
    for i, row in enumerate(rows, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        cid = row["id"]
        crawls.fill(i - 1)
        found_lpr = False        # контакт первого лица найден, а не выведен
        guessed_lpr = False      # выведен по схеме домена — это догадка
        step_t0 = time.time()
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
                # Именно в эту строку, а не upsert по содержимому ответа.
                # Из ЕГРЮЛ приходит юридическое название — «ПАО ДВМП»
                # вместо вывески «Fesco», — и upsert не узнавал исходную
                # компанию: заводил вторую, а первая оставалась пустой.
                db.fill_company(cid, info)
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
            # Имя переменной не net: так зовётся модуль сетевого слоя, и
            # цикл затенял его до конца функции. Сейчас после цикла к нему
            # не обращаются, но это вопрос везения, а не устройства.
            for _net, title, url in social.as_links(res.get("socials") or {}):
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
                for _net, title, url in social.as_links(
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

        # 2.5. Реквизиты с сайта — и второй заход в ЕГРЮЛ.
        #
        # Это самый частый случай пустой карточки. В справочнике стоит
        # вывеска — «Fesco», «Дента-Люкс», — а в ЕГРЮЛ та же компания
        # записана как ПАО «ДВМП» или ООО «Стоматология плюс», и по
        # вывеске юрлицо не находится. Без ИНН дальше не спросить ни
        # ЕГРЮЛ, ни ФНС: карточка остаётся без руководителя, без
        # выручки, без численности и без года — то есть без всего, ради
        # чего её открывают.
        #
        # Сам ИНН при этом лежит в подвале сайта, который мы только что
        # прочитали. Берём его оттуда и переспрашиваем — теперь по
        # номеру, а не по названию.
        if res.get("inn") and not (row["inn"] or "").strip():
            db.fill_company(cid, {"inn": res["inn"], "ogrn": res.get("ogrn")})
            row = db.conn().execute("SELECT * FROM companies WHERE id=?",
                                    (cid,)).fetchone()
            log("   ИНН с сайта: %s" % res["inn"])
            if token:
                info = dadata.by_inn(res["inn"], token, session=http)
                info.pop("opf", None)
                if info:
                    if info.get("status") and info["status"] != "ACTIVE":
                        log("   ВНИМАНИЕ: статус в ЕГРЮЛ — %s"
                            % info["status"], "warn")
                    # over=True: ответ пришёл по ИНН, то есть надёжнее
                    # того, что могло стоять раньше по названию.
                    db.fill_company(cid, info, over=True)
                    row = db.conn().execute(
                        "SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
                    log("   ЕГРЮЛ по ИНН с сайта: %s%s"
                        % (info.get("name") or "",
                           (" · " + info["director"]) if info.get("director") else ""))
                else:
                    log("   ЕГРЮЛ по ИНН с сайта ничего не дал", "warn")

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
        #
        # Домен берём у сайта, а не у первой попавшейся почты. На сайте
        # рядом с корпоративными адресами сплошь и рядом лежит почта на
        # бесплатной службе — своя у бухгалтера, партнёрская, оставшаяся
        # с прошлого подрядчика. Раньше схему строили по первой из
        # найденных, и у компании с info@gmail.com «адресом руководителя»
        # оказывался ivanov@gmail.com: ящик какого-то Иванова, которых
        # там десятки тысяч.
        site_host = ""
        if (row["site"] or ""):
            site_host = (row["site"].split("//")[-1].split("/")[0]
                         .replace("www.", "").lower())
        own = [e for e in emails_found
               if site_host and e.lower().endswith("@" + site_host)]
        if site_host:
            domain = site_host
        elif emails_found:
            domain = emails_found[0].split("@")[1].lower()
        else:
            domain = ""
        # Примеры для угадывания схемы — только с этого же домена: по
        # чужим адресам видно чужие привычки именования.
        emails_found = own or ([] if site_host else emails_found)

        cands = enrich.candidates(row["director"], domain, emails_found) if domain else []
        if cands:
            verdicts = {}
            if do_verify:
                verdicts = verify.check([a for a, _ in cands[:6]])
            keep, why = enrich.keep_candidates(cands, verdicts)
            for addr, conf in keep:
                v = verdicts.get(addr, "unchecked")
                if db.add_contact(cid, "email", addr, "director",
                                  conf if v != "ok" else 95, v, why):
                    guessed_lpr = True
            if keep:
                log("   кандидатов в почту руководителя: %d, оставили %d — %s"
                    % (len(cands), len(keep), why))
            else:
                log("   все кандидаты в почту руководителя отвергнуты сервером")

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

        # Чем кончился поиск первого лица.
        #
        # Признак читают четверо: счётчик в шапке, фильтр «Контакт ГД
        # найден», выгрузка и сама оценка — там на нём висит четверть
        # веса. До сих пор его никто не записывал: переменная считалась
        # по всему обогащению и молча пропадала в конце. Счётчик стоял на
        # нуле при любом числе находок, фильтр не показывал ничего, а
        # двадцать пять баллов в оценке были недостижимы.
        if found_lpr:
            db.add_signal(cid, "lpr_contact", "найден")
            lpr_now += 1
        elif guessed_lpr:
            db.add_signal(cid, "lpr_contact", "выведен")
            lpr_guessed_now += 1

        db.add_signal(cid, "enriched", int(time.time()))
        _rescore(cid)
        db.update_task(task_id, done=i)
        # Долгая компания — не беда сама по себе: чужой сервер думает
        # столько, сколько думает. Но если жалуются на зависание, по
        # журналу должно быть видно, на ком именно оно случилось.
        spent = time.time() - step_t0
        if spent > 25:
            log("   заняла %.0f с — это много. Обычно виноват медленный "
                "сайт компании." % spent, "warn")

    crawls.close()
    if _stall["worst"] > 2:
        log("За прогон программа переставала отвечать, худшая заминка "
            "%.0f с. Это Python, а не отрисовка." % _stall["worst"], "warn")
        _stall["worst"] = 0.0
    # Итог — по этому прогону, а не по всей базе. Раньше считалось
    # запросом ко всей таблице, и на втором прогоне строка «найдено 40»
    # означала сорок за всё время, включая вчерашние: цифра росла сама
    # собой и ничего не говорила о том, что дал этот обход.
    total_lpr = db.conn().execute(
        "SELECT COUNT(*) n FROM signals WHERE key='lpr_contact' AND value='найден'"
    ).fetchone()["n"]
    log("Обогащение завершено. Контакт ГД найден у %d из %d за этот прогон"
        "%s. Всего таких в базе: %d."
        % (lpr_now, i if rows else 0,
           ", выведен по схеме ещё у %d" % lpr_guessed_now
           if lpr_guessed_now else "", total_lpr))

    if params.get("then_ai") and not _chain_stopped(task_id, "разбор ИИ"):
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
    pages = max(1, min(10, int(params.get("pages") or 2)))
    # Город называется по имени: номер региона в справочнике 2ГИС есть
    # не у каждого, и для остальных поиск идёт по координатам.
    name = (params.get("city") or "").strip()
    city = next((c for c in geo.cities() if c["name"] == name), None)
    region = city["gis"] if city else int(params.get("region") or 32)
    point = (city or {}).get("ll") or ""

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    if not key:
        log("Не задан ключ 2ГИС. Откройте «Настройки» и вставьте ключ "
            "Places API — без него источник не работает.", "error")
        return
    if not query:
        log("Не выбрана рубрика.", "error")
        return

    log("2ГИС: ищу «%s»%s" % (query, (" · " + name) if name else ""))
    items = gis2.search(query, region, key, pages=pages, on_log=log,
                        should_stop=_should_stop, point=point)
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

    # Разбор идёт в несколько потоков.
    #
    # Запрос к модели — это ожидание чужого сервера: секунд двадцать, из
    # которых программа не делает ничего. По одному тридцать компаний
    # занимали тринадцать минут, и всё это время человек сидел и смотрел
    # на счётчик. Обход сайтов ходит в три потока давно, а разбор почему-то
    # нет.
    #
    # Потоков по умолчанию четыре: посредники держат лимит на запросы в
    # минуту, и десяток параллельных обращений упрётся в него раньше, чем
    # в скорость. Число настраивается — у каждого посредника лимит свой.
    threads = max(1, min(8, int(params.get("threads")
                               or db.get_setting("ai_threads", "") or 4)))
    # Сессию на поток свою: одна на всех в requests не потокобезопасна, а
    # новая на каждый запрос заново делает рукопожатие TLS — через прокси
    # это дороже самого ответа.
    _local = threading.local()

    # Отступление к одному потоку.
    #
    # Четыре одновременных соединения выдерживает не всякий посредник:
    # кто-то держит лимит на запросы, кто-то на соединения с одного
    # адреса, а фильтр по дороге просто рвёт лишние. Снаружи это выглядит
    # одинаково — «связь разорвана» на каждой компании подряд, и весь
    # прогон уходит впустую.
    #
    # Поэтому обрывы считаются, и после трёх подряд программа сама
    # переходит на один запрос за раз. Лучше медленно и до конца, чем
    # быстро и вхолостую. Обратно не возвращаемся: если посредник уже
    # показал, что параллельных не любит, проверять это ещё раз на каждой
    # следующей компании незачем.
    solo = {"on": threads == 1, "said": False, "streak": 0}
    gate = threading.Lock()          # пропускает по одному запросу
    count_lock = threading.Lock()    # общий счётчик обрывов

    def one(row):
        """Разбор одной компании. Выполняется в рабочем потоке.

        Обрыв связи — не приговор: следующая попытка через паузу обычно
        проходит. Отказ по существу («неизвестная модель», «неверный
        ключ») повторять незачем — ответ будет тот же.
        """
        if not hasattr(_local, "http"):
            _local.http = requests.Session()
        data, err = {}, ""
        for attempt in range(3):
            if solo["on"]:
                with gate:
                    data, err = ai.analyze(row["brief"], icp=icp, offer=offer,
                                           cfg=cfg, session=_local.http)
            else:
                data, err = ai.analyze(row["brief"], icp=icp, offer=offer,
                                       cfg=cfg, session=_local.http)
            if not err:
                solo["streak"] = 0
                return data, ""
            if not (ai._is_reset(err) or "429" in err or "503" in err):
                return data, err
            with count_lock:
                solo["streak"] += 1
                if solo["streak"] >= 3 and not solo["on"]:
                    solo["on"] = True
                    if not solo["said"]:
                        solo["said"] = True
                        log("Три обрыва подряд — перехожу на один запрос за "
                            "раз. Посредник не держит несколько соединений "
                            "сразу; будет медленнее, зато дойдёт до конца.",
                            "warn")
            # Пауза растёт: 2, затем 10 секунд. Долбить сервер, который
            # только что порвал соединение, значит получить тот же ответ.
            # Десять секунд хватает, чтобы отпустил поминутный лимит.
            time.sleep(2.0 if attempt == 0 else 10.0)
        return data, err

    todo = []
    for row in rows:
        cid = row["id"]
        sig = {r["key"]: r["value"] for r in
               c.execute("SELECT key, value FROM signals WHERE company_id=?", (cid,))}
        cts = c.execute("SELECT kind FROM contacts WHERE company_id=?", (cid,)).fetchall()
        item = dict(row)
        item["brief"] = ai.company_brief(row, sig, cts)
        todo.append(item)

    log("Разбираю в %d поток%s" % (threads,
                                   "" if threads == 1 else
                                   "а" if threads < 5 else "ов"))
    done = 0
    i = 0
    pool = futures.ThreadPoolExecutor(max_workers=threads)
    try:
        pending = {pool.submit(one, item): item for item in todo}
        for fut in futures.as_completed(pending):
            row = pending[fut]
            i += 1
            if _should_stop():
                for f in pending:
                    f.cancel()
                log("Остановлено пользователем.", "warn")
                break
            cid = row["id"]
            try:
                data, err = fut.result()
            except Exception as e:
                data, err = {}, str(e)[:160]
            if err:
                log("[%d/%d] %s — %s" % (i, len(rows), row["name"], err), "warn")
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
            # Запись в базу — только здесь, в одном потоке. SQLite держит
            # соединение на поток, и писать из четырёх разом значит
            # получить «database is locked» на ровном месте.
            if data.get("signals"):
                db.add_signal(cid, "ai_signals", "; ".join(
                    str(x)[:120] for x in list(data["signals"])[:4]))
            if patch:
                db.update_company_fields(cid, patch)
                done += 1
            log("[%d/%d] %s%s" % (i, len(rows), row["name"],
                                  (" — %s" % patch.get("ai_fit", "")) if icp else ""))
            db.update_task(task_id, done=i)
    finally:
        pool.shutdown(wait=False)

    failed = i - done
    log("Разобрано компаний: %d%s" % (done, ", не вышло: %d" % failed
                                      if failed > 0 else ""))
    if failed:
        log("Неразобранные останутся в очереди: запустите «Разобрать» ещё "
            "раз — программа берёт только тех, у кого разбора ещё нет.",
            "warn")


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
    # Близкие слова. Одно слово — одна вывеска: «стоматология» не найдёт
    # «Центр имплантации», хотя это тот же покупатель. Обход по
    # нескольким словам за прогон даёт вдвое-втрое больший улов без
    # единого нового источника, а дубли сводятся тем же механизмом,
    # что и всегда.
    words = (trades.words_for(query) if params.get("synonyms", True)
             else [query])

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    if not query:
        raise RuntimeError("не задано, кого искать")

    # Засечка времени: по ней список умеет показать только тех, кто
    # появился в этом прогоне. Без неё повторный поиск по той же теме
    # приносит ту же тысячу компаний, и новые десять в ней не найти.
    db.set_setting("last_find_at", str(db.now()))

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
    if len(words) > 1:
        log("Заодно близкие слова: %s — это те же покупатели, просто "
            "назвавшие себя иначе." % ", ".join("«%s»" % w for w in words[1:]))

    # Счётчик на время обхода.
    #
    # Раньше total выставлялся только после того, как опрошены все
    # источники по всем городам, — то есть после самой долгой части
    # работы. Всё это время в строке состояния не было ни одной цифры, и
    # программа, которая честно ждёт чужой сервер, выглядела зависшей.
    # Считаем шагами «источник × город»: их число известно заранее.
    steps = 0
    for c in cities:
        per_city = 0
        if want_osm and c.get("ll"):
            per_city += 1
        if want_gis and (c["gis"] or c.get("ll")):
            per_city += 1
        if want_yandex and c.get("ll"):
            per_city += 1
        if want_egrul:
            per_city += 1
        if want_hh and c["hh"]:
            per_city += 1
        # Каждое близкое слово — полный обход источников заново.
        steps += per_city * len(words)
    db.update_task(task_id, total=steps, done=0)
    step = [0]

    def did(what):
        step[0] += 1
        db.update_task(task_id, done=step[0],
                       message="%s · найдено %d" % (what, len(rows)))

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
        # Название — ключ слабый, и годится только вместе с городом.
        # «Дентал» в Москве и «Дентал» в Петербурге — разные компании, а
        # склеивались в одну: у объединённой оставался город первой, и
        # вторая исчезала из выдачи совсем.
        name = norm_name(row.get("name"))
        if name:
            out.append("имя:%s|%s" % (name, (row.get("region") or "").lower()))
        return out

    def full():
        return len(rows) >= limit

    def add(row, source):
        # Справочники отдают сайт так, как его вписала сама компания:
        # с чужими метками перехода («?utm_source=yandex&utm_medium=maps»)
        # и иногда со страницей внутри. В карточке нужен адрес компании,
        # а не след того, откуда мы пришли.
        if row.get("site"):
            row["site"] = site_src.normalize_url(row["site"])
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

        for word in words:
            if _should_stop() or full():
                break
            # Подпись к шагу называет слово, только когда их несколько:
            # иначе в строке состояния дважды повторяется одно и то же.
            where = (city["name"] if len(words) == 1
                     else "%s · %s" % (city["name"], word))
            if want_osm and city.get("ll") and not full():
                _say(task_id, log, "OpenStreetMap · %s" % where)
                for it in osm.search(word, city, session=http, on_log=log,
                                     should_stop=_should_stop):
                    add({"name": it["name"], "site": it["site"],
                         "address": it["address"], "okved_name": it["rubric"],
                         "region": city["name"], "phones": it["phones"],
                         "emails": it["emails"], "links": it["links"]},
                        "OpenStreetMap")
                did("OpenStreetMap · %s" % where)

            if want_gis and (city["gis"] or city.get("ll")) and not full():
                _say(task_id, log, "2ГИС · %s" % where)
                for it in gis2.search(word, city["gis"], gis_key, pages=pages,
                                      session=http, on_log=log,
                                      should_stop=_should_stop,
                                      point=city.get("ll") or ""):
                    add({"name": it["name"], "site": it["site"],
                         "address": it["address"], "okved_name": it["rubric"],
                         "region": city["name"], "phones": it["phones"],
                         "emails": it["emails"]}, "2ГИС")
                did("2ГИС · %s" % where)

            if want_yandex and city.get("ll") and not full():
                _say(task_id, log, "Яндекс · %s" % where)
                for it in yandex.search(word, city, yandex_key, pages=min(pages, 4),
                                        session=http, on_log=log,
                                        should_stop=_should_stop):
                    add({"name": it["name"], "site": it["site"],
                         "address": it["address"], "okved_name": it["rubric"],
                         "region": city["name"], "phones": it["phones"],
                         "emails": [], "links": it["links"]}, "Яндекс")
                did("Яндекс · %s" % where)

            if want_egrul and not full():
                _say(task_id, log, "ЕГРЮЛ · %s" % where)
                region = "" if city["name"] == "Россия целиком" else city["name"]
                for it in dadata.search_by_name(word, dadata_token, region=region,
                                                session=http, on_log=log,
                                                explain=(word == words[0])):
                    add(dict(it, phones=[], emails=[]), "ЕГРЮЛ")
                did("ЕГРЮЛ · %s" % where)

            if want_hh and city["hh"] and not full():
                _say(task_id, log, "hh.ru · %s" % where)
                before = len(errors)
                part = hh.search_employers_by_text(word, area=city["hh"],
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
                did("hh.ru · %s" % where)

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

    # Тощая выдача — не ошибка, но и не норма, и человеку надо сказать,
    # почему так вышло. Молчаливые «3 компании» он читает как поломку
    # программы, хотя на деле выключены два источника из пяти.
    if len(rows) < 15:
        off = []
        if not want_gis:
            off.append("2ГИС")
        if not want_yandex:
            off.append("Яндекс")
        if not want_egrul:
            off.append("ЕГРЮЛ")
        if not want_osm:
            off.append("OpenStreetMap")
        if not want_hh:
            off.append("hh.ru")
        why = ("Не работали: %s — из-за ключей или отказа источника. "
               % ", ".join(off)) if off else ""
        log("Нашлось мало — всего %d. %sПопробуйте слово короче "
            "(«стоматология» вместо «стоматологическая клиника»), добавьте "
            "города или включите недостающие источники." % (len(rows), why),
            "warn")

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

    # По факту находок, а не только новых: обогащение и так берёт лишь
    # тех, у кого его ещё не было, а прерванный прогон иначе оставлял
    # компании пустыми навсегда.
    if params.get("then_enrich") and (added or known) \
            and not _chain_stopped(task_id, "обогащение"):
        db.create_task("enrich", {
            "limit": min(500, max(added + known, 1)), "fns": True,
            "only_lpr": False,
            "zakupki": bool(params.get("then_zakupki")),
            "then_ai": bool(params.get("then_ai")),
        })
        log("Обогащение поставлено в очередь.")
    elif params.get("then_enrich"):
        log("Новых компаний нет — обогащать нечего.", "warn")


def task_socials(task_id, params):
    """Пройтись по компаниям без соцсетей и попробовать их найти.

    Отдельно от обогащения намеренно. Обогащение — долгий проход, в
    котором соцсети лишь одна строка из двадцати; когда нужны именно
    они, гонять весь цикл ради одной строки незачем. Плюс этот проход
    можно натравить на базу, собранную раньше, не трогая всё остальное.

    Два способа, оба — только то, что компания опубликовала сама:
    ссылки с её же сайта и группа ВК, подтверждённая ссылкой на этот
    сайт из самой группы. Догадки не сохраняются: чужая группа в
    карточке хуже пустой клетки — по ней напишут.
    """
    limit = max(1, min(1000, int(params.get("limit") or 100)))
    only_empty = params.get("only_empty", True)

    def log(msg, level="info"):
        db.log(task_id, msg, level)

    c = db.conn()
    where = ""
    if only_empty:
        where = ("AND id NOT IN (SELECT company_id FROM contacts "
                 "WHERE kind='social')")
    rows = c.execute(
        "SELECT id, name, site FROM companies "
        "WHERE COALESCE(site,'') <> '' %s ORDER BY score DESC LIMIT ?"
        % where, (limit,)).fetchall()
    if not rows:
        raise RuntimeError(
            "не нашлось компаний, у которых есть сайт и нет соцсетей. "
            "Соцсети ищутся по сайту и по карточке справочника — без "
            "сайта искать не по чему.")

    db.update_task(task_id, total=len(rows), done=0)
    log("Компаний к проверке: %d" % len(rows))
    vk_token = db.get_setting("vk_token", "")
    if not vk_token:
        log("Сервисный ключ ВК не задан — группа ВК по домену искаться не "
            "будет. Ключ бесплатный: vk.com/apps?act=manage", "warn")

    http = net.plain(browser=True)
    found_total = with_any = 0
    for i, row in enumerate(rows, 1):
        if _should_stop():
            log("Остановлено пользователем.", "warn")
            break
        _say(task_id, log, "%s · найдено %d" % (row["name"][:40], found_total))
        got = 0
        try:
            data = site_src.crawl(row["site"], session=http, max_pages=5,
                                  budget=12)
        except Exception as e:
            data = {}
            log("%s: сайт не открылся (%s)" % (row["name"], str(e)[:70]), "warn")
        for net_name, title, url in social.as_links(data.get("socials") or {}):
            if db.add_contact(row["id"], "social", url, "general", 80,
                              "unchecked", "сайт: %s" % title):
                got += 1
        if vk_token and not (data.get("socials") or {}).get("vk"):
            group, err = vk.by_domain(row["site"], vk_token, session=http,
                                      on_log=log)
            if err:
                log("ВК: %s" % err, "warn")
            elif group:
                if db.add_contact(row["id"], "social", group["url"], "general",
                                  85, "unchecked", "ВК: подтверждена сайтом"):
                    got += 1
        if got:
            found_total += got
            with_any += 1
        db.update_task(task_id, done=i)
        time.sleep(0.2)

    log("Готово. Новых ссылок: %d, компаний с соцсетями стало больше на %d."
        % (found_total, with_any))
    if not found_total:
        log("Ничего не нашлось. Соцсети берутся только оттуда, где компания "
            "их опубликовала сама: со своего сайта и из карточки "
            "справочника. Угадывать программа не станет — чужая группа в "
            "карточке хуже пустой клетки.", "warn")


HANDLERS = {
    "ai": task_ai,
    "socials": task_socials,
    "find": task_find,
    "hh_search": task_hh_search,
    "gis_search": task_gis_search,
    "import": task_import,
    "enrich": task_enrich,
}
