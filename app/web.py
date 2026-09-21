# -*- coding: utf-8 -*-
"""Flask-приложение. Крутится на 127.0.0.1 внутри окна программы.

Это не сайт: снаружи порт не слушается, авторизации нет и не нужно —
программа работает на машине пользователя и видна только ему.
"""
import datetime
import json
import os
import re
import threading
import time

from flask import (Flask, Response, jsonify, render_template, request,
                   send_file)

from . import (ai, db, diag, export, geo, score, settings, trades,
               update, worker)
from .sources import hh


def _open_outside(url, is_path=False):
    """Отдать адрес или путь системе. Возвращает (получилось, ошибка).

    Способов несколько, и это не перестраховка. Модуль webbrowser в
    собранном exe срывается: он ищет браузер по путям и переменным
    окружения, которых внутри сборки нет, и Windows получает вместо
    адреса пустую строку — на экране появляется «Windows не удаётся найти
    "\\"». Поэтому сначала просим систему открыть адрес напрямую, и
    только в самом конце пробуем webbrowser.
    """
    import subprocess
    tried = []

    if is_path and os.name == "nt":
        # Папку открываем проводником напрямую: startfile на каталоге
        # иногда молчит, а explorer берёт путь всегда.
        try:
            subprocess.Popen(["explorer", os.path.normpath(url)])
            return True, ""
        except Exception as e:
            tried.append("explorer: %s" % str(e)[:80])

    if os.name == "nt":
        try:
            os.startfile(url)                    # noqa: S606 — это и есть цель
            return True, ""
        except Exception as e:
            tried.append("startfile: %s" % str(e)[:80])
        try:
            # Обработчик протоколов Windows. Берёт адрес как аргумент
            # целиком, поэтому «?» и «&» в нём не нужно экранировать —
            # в отличие от cmd start, который на «&» ломает команду.
            subprocess.Popen(["rundll32", "url.dll,FileProtocolHandler", url],
                             creationflags=0x08000000)   # без окна консоли
            return True, ""
        except Exception as e:
            tried.append("rundll32: %s" % str(e)[:80])

    try:
        import webbrowser
        if webbrowser.open(url):
            return True, ""
        tried.append("webbrowser: браузер не отозвался")
    except Exception as e:
        tried.append("webbrowser: %s" % str(e)[:80])

    return False, "не удалось открыть браузер (%s)" % "; ".join(tried)


def _exit_soon(delay=1.5):
    """Закрыть программу, дав браузеру получить ответ.

    Выходим через os._exit, а не через штатную остановку сервера: в этот
    момент установщик уже ждёт своей очереди, и корректное завершение
    фоновых задач здесь только тянет время.
    """
    def later():
        import time
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=later, daemon=True).start()


# Когда запустилась эта копия программы. Нужно строке хода работы:
# показывать в ней завершённую задачу имеет смысл, только пока человек
# помнит, что её запускал. Задача, оборвавшаяся на прошлой неделе,
# висела в шапке вечно и читалась как поломка.
STARTED_AT = int(time.time())


def num(value, default, low, high):
    """Число из запроса — с границами и без падения.

    int() на строке «абв» поднимает исключение, и ответом становится
    пятисотая ошибка: снаружи это выглядит как сломанная программа,
    хотя сломано всего лишь одно поле формы. Пустое поле формы приходит
    пустой строкой, а не отсутствующим ключом, поэтому проверять на
    None мало.
    """
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        n = default
    return max(low, min(high, n))


# Что делать дальше на каждой стадии и через сколько дней.
#
# Сроки не выдуманы из головы: «написали» ждёт три дня, потому что
# раньше отвечать обычно не успевают, а позже про письмо забывают обе
# стороны; «созвон» назначается на завтра, потому что договорённость
# протухает за выходные.
STAGE_NEXT = {
    "в работе": ("связаться", 0),
    "написали": ("проверить, ответили ли", 3),
    "созвон": ("созвониться", 1),
}


# Признаки, которые видно в строке списка. Всё остальное — в карточке.
LIST_SIGNALS = ("hh_vacancies", "hh_fresh_days", "tech_calltracking",
                "tech_crm", "tech_telephony", "gis_rubric", "size",
                "revenue", "zakupki_person", "found_by", "cc_why")


def create_app():
    app = Flask(__name__,
                template_folder=settings.resource_path("app", "templates"),
                static_folder=settings.resource_path("app", "static"))
    app.config["JSON_AS_ASCII"] = False

    @app.get("/favicon.ico")
    def favicon():
        """Значок окна. Без маршрута браузер каждый раз получает 404 —
        в журнале это выглядит как ошибка, которой нет."""
        # В сборке значок лежит в корне, в исходниках — в build/.
        for path in (settings.resource_path("icon.ico"),
                     settings.resource_path("build", "icon.ico")):
            if os.path.exists(path):
                return send_file(path, mimetype="image/x-icon")
        return Response(status=204)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            app_name=settings.APP_NAME, version=settings.VERSION,
            areas=hh.AREAS, presets=hh.PRESETS,
            rubrics=trades.all_words(), trades=trades.catalog(),
            dadata_token=db.get_setting("dadata_token", ""),
            gis_key=db.get_setting("gis_key", ""),
            ai_key=db.get_setting("ai_key", ""),
            ai_url=db.get_setting("ai_url", "") or ai.DEFAULT_URL,
            ai_model=db.get_setting("ai_model", "") or ai.DEFAULT_MODEL,
            ai_kind=ai.config()["kind"],
            proxy_url=db.get_setting("proxy_url", ""),
            hh_ua=db.get_setting("hh_ua", ""),
            hh_token=db.get_setting("hh_token", ""),
            update_repo=db.get_setting("update_repo", "") or update.DEFAULT_REPO,
            update_token=db.get_setting("update_token", ""),
            ai_icp=db.get_setting("ai_icp", ""),
            ai_offer=db.get_setting("ai_offer", ""),
            ai_terms=db.get_setting("ai_terms", ""),
            ai_threads=db.get_setting("ai_threads", "") or 4,
            # В скрипт страницы это попадает как есть, поэтому «<»
            # экранируем: запрос человек пишет сам, и «</script>» в нём
            # сломал бы страницу целиком.
            build=settings.BUILD,
            data_dir=settings.data_dir(),
            last_search=(db.get_setting("last_search", "") or "{}").replace("<", "\\u003c"),
            last_find=(db.get_setting("last_find", "") or "{}").replace("<", "\\u003c"),
            geo_cities=geo.cities(),
            geo_groups=geo.groups(),
            geo_whole=geo.WHOLE,
            trade_also=trades.ALSO,
            trade_max_words=trades.MAX_WORDS,
            # Какие источники готовы к работе. Сказать это надо до запуска,
            # а не после: «ничего не нашлось» из-за незаданного ключа —
            # самая обидная из возможных причин.
            has_gis=bool(db.get_setting("gis_key", "")),
            has_dadata=bool(db.get_setting("dadata_token", "")),
            has_yandex=bool(db.get_setting("yandex_key", "")),
            has_vk=bool(db.get_setting("vk_token", "")),
            yandex_key=db.get_setting("yandex_key", ""),
            vk_token=db.get_setting("vk_token", ""),
        )

    # ── Задачи ───────────────────────────────────────────
    @app.post("/api/find")
    def api_find():
        """Поиск по виду деятельности: «стоматология», «грузоперевозки»."""
        d = request.get_json(silent=True) or {}
        query = (d.get("query") or "").strip()
        if not query:
            return jsonify(ok=False, error="впишите, кого ищем")
        # Пустой список городов означал «Россия целиком», а это отключает
        # все справочники разом: они ищут по прямоугольнику на карте, а
        # не по стране. Прийти к такому поиску случайно, сняв выделение,
        # нельзя — это должен быть выбор, а не промах.
        if not [str(c).strip() for c in (d.get("cities") or []) if str(c).strip()]:
            return jsonify(ok=False, error=(
                "выберите хотя бы один город. «Россия целиком» в списке "
                "тоже есть, но она отключает справочники — они ищут по "
                "карте, а не по стране"))
        params = _find_params(d)
        db.set_setting("last_find", json.dumps(params, ensure_ascii=False))
        return jsonify(ok=True, task_id=db.create_task("find", params))

    def _find_params(d):
        """Условия поиска по виду деятельности — как их берёт задача.

        Вынесено отдельно по той же причине, что и у поиска по
        вакансиям: одни и те же условия запускаются, сохраняются под
        именем и повторяются по расписанию. Три места, считающие их
        каждое по-своему, разъезжаются на первой же правке.
        """
        return {
            "query": (d.get("query") or "").strip()[:120],
            "cities": [str(c) for c in (d.get("cities") or [])][:14],
            "pages": num(d.get("pages"), 3, 1, 10),
            "limit": num(d.get("limit"), 200, 10, 5000),
            "sources": {
                "osm": bool(d.get("osm", True)),
                "gis": bool(d.get("gis", True)),
                "yandex": bool(d.get("yandex", True)),
                "dadata": bool(d.get("dadata", True)),
                "hh": bool(d.get("hh", True)),
            },
            "synonyms": bool(d.get("synonyms", True)),
            "skip_empty": bool(d.get("skip_empty", True)),
            "then_enrich": bool(d.get("then_enrich")),
            "then_zakupki": bool(d.get("then_zakupki")),
            "then_ai": bool(d.get("then_ai")),
        }

    def _search_params(d):
        """Условия поиска из формы — в том виде, в каком их берёт задача.

        Вынесено отдельно, потому что те же условия и запускаются, и
        сохраняются под именем, и повторяются позже. Три места, считающие
        их каждое по-своему, разъезжаются на первой же правке.
        """
        queries = [q.strip() for q in (d.get("queries") or []) if q.strip()]
        if not queries:
            queries = [(d.get("text") or "").strip() or hh.PRESETS["Отдел продаж"]]
        areas = [str(a) for a in (d.get("areas") or []) if str(a).strip()]
        return {
            "queries": queries[:12],
            "areas": areas[:8] or [str(d.get("area") or "113")],
            "period": num(d.get("period"), 30, 1, 30),
            "pages": num(d.get("pages"), 5, 1, 20),
            "in_title": bool(d.get("in_title", True)),
            "skip_agencies": bool(d.get("skip_agencies", True)),
            "max_open": num(d.get("max_open"), 0, 0, 5000),
            "then_enrich": bool(d.get("then_enrich")),
            "then_zakupki": bool(d.get("then_zakupki")),
            "then_ai": bool(d.get("then_ai")),
        }

    @app.post("/api/search")
    def api_search():
        d = request.get_json(silent=True) or {}
        params = _search_params(d)
        task_id = db.create_task("hh_search", params)
        # Последние условия запоминаются всегда: программу закрыли,
        # открыли — и форма та же, что вчера, а не пустая.
        db.set_setting("last_search", json.dumps(params, ensure_ascii=False))
        if d.get("search_id"):
            db.mark_search_run(num(d.get("search_id"), 0, 0, 2**31))
        return jsonify(ok=True, task_id=task_id, queries=len(params["queries"]))

    # ── Сохранённые поиски ───────────────────────────────
    @app.get("/api/searches")
    def api_searches():
        return jsonify(ok=True, rows=db.list_searches())

    @app.post("/api/searches")
    def api_searches_save():
        d = request.get_json(silent=True) or {}
        name = (d.get("name") or "").strip()
        if not name:
            return jsonify(ok=False, error="без названия набор не найти потом")
        kind = "find" if (d.get("kind") == "find") else "hh_search"
        params = _find_params(d) if kind == "find" else _search_params(d)
        if kind == "find" and not params["query"]:
            return jsonify(ok=False, error="впишите, кого ищем")
        if kind == "find" and not params["cities"]:
            return jsonify(ok=False, error="выберите хотя бы один город")
        sid = db.save_search(name, params, kind=kind,
                             every_days=num(d.get("every_days"), 0, 0, 365))
        return jsonify(ok=True, id=sid, rows=db.list_searches())

    @app.post("/api/searches/<int:sid>/plan")
    def api_searches_plan(sid):
        """Поменять расписание, не трогая сами условия."""
        d = request.get_json(silent=True) or {}
        every = (None if d.get("every_days") is None
                 else num(d.get("every_days"), 0, 0, 365))
        on = None if d.get("enabled") is None else bool(d.get("enabled"))
        row = db.set_search_plan(sid, every_days=every, enabled=on)
        if row is None:
            return jsonify(ok=False, error="такого набора уже нет")
        return jsonify(ok=True, rows=db.list_searches())

    @app.post("/api/searches/<int:sid>/run")
    def api_searches_run(sid):
        """Запустить сохранённый набор прямо сейчас."""
        row = db.get_search(sid)
        if row is None:
            return jsonify(ok=False, error="такого набора уже нет")
        task_id = db.create_task(row["kind"] or "hh_search", row["params"])
        db.mark_search_run(sid)
        return jsonify(ok=True, task_id=task_id, rows=db.list_searches())

    @app.post("/api/searches/<int:sid>/delete")
    def api_searches_delete(sid):
        db.delete_search(sid)
        return jsonify(ok=True, rows=db.list_searches())

    @app.post("/api/gis")
    def api_gis():
        d = request.get_json(silent=True) or {}
        task_id = db.create_task("gis_search", {
            "query": (d.get("query") or "").strip(),
            # Город приходит названием: номер региона есть не у всех, и
            # для остальных нужны координаты — искать по ним 2ГИС умеет.
            "city": (d.get("region") or "").strip(),
            "pages": num(d.get("pages"), 2, 1, 10),
        })
        return jsonify(ok=True, task_id=task_id)

    @app.post("/api/import")
    def api_import():
        d = request.get_json(silent=True) or {}
        task_id = db.create_task("import", {"text": d.get("text") or ""})
        return jsonify(ok=True, task_id=task_id)

    @app.post("/api/enrich")
    def api_enrich():
        d = request.get_json(silent=True) or {}
        task_id = db.create_task("enrich", {
            "limit": num(d.get("limit"), 50, 1, 500),
            "verify": bool(d.get("verify")),
            "fns": d.get("fns", True),
            "vk": d.get("vk", True),
            "only_lpr": bool(d.get("only_lpr")),
            "zakupki": bool(d.get("zakupki")),
        })
        return jsonify(ok=True, task_id=task_id)

    @app.post("/api/ai")
    def api_ai():
        d = request.get_json(silent=True) or {}
        db.set_setting("ai_icp", (d.get("icp") or "").strip())
        db.set_setting("ai_offer", (d.get("offer") or "").strip())
        db.set_setting("ai_terms", (d.get("terms") or "").strip())
        threads = num(d.get("threads"), 4, 1, 8)
        db.set_setting("ai_threads", str(threads))
        task_id = db.create_task("ai", {
            "limit": num(d.get("limit"), 30, 1, 300),
            "icp": d.get("icp") or "", "offer": d.get("offer") or "",
            "redo": bool(d.get("redo")), "threads": threads,
        })
        return jsonify(ok=True, task_id=task_id)

    @app.post("/api/socials")
    def api_socials():
        """Отдельный проход за соцсетями по уже собранной базе."""
        d = request.get_json(silent=True) or {}
        return jsonify(ok=True, task_id=db.create_task("socials", {
            "limit": num(d.get("limit"), 100, 1, 1000),
            "only_empty": bool(d.get("only_empty", True)),
        }))

    @app.post("/api/ai/check")
    def api_ai_check():
        cfg = ai.config()
        ok, note = ai.check(cfg)
        # Если отказали из-за имени модели — сразу берём список и
        # показываем его. Посредник в этом же отказе пишет «получите
        # список моделей»; заставлять человека жать вторую кнопку ради
        # того, что программа умеет сделать сама, — лишний ход.
        names = []
        if not ok and ai.model_missing(note):
            names, _err = ai.models(cfg)
            if names:
                note += "\n\nДоступные модели: %s%s\nВыберите одну в поле " \
                        "«Модель» — список уже подставлен в подсказку." % (
                            ", ".join(names[:12]),
                            " и ещё %d" % (len(names) - 12) if len(names) > 12
                            else "")
        # Формат называем всегда: половина неудач здесь — не тот формат,
        # а по сообщению «HTTP 404» этого не понять.
        return jsonify(ok=ok, note=note, model=cfg["model"],
                       kind=cfg["kind"], models=names)

    @app.get("/api/ai/models")
    def api_ai_models():
        """Какие модели доступны этому ключу.

        У посредников названия свои, и заставлять человека искать их в
        чужой документации ради одной строки настроек — лишний шаг.
        """
        names, err = ai.models()
        return jsonify(ok=not err, error=err, models=names,
                       kind=ai.config()["kind"])

    @app.post("/api/ai/queries")
    def api_ai_queries():
        """Подсказать, чем искать таких клиентов.

        Человек описывает клиента словами, а искать надо запросами — между
        этим лежит шаг, на котором обычно и промахиваются.
        """
        d = request.get_json(silent=True) or {}
        icp = (d.get("icp") or "").strip()
        if not icp:
            return jsonify(ok=False, error="опишите, кого ищете")
        data, err = ai.suggest_queries(icp)
        if err:
            return jsonify(ok=False, error=err)
        db.set_setting("ai_icp", icp)
        return jsonify(ok=True, **data)

    @app.post("/api/diag")
    def api_diag():
        """Проверка источников по очереди.

        «Ничего не находит» имеет десяток причин, и все они выглядят
        одинаково, пока каждый источник не спросить отдельно.
        """
        return jsonify(ok=True, rows=diag.run())

    @app.get("/api/update/check")
    def api_update_check():
        info, err = update.check()
        return jsonify(ok=not err, error=err, **info)

    @app.post("/api/update/apply")
    def api_update_apply():
        """Обновление тем способом, который подходит этой копии.

        Адреса берём заново, а не из тела запроса: окно настроек может
        висеть открытым с утра, и ссылка на релиз за это время протухнет.
        """
        info, err = update.check()
        if err:
            return jsonify(ok=False, error=err)
        ok, msg, restart = update.run(info)
        if ok and restart:
            _exit_soon()
        return jsonify(ok=ok, message=msg, restart=bool(ok and restart))

    @app.post("/api/open")
    def api_open():
        """Открыть ссылку в браузере, а не внутри окна программы.

        Окно программы — это webview без адресной строки и без кнопки
        «назад». Ссылка, открытая в нём, уводит человека на чужой сайт без
        дороги обратно: остаётся закрывать программу целиком. Поэтому все
        внешние ссылки уходят сюда, а отсюда — в системный браузер.
        """
        url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return jsonify(ok=False, error="ссылка не похожа на адрес", url=url)
        ok, err = _open_outside(url)
        # Адрес возвращаем всегда: не открылось — человек хотя бы скопирует.
        return jsonify(ok=ok, error=err, url=url)

    @app.post("/api/reveal")
    def api_reveal():
        """Открыть папку с данными. База и выгрузки лежат там же."""
        ok, err = _open_outside(settings.data_dir(), is_path=True)
        return jsonify(ok=ok, error=err, path=settings.data_dir())

    @app.post("/api/stop")
    def api_stop():
        worker.stop_all()
        return jsonify(ok=True)

    @app.get("/api/task")
    def api_task():
        # Поток обхода мог умереть — например, база была занята в
        # неудачный момент. Молча оставлять его мёртвым нельзя: задачи
        # будут стоять в очереди вечно, а программа выглядеть зависшей.
        if not worker.alive():
            worker.start()
        c = db.conn()
        # Показываем идущую задачу, а если её нет — последнюю. Иначе,
        # поставив обогащение в очередь следом за поиском, человек видит
        # завершённый поиск и думает, что всё встало.
        row = c.execute("SELECT * FROM tasks WHERE status IN ('running','queued') "
                        "ORDER BY id LIMIT 1").fetchone()
        if row is None:
            # Только то, что закончилось при этом запуске. Иначе строка
            # «остановлено · 0 из 3» от прошлого раза встречает человека
            # при каждом открытии программы и выглядит как поломка.
            row = c.execute("SELECT * FROM tasks WHERE updated_at >= ? "
                            "ORDER BY id DESC LIMIT 1",
                            (STARTED_AT,)).fetchone()
        queued = c.execute("SELECT COUNT(*) n FROM tasks "
                           "WHERE status='queued'").fetchone()["n"]
        if row is None:
            return jsonify(ok=True, task=None, logs=[], queued=0)
        logs = c.execute("""SELECT level, text, created_at FROM logs
                            WHERE task_id=? ORDER BY id DESC LIMIT 80""",
                         (row["id"],)).fetchall()
        # Очередь списком, а не числом. «В очереди ещё 2» не говорит ни
        # что это, ни как их отменить: человек, передумавший на середине,
        # мог только ждать, пока программа доделает то, что он уже не
        # хочет.
        return jsonify(ok=True, task=dict(row), queued=queued,
                       queue=db.queued_tasks(),
                       logs=[dict(x) for x in reversed(logs)])

    @app.get("/api/tasks")
    def api_tasks():
        """Последние задачи: что шло, чем кончилось и сколько заняло."""
        return jsonify(ok=True, rows=db.recent_tasks())

    @app.post("/api/task/<int:tid>/cancel")
    def api_task_cancel(tid):
        return jsonify(ok=True, cancelled=db.cancel_task(tid),
                       queue=db.queued_tasks())

    @app.post("/api/queue/clear")
    def api_queue_clear():
        return jsonify(ok=True, cancelled=db.cancel_queued(),
                       queue=db.queued_tasks())

    # ── Данные ───────────────────────────────────────────
    def _company_where(q="", only="", ids=""):
        """Условие выборки по тому, что человек видит на экране.

        Тот же фильтр нужен выгрузке: отдавать в Excel всю базу, когда на
        экране отобраны двадцать подходящих компаний, — значит заставить
        человека фильтровать второй раз, уже в Excel.
        """
        where, args = [], []
        if q:
            # ОГРН ищется наравне с ИНН: в выписке и в договоре стоят оба,
            # и какой из них под рукой — дело случая.
            where.append("(name LIKE ? OR director LIKE ? OR inn LIKE ? "
                         "OR ogrn LIKE ? OR site LIKE ?)")
            args += ["%%%s%%" % q] * 5
        picked = [int(x) for x in str(ids or "").split(",") if x.strip().isdigit()]
        if picked:
            where.append("id IN (%s)" % ",".join("?" * len(picked)))
            args += picked
        if only == "director":
            where.append("id IN (SELECT company_id FROM contacts "
                         "WHERE kind='email' AND owner='director')")
        elif only == "calltracking":
            where.append("id IN (SELECT company_id FROM signals WHERE key='tech_calltracking')")
        elif only == "fit":
            where.append("id IN (SELECT company_id FROM signals "
                         "WHERE key='size' AND value IN ('малый','средний'))")
        elif only == "callcenter":
            where.append("callcenter IN ('да','вероятно')")
        elif only == "lpr_found":
            where.append("id IN (SELECT company_id FROM signals "
                         "WHERE key='lpr_contact' AND value='найден')")
        elif only == "just_found":
            # Компании, появившиеся в последнем прогоне поиска. Повторный
            # поиск по той же теме приносит ту же тысячу компаний, и
            # десять новых в ней не найти глазами.
            since = db.get_setting("last_find_at", "")
            where.append("COALESCE(created_at,0) >= ?")
            args.append(int(since) if str(since).isdigit() else 0)
        elif only == "ai_fit":
            where.append("ai_fit >= 60")
        elif only == "zakupki":
            where.append("id IN (SELECT company_id FROM signals WHERE key='zakupki_person')")
        elif only == "contactable":
            # Компания без единого способа связи — не лид, а строка в
            # реестре. Держать её в общем списке можно, показывать первой
            # нельзя.
            where.append("(COALESCE(site,'') <> '' OR id IN (SELECT company_id FROM contacts "
                         "WHERE kind IN ('phone','email','social')))")
        elif only == "empty":
            # COALESCE, а не просто site = ''. Компания, пришедшая без
            # сайта вовсе, хранит в этом поле NULL, а NULL = '' в SQL не
            # истина и не ложь — сравнение просто не срабатывает. Фильтр
            # «Пустые» из-за этого не показывал ни одной пустой компании,
            # то есть ровно тех, ради кого он и сделан.
            where.append("(COALESCE(site,'') = '' AND id NOT IN (SELECT company_id FROM "
                         "contacts WHERE kind IN ('phone','email','social')))")
        elif only == "today":
            # Просроченное — тоже на сегодня: вчерашний звонок, который
            # не сделали, не становится менее нужным.
            where.append("next_date <> '' AND next_date IS NOT NULL "
                         "AND next_date <= date('now','localtime')")
        elif only == "social":
            where.append("id IN (SELECT company_id FROM contacts WHERE kind='social')")
        elif only == "fresh":
            # Вакансия, вывешенная на этой неделе: повод для звонка ещё
            # горячий, и о нём можно говорить в настоящем времени.
            where.append("id IN (SELECT company_id FROM signals "
                         "WHERE key='hh_fresh_days' AND CAST(value AS INTEGER) <= 7)")
        return " AND ".join(where), args

    @app.get("/api/companies")
    def api_companies():
        c = db.conn()
        q = (request.args.get("q") or "").strip()
        only = request.args.get("only") or ""
        cond, args = _company_where(q, only, request.args.get("ids") or "")
        # Только те колонки, которые видно в строке. «SELECT *» тянул и
        # составленное КП — до шести килобайт на компанию, — и черновик
        # письма, и список учредителей: всё это едет в ответе лишь затем,
        # чтобы браузер его выбросил. На сотне строк это половина веса
        # ответа, а ответ приходит заново, пока идёт обход.
        sql = ("SELECT id, name, inn, ogrn, site, region, director, "
               "director_post, score, stage, callcenter, ai_fit, "
               "next_step, next_date FROM companies")
        if cond:
            sql += " WHERE " + cond
        # Сколько строк отдавать. Интерфейс просит ровно столько, сколько
        # рисует: пятьсот строк со всеми контактами — это мегабайт JSON
        # на каждый опрос, и разбирает его тот же поток, который рисует
        # окно.
        want = num(request.args.get("limit"), 200, 20, 2000)
        sql += " ORDER BY score DESC, id LIMIT %d" % want

        # Три запроса вместо тысячи.
        #
        # Раньше на каждую из пятисот строк делалось по два отдельных
        # запроса за контактами и сигналами. Пока список открыт во время
        # работы задачи, он перезапрашивается постоянно — и эта тысяча
        # запросов соревновалась за базу с тем, что как раз в неё пишет.
        # Снаружи это выглядело как зависшая программа.
        rows = c.execute(sql, args).fetchall()
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        cts_by, sig_by = {}, {}
        if ids:
            for r in c.execute(
                    "SELECT company_id, kind, value, owner, confidence, verified, "
                    "source FROM contacts WHERE company_id IN (%s) "
                    "ORDER BY confidence DESC" % marks, ids):
                cts_by.setdefault(r["company_id"], []).append(dict(r))
            # Признаков у обогащённой компании десятки, а в строке
            # таблицы видно восемь. Остальные — ряды выручки по годам,
            # тексты вакансий, описания — едут в ответе только чтобы
            # быть выброшенными, и весят больше всего остального.
            for r in c.execute(
                    "SELECT company_id, key, value FROM signals "
                    "WHERE company_id IN (%s) AND key IN (%s)"
                    % (marks, ",".join("?" * len(LIST_SIGNALS))),
                    ids + list(LIST_SIGNALS)):
                sig_by.setdefault(r["company_id"], {})[r["key"]] = r["value"]
        # В строке таблицы видно три контакта, остальные — в карточке.
        # Отдавать все значило слать по три сотни штук на компанию: у
        # обойдённого сайта их набирается столько, и ответ на сотню строк
        # разрастался до трёхсот килобайт. Он приходит заново каждые
        # несколько секунд, пока идёт обход.
        out = []
        for row in rows:
            all_cts = cts_by.get(row["id"], [])
            socials = [x for x in all_cts if x["kind"] == "social"][:6]
            rest = [x for x in all_cts if x["kind"] != "social"][:6]
            out.append(dict(row, contacts=rest + socials,
                            contacts_total=len(all_cts),
                            signals=sig_by.get(row["id"], {})))
        total = c.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"]
        # Сколько строк подходит под фильтр — считаем отдельно, иначе
        # «показать ещё» не знает, есть ли что показывать.
        csql = "SELECT COUNT(*) n FROM companies"
        if cond:
            csql += " WHERE " + cond
        matched = c.execute(csql, args).fetchone()["n"]
        return jsonify(ok=True, rows=out, total=total, shown=matched,
                       returned=len(out))

    @app.get("/api/score/legend")
    def api_score_legend():
        """Из чего вообще складывается балл — вне привязки к компании."""
        return jsonify(ok=True, legend=score.legend(), max=100)

    @app.get("/api/company/<int:cid>")
    def api_company(cid):
        """Полная карточка — то, что не влезает в строку таблицы."""
        c = db.conn()
        row = c.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if row is None:
            return jsonify(ok=False, error="не найдено"), 404
        cts = c.execute("""SELECT kind, value, owner, confidence, verified, source
                           FROM contacts WHERE company_id=?
                           ORDER BY confidence DESC""", (cid,)).fetchall()
        sig = {r["key"]: r["value"] for r in
               c.execute("SELECT key, value FROM signals WHERE company_id=?", (cid,))}
        # Из чего сложился балл. Считаем на лету, а не храним: правила
        # меняются от версии к версии, и сохранённое объяснение начнёт
        # расходиться с числом, которое лежит рядом в той же строке.
        score_now, score_parts = score.compute(dict(row), sig,
                                               [dict(x) for x in cts])
        # Ссылки на поиск по ФИО отдаются, но не сохраняются: программа по
        # ним не ходит. Автоматически собранная база личных страниц — это
        # профилирование частного лица, а по имени ещё и ненадёжно.
        from . import social
        from .sources import site as site_src
        out = []
        for x in cts:
            x = dict(x)
            if x["kind"] == "phone":
                x["note"] = site_src.phone_kind(x["value"])
            out.append(x)
        return jsonify(ok=True, company=dict(row), contacts=out, signals=sig,
                       notes=db.notes(cid), score_parts=score_parts,
                       score_now=score_now,
                       search=[{"title": t, "url": u} for t, u in
                               social.search_links(row["director"], row["name"])])

    STAGES = ["new", "в работе", "написали", "созвон", "отказ"]

    @app.get("/api/board")
    def api_board():
        """Компании по стадиям — для воронки.

        Отдаём коротко: на доске видно имя, балл, один контакт и
        следующий шаг. Всё остальное открывается в карточке, и тащить
        его в каждую колонку значит везти мегабайты ради эскизов.
        """
        c = db.conn()
        per = num(request.args.get("per"), 20, 5, 200)
        out = {}
        for stage in STAGES:
            rows = c.execute(
                "SELECT id, name, score, director, next_step, next_date, region "
                "FROM companies WHERE COALESCE(stage,'new')=? "
                "ORDER BY score DESC, id LIMIT ?", (stage, per)).fetchall()
            total = c.execute("SELECT COUNT(*) n FROM companies "
                              "WHERE COALESCE(stage,'new')=?", (stage,)).fetchone()["n"]
            ids = [r["id"] for r in rows]
            first = {}
            if ids:
                marks = ",".join("?" * len(ids))
                for r in c.execute(
                        "SELECT company_id, kind, value FROM contacts "
                        "WHERE company_id IN (%s) ORDER BY confidence DESC" % marks,
                        ids):
                    first.setdefault(r["company_id"], (r["kind"], r["value"]))
            out[stage] = {
                "total": total,
                "cards": [dict(r, contact=first.get(r["id"], ("", ""))[1],
                               contact_kind=first.get(r["id"], ("", ""))[0])
                          for r in rows],
            }
        return jsonify(ok=True, stages=STAGES, board=out)

    @app.get("/api/today")
    def api_today():
        """Что делать сегодня и что происходит с базой."""
        c = db.conn()
        due = [dict(r) for r in c.execute(
            "SELECT id, name, score, next_step, next_date, director "
            "FROM companies WHERE next_date <> '' AND next_date IS NOT NULL "
            "AND next_date <= date('now','localtime') "
            "ORDER BY next_date, score DESC LIMIT 50")]
        fresh = [dict(r) for r in c.execute(
            "SELECT id, name, score, region, activity FROM companies "
            "ORDER BY id DESC LIMIT 8")]
        по_стадиям = {r["s"]: r["n"] for r in c.execute(
            "SELECT COALESCE(stage,'new') s, COUNT(*) n FROM companies GROUP BY 1")}
        # Кому звонить, если на сегодня ничего не назначено.
        #
        # Главный экран программы при полной базе сообщал «ничего не
        # назначено» и оставлял человека одного: дальше он шёл в базу и
        # сортировал её глазами. Но кому звонить первым, программа знает
        # — за это и считался балл. Берём тех, до кого ещё не дошли руки,
        # у кого есть чем связаться, и показываем сразу с телефоном.
        suggest = [dict(r) for r in c.execute("""
            SELECT c.id, c.name, c.score, c.director, c.region,
                   (SELECT value FROM contacts t WHERE t.company_id=c.id
                     AND t.kind='phone'
                     ORDER BY (t.owner='director') DESC, t.confidence DESC
                     LIMIT 1) phone,
                   (SELECT value FROM contacts t WHERE t.company_id=c.id
                     AND t.kind='email'
                     ORDER BY (t.owner='director') DESC, t.confidence DESC
                     LIMIT 1) email
            FROM companies c
            WHERE COALESCE(c.stage,'new')='new'
              AND COALESCE(c.next_date,'')=''
              AND EXISTS (SELECT 1 FROM contacts t WHERE t.company_id=c.id)
            ORDER BY c.score DESC, c.id DESC LIMIT 8""")]
        return jsonify(ok=True, due=due, fresh=fresh, stages=по_стадиям,
                       suggest=suggest)

    @app.get("/api/stats")
    def api_stats():
        c = db.conn()
        one = lambda sql: c.execute(sql).fetchone()[0]
        return jsonify(
            ok=True,
            companies=one("SELECT COUNT(*) FROM companies"),
            with_director=one("SELECT COUNT(*) FROM companies WHERE director<>''"),
            with_dir_mail=one("SELECT COUNT(DISTINCT company_id) FROM contacts "
                              "WHERE kind='email' AND owner='director'"),
            lpr_found=one("SELECT COUNT(*) FROM signals "
                          "WHERE key='lpr_contact' AND value='найден'"),
            callcenter=one("SELECT COUNT(*) FROM companies "
                           "WHERE callcenter IN ('да','вероятно')"),
            ai_done=one("SELECT COUNT(*) FROM companies "
                        "WHERE ai_summary IS NOT NULL AND ai_summary<>''"),
            ai_fit=one("SELECT COUNT(*) FROM companies WHERE ai_fit >= 60"),
            verified=one("SELECT COUNT(DISTINCT company_id) FROM contacts "
                         "WHERE kind='email' AND verified='ok'"),
            hot=one("SELECT COUNT(*) FROM companies WHERE score>=60"),
        )

    @app.post("/api/company/<int:cid>")
    def api_company_update(cid):
        d = request.get_json(silent=True) or {}
        patch = {k: d[k] for k in ("stage", "note", "next_step", "next_date")
                 if k in d}
        # Дата приходит из поля ввода в виде ГГГГ-ММ-ДД; всё остальное —
        # не дата, и в базу ему попадать незачем: по этому полю идёт
        # отбор «на сегодня».
        if "next_date" in patch:
            val = (patch["next_date"] or "").strip()
            patch["next_date"] = val if re.match(r"^\d{4}-\d{2}-\d{2}$", val) else ""
        # Смена стадии назначает следующий шаг, если его ещё нет.
        #
        # Воронка и «Сегодня» до сих пор не разговаривали: карточку
        # перетаскивали в «созвон», а на экране «Сегодня» не появлялось
        # ничего — срок надо было проставить руками, отдельно, в другой
        # карточке. В итоге экран, отвечающий на вопрос «кому звонить»,
        # у большинства оставался пустым, а стадия жила сама по себе.
        #
        # Своё не перетираем: если человек уже написал, что делать и
        # когда, его слово сильнее нашего умолчания.
        if patch.get("stage") and "next_date" not in patch:
            cur = db.conn().execute(
                "SELECT next_step, next_date FROM companies WHERE id=?",
                (cid,)).fetchone()
            if patch["stage"] == "отказ":
                # Отказ — конец разговора, и висеть в списке на сегодня
                # компания больше не должна. Снимаем срок всегда, а не
                # только когда его нет: смысл как раз в том, чтобы убрать
                # уже назначенный.
                patch["next_date"] = ""
                patch["next_step"] = ""
            elif cur is not None and not (cur["next_date"] or "").strip():
                step, days = STAGE_NEXT.get(patch["stage"], ("", None))
                if days is not None:
                    patch["next_date"] = (datetime.date.today() +
                                          datetime.timedelta(days=days)).isoformat()
                    if not (cur["next_step"] or "").strip():
                        patch["next_step"] = step
        if patch:
            db.update_company_fields(cid, patch)
        return jsonify(ok=True)

    @app.post("/api/settings")
    def api_settings():
        d = request.get_json(silent=True) or {}
        for key in ("dadata_token", "gis_key", "ai_key", "ai_url",
                    "ai_model", "hh_ua", "hh_token",
                    "update_repo", "update_token", "update_url",
                    "yandex_key", "vk_token", "ai_kind", "proxy_url"):
            if key in d:
                db.set_setting(key, (d[key] or "").strip())
        return jsonify(ok=True)

    def _company_facts(cid):
        """Компания, её признаки и контакты — вход для любого разбора."""
        c = db.conn()
        row = c.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if row is None:
            return None, None, None
        sig = {r["key"]: r["value"] for r in
               c.execute("SELECT key, value FROM signals WHERE company_id=?", (cid,))}
        cts = [dict(x) for x in c.execute(
            "SELECT kind, value, owner, confidence, verified, source "
            "FROM contacts WHERE company_id=? ORDER BY confidence DESC", (cid,))]
        return dict(row), sig, cts

    @app.post("/api/company/<int:cid>/analyze")
    def api_company_analyze(cid):
        """Разобрать одну компанию сейчас, не дожидаясь общего прогона.

        Общий прогон идёт по тридцати карточкам и занимает минуты. Когда
        открыта одна и звонить по ней надо сегодня, ждать незачем.
        """
        row, sig, cts = _company_facts(cid)
        if row is None:
            return jsonify(ok=False, error="компания не найдена")
        if not db.get_setting("ai_key", ""):
            return jsonify(ok=False, error="не задан ключ ИИ — «Настройки» → «Ключ ИИ»")
        brief = ai.company_brief(row, sig, cts)
        data, err = ai.analyze(brief,
                               icp=db.get_setting("ai_icp", ""),
                               offer=db.get_setting("ai_offer", ""))
        if err:
            return jsonify(ok=False, error=err)
        patch = {}
        if data.get("summary"):
            patch["ai_summary"] = str(data["summary"])[:400]
        if data.get("segment"):
            patch["ai_segment"] = str(data["segment"])[:40]
        if data.get("fit") is not None:
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
        if patch:
            db.update_company_fields(cid, patch)
        if isinstance(data.get("signals"), list) and data["signals"]:
            db.add_signal(cid, "ai_signals",
                          "; ".join(str(x) for x in data["signals"][:4])[:600])
        return jsonify(ok=True, **patch)

    @app.post("/api/company/<int:cid>/kp")
    def api_company_kp(cid):
        """Коммерческое предложение под эту компанию."""
        row, sig, cts = _company_facts(cid)
        if row is None:
            return jsonify(ok=False, error="компания не найдена")
        if not db.get_setting("ai_key", ""):
            return jsonify(ok=False, error="не задан ключ ИИ — «Настройки» → «Ключ ИИ»")
        data, err = ai.kp(row, sig, cts,
                          offer=db.get_setting("ai_offer", ""),
                          terms=db.get_setting("ai_terms", ""),
                          icp=db.get_setting("ai_icp", ""))
        if err:
            return jsonify(ok=False, error=err)
        plain = ai.kp_text(data, row.get("name") or "")
        # КП пишут раз и возвращаются к нему: пересылают, правят, читают
        # перед звонком. Держать его только на экране значит потерять при
        # первом же закрытии карточки.
        db.update_company_fields(cid, {"ai_kp": plain[:6000]})
        return jsonify(ok=True, text=plain, **data)

    @app.post("/api/company/<int:cid>/letter")
    def api_company_letter(cid):
        """Первое письмо этой компании — по уже собранным фактам."""
        c = db.conn()
        row = c.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        if row is None:
            return jsonify(ok=False, error="компания не найдена")
        if not db.get_setting("ai_key", ""):
            return jsonify(ok=False, error="не задан ключ ИИ — «Настройки» → «Ключ ИИ»")
        sig = {r["key"]: r["value"] for r in
               c.execute("SELECT key, value FROM signals WHERE company_id=?", (cid,))}
        cts = [dict(x) for x in c.execute(
            "SELECT kind, value, owner, confidence, verified, source "
            "FROM contacts WHERE company_id=? ORDER BY confidence DESC", (cid,))]
        letter, err = ai.letter(dict(row), sig, cts,
                                offer=db.get_setting("ai_offer", ""),
                                icp=db.get_setting("ai_icp", ""))
        if err:
            return jsonify(ok=False, error=err)
        # Кому писать: найденный адрес руководителя лучше общего ящика.
        to = ""
        for x in cts:
            if x["kind"] == "email" and x["owner"] == "director":
                to = x["value"]
                break
        if not to:
            to = next((x["value"] for x in cts if x["kind"] == "email"), "")
        return jsonify(ok=True, to=to, **letter)

    @app.post("/api/company/<int:cid>/note")
    def api_company_note(cid):
        d = request.get_json(silent=True) or {}
        if d.get("delete"):
            # Заметку удаляем только у той компании, чья карточка
            # открыта: номер приходит из запроса, и брать его на веру
            # значит позволить стереть чужую запись по опечатке.
            db.delete_note(num(d.get("delete"), 0, 0, 2**31), company_id=cid)
        else:
            if not db.add_note(cid, d.get("text") or ""):
                return jsonify(ok=False, error="пустая заметка")
        return jsonify(ok=True, notes=db.notes(cid))

    @app.post("/api/company/<int:cid>/delete")
    def api_company_delete(cid):
        """Убрать компанию. С пометкой — чтобы не вернулась поиском."""
        d = request.get_json(silent=True) or {}
        if d.get("blacklist"):
            db.blacklist_add(cid, (d.get("reason") or "")[:200])
        db.delete_company(cid)
        return jsonify(ok=True)

    @app.post("/api/bulk")
    def api_bulk():
        """Действие над пачкой сразу.

        При обзвоне отмечать полсотни отказов по одному — это отдельная
        работа, которую никто делать не станет, и стадии перестают
        отражать действительность.
        """
        d = request.get_json(silent=True) or {}
        # Через int() напрямую нельзя: один нечисловой элемент в списке
        # роняет весь запрос, и вместо «ничего не выбрано» человек
        # получает пятисотую ошибку.
        ids = [int(x) for x in (d.get("ids") or [])
               if str(x).strip().lstrip("-").isdigit()][:2000]
        action = d.get("action") or ""
        if not ids:
            return jsonify(ok=False, error="ничего не выбрано")
        if action == "stage":
            for cid in ids:
                db.update_company_fields(cid, {"stage": d.get("stage") or "new"})
        elif action == "delete":
            for cid in ids:
                if d.get("blacklist"):
                    db.blacklist_add(cid, "массовое удаление")
                db.delete_company(cid)
        elif action == "blacklist":
            for cid in ids:
                db.blacklist_add(cid, (d.get("reason") or "отказ")[:200])
        else:
            return jsonify(ok=False, error="неизвестное действие")
        return jsonify(ok=True, count=len(ids))

    @app.get("/api/blacklist")
    def api_blacklist():
        rows = db.conn().execute(
            "SELECT key, name, reason, created_at FROM blacklist "
            "ORDER BY created_at DESC LIMIT 500").fetchall()
        total = db.conn().execute("SELECT COUNT(*) n FROM blacklist").fetchone()["n"]
        return jsonify(ok=True, rows=[dict(r) for r in rows], total=total)

    @app.post("/api/dedupe")
    def api_dedupe():
        """Найти и склеить дубли по всей базе.

        Внутри одного прогона записи сводятся сразу, между прогонами —
        нет: сегодня компания пришла из карты без ИНН, завтра из ЕГРЮЛ с
        ИНН, и это две строки.
        """
        pairs = db.find_duplicates()
        if (request.get_json(silent=True) or {}).get("dry"):
            return jsonify(ok=True, found=len(pairs))
        done = 0
        for keep, drop in pairs:
            if db.merge_companies(keep, drop):
                done += 1
        # Заодно выкидываем телефоны-заглушки, попавшие в базу до того,
        # как появилась проверка. Они не становятся телефонами оттого,
        # что лежат давно.
        return jsonify(ok=True, merged=done, phones=db.clean_junk_phones())

    @app.post("/api/blacklist/clear")
    def api_blacklist_clear():
        db.blacklist_clear()
        return jsonify(ok=True)

    @app.post("/api/clear")
    def api_clear():
        c = db.conn()
        # Чёрный список и сохранённые наборы переживают очистку
        # намеренно: они про решения человека, а не про найденные данные.
        #
        # Заметок в этом списке не было, и это оборачивалось не потерей,
        # а подменой. Номера компаний в SQLite начинаются заново после
        # опустошения таблицы, и первая же компания следующего поиска
        # получала номер удалённой — вместе с её заметками. «Отказались,
        # больше не звонить» оказывалось в карточке компании, с которой
        # никто не говорил.
        #
        # Кэш обхода сайтов тоже чистим: он помнит, что сайт обходили на
        # этой неделе, и после очистки базы новый поиск пропускал бы те
        # же сайты, оставляя карточки без телефонов и почт.
        for t in ("contacts", "signals", "notes", "companies", "logs",
                  "tasks", "site_visits"):
            c.execute("DELETE FROM %s" % t)
        c.commit()
        return jsonify(ok=True)

    # ── Выгрузка ─────────────────────────────────────────
    @app.post("/api/save")
    def api_save():
        """Сохранить выгрузку в файл и открыть папку с ним.

        Раньше Excel и CSV были ссылками на скачивание, и это была
        ошибка: окно программы — не браузер, качать файлы оно не умеет.
        Windows получал от него пустой путь и показывал «не удаётся найти
        "\\"» вместо файла.

        Настольная программа и не должна ничего «скачивать»: она пишет
        файл на диск, в свою папку рядом с базой, и показывает, куда.
        """
        d = request.get_json(silent=True) or {}
        fmt = "csv" if (d.get("fmt") or "xlsx") == "csv" else "xlsx"
        cond, args = _company_where(d.get("q") or "", d.get("only") or "",
                                    d.get("ids") or "")
        rows = export.rows_for_export(db.conn(), cond, tuple(args))
        if not rows:
            return jsonify(ok=False, error="выгружать нечего — список пуст")

        folder = os.path.join(settings.data_dir(), "Выгрузки")
        os.makedirs(folder, exist_ok=True)
        name = "navodka-%s.%s" % (time.strftime("%Y%m%d-%H%M"), fmt)
        path = os.path.join(folder, name)

        blob = None
        if fmt == "xlsx":
            blob = export.to_xlsx(rows)
            if blob is None:                      # openpyxl не собрался
                fmt, name = "csv", name[:-4] + "csv"
                path = os.path.join(folder, name)
        if blob is None:
            blob = export.to_csv(rows)
        try:
            with open(path, "wb") as f:
                f.write(blob)
        except Exception as e:
            return jsonify(ok=False, error="не удалось записать файл: %s"
                                           % str(e)[:160])

        opened, _ = _open_outside(folder, is_path=True)
        return jsonify(ok=True, path=path, folder=folder, name=name,
                       count=len(rows), opened=opened)

    @app.get("/api/export.<fmt>")
    def api_export(fmt):
        cond, args = _company_where(request.args.get("q") or "",
                                    request.args.get("only") or "",
                                    request.args.get("ids") or "")
        rows = export.rows_for_export(db.conn(), cond, tuple(args))
        if fmt == "xlsx":
            blob = export.to_xlsx(rows)
            if blob is not None:
                return Response(blob, mimetype="application/vnd.openxmlformats-"
                                "officedocument.spreadsheetml.sheet",
                                headers={"Content-Disposition":
                                         "attachment; filename=navodka.xlsx"})
        return Response(export.to_csv(rows), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=navodka.csv"})

    return app
