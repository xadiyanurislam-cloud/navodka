# -*- coding: utf-8 -*-
"""Flask-приложение. Крутится на 127.0.0.1 внутри окна программы.

Это не сайт: снаружи порт не слушается, авторизации нет и не нужно —
программа работает на машине пользователя и видна только ему.
"""
import json
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

from . import ai, db, diag, export, geo, settings, update, worker
from .sources import gis2, hh


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


def create_app():
    app = Flask(__name__,
                template_folder=settings.resource_path("app", "templates"),
                static_folder=settings.resource_path("app", "static"))
    app.config["JSON_AS_ASCII"] = False

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            app_name=settings.APP_NAME, version=settings.VERSION,
            areas=hh.AREAS, presets=hh.PRESETS,
            rubrics=gis2.RUBRICS, cities=gis2.CITIES,
            dadata_token=db.get_setting("dadata_token", ""),
            gis_key=db.get_setting("gis_key", ""),
            ai_key=db.get_setting("ai_key", ""),
            ai_url=db.get_setting("ai_url", "") or ai.DEFAULT_URL,
            ai_model=db.get_setting("ai_model", "") or ai.DEFAULT_MODEL,
            hh_ua=db.get_setting("hh_ua", ""),
            hh_token=db.get_setting("hh_token", ""),
            update_repo=db.get_setting("update_repo", "") or update.DEFAULT_REPO,
            update_token=db.get_setting("update_token", ""),
            ai_icp=db.get_setting("ai_icp", ""),
            ai_offer=db.get_setting("ai_offer", ""),
            # В скрипт страницы это попадает как есть, поэтому «<»
            # экранируем: запрос человек пишет сам, и «</script>» в нём
            # сломал бы страницу целиком.
            build=settings.BUILD,
            data_dir=settings.data_dir(),
            last_search=(db.get_setting("last_search", "") or "{}").replace("<", "\\u003c"),
            last_find=(db.get_setting("last_find", "") or "{}").replace("<", "\\u003c"),
            geo_cities=geo.cities(),
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
        params = {
            "query": query[:120],
            "cities": [str(c) for c in (d.get("cities") or [])][:14],
            "pages": max(1, min(10, int(d.get("pages") or 3))),
            "sources": {
                "osm": bool(d.get("osm", True)),
                "gis": bool(d.get("gis", True)),
                "yandex": bool(d.get("yandex", True)),
                "dadata": bool(d.get("dadata", True)),
                "hh": bool(d.get("hh", True)),
            },
            "skip_empty": bool(d.get("skip_empty", True)),
            "then_enrich": bool(d.get("then_enrich")),
            "then_zakupki": bool(d.get("then_zakupki")),
            "then_ai": bool(d.get("then_ai")),
        }
        db.set_setting("last_find", json.dumps(params, ensure_ascii=False))
        return jsonify(ok=True, task_id=db.create_task("find", params))

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
            "period": max(1, min(30, int(d.get("period") or 30))),
            "pages": max(1, min(20, int(d.get("pages") or 5))),
            "in_title": bool(d.get("in_title", True)),
            "skip_agencies": bool(d.get("skip_agencies", True)),
            "max_open": max(0, min(5000, int(d.get("max_open") or 0))),
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
            db.mark_search_run(int(d["search_id"]))
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
        sid = db.save_search(name, _search_params(d))
        return jsonify(ok=True, id=sid, rows=db.list_searches())

    @app.post("/api/searches/<int:sid>/delete")
    def api_searches_delete(sid):
        db.delete_search(sid)
        return jsonify(ok=True, rows=db.list_searches())

    @app.post("/api/gis")
    def api_gis():
        d = request.get_json(silent=True) or {}
        task_id = db.create_task("gis_search", {
            "query": (d.get("query") or "").strip(),
            "region": int(d.get("region") or 32),
            "pages": max(1, min(10, int(d.get("pages") or 2))),
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
            "limit": max(1, min(500, int(d.get("limit") or 50))),
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
        task_id = db.create_task("ai", {
            "limit": max(1, min(300, int(d.get("limit") or 30))),
            "icp": d.get("icp") or "", "offer": d.get("offer") or "",
            "redo": bool(d.get("redo")),
        })
        return jsonify(ok=True, task_id=task_id)

    @app.post("/api/ai/check")
    def api_ai_check():
        ok, note = ai.check()
        return jsonify(ok=ok, note=note, model=ai.config()["model"])

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
        c = db.conn()
        # Показываем идущую задачу, а если её нет — последнюю. Иначе,
        # поставив обогащение в очередь следом за поиском, человек видит
        # завершённый поиск и думает, что всё встало.
        row = c.execute("SELECT * FROM tasks WHERE status IN ('running','queued') "
                        "ORDER BY id LIMIT 1").fetchone()
        if row is None:
            row = c.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        queued = c.execute("SELECT COUNT(*) n FROM tasks "
                           "WHERE status='queued'").fetchone()["n"]
        if row is None:
            return jsonify(ok=True, task=None, logs=[], queued=0)
        logs = c.execute("""SELECT level, text, created_at FROM logs
                            WHERE task_id=? ORDER BY id DESC LIMIT 80""",
                         (row["id"],)).fetchall()
        return jsonify(ok=True, task=dict(row), queued=queued,
                       logs=[dict(x) for x in reversed(logs)])

    # ── Данные ───────────────────────────────────────────
    def _company_where(q="", only="", ids=""):
        """Условие выборки по тому, что человек видит на экране.

        Тот же фильтр нужен выгрузке: отдавать в Excel всю базу, когда на
        экране отобраны двадцать подходящих компаний, — значит заставить
        человека фильтровать второй раз, уже в Excel.
        """
        where, args = [], []
        if q:
            where.append("(name LIKE ? OR director LIKE ? OR inn LIKE ? OR site LIKE ?)")
            args += ["%%%s%%" % q] * 4
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
        elif only == "ai_fit":
            where.append("ai_fit >= 60")
        elif only == "zakupki":
            where.append("id IN (SELECT company_id FROM signals WHERE key='zakupki_person')")
        elif only == "contactable":
            # Компания без единого способа связи — не лид, а строка в
            # реестре. Держать её в общем списке можно, показывать первой
            # нельзя.
            where.append("(site <> '' OR id IN (SELECT company_id FROM contacts "
                         "WHERE kind IN ('phone','email','social')))")
        elif only == "empty":
            where.append("(site = '' AND id NOT IN (SELECT company_id FROM "
                         "contacts WHERE kind IN ('phone','email','social')))")
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
        sql = "SELECT * FROM companies"
        if cond:
            sql += " WHERE " + cond
        sql += " ORDER BY score DESC, id LIMIT 500"

        out = []
        for row in c.execute(sql, args).fetchall():
            cts = c.execute("""SELECT kind, value, owner, confidence, verified, source
                               FROM contacts WHERE company_id=?
                               ORDER BY confidence DESC""", (row["id"],)).fetchall()
            sig = {r["key"]: r["value"] for r in
                   c.execute("SELECT key, value FROM signals WHERE company_id=?", (row["id"],))}
            out.append(dict(row, contacts=[dict(x) for x in cts], signals=sig))
        total = c.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"]
        return jsonify(ok=True, rows=out, total=total, shown=len(out))

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
        # Ссылки на поиск по ФИО отдаются, но не сохраняются: программа по
        # ним не ходит. Автоматически собранная база личных страниц — это
        # профилирование частного лица, а по имени ещё и ненадёжно.
        from . import social
        return jsonify(ok=True, company=dict(row),
                       contacts=[dict(x) for x in cts], signals=sig,
                       search=[{"title": t, "url": u} for t, u in
                               social.search_links(row["director"], row["name"])])

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
        patch = {k: d[k] for k in ("stage", "note") if k in d}
        if patch:
            db.update_company_fields(cid, patch)
        return jsonify(ok=True)

    @app.post("/api/settings")
    def api_settings():
        d = request.get_json(silent=True) or {}
        for key in ("dadata_token", "gis_key", "ai_key", "ai_url",
                    "ai_model", "hh_ua", "hh_token",
                    "update_repo", "update_token", "update_url",
                    "yandex_key", "vk_token"):
            if key in d:
                db.set_setting(key, (d[key] or "").strip())
        return jsonify(ok=True)

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
        ids = [int(x) for x in (d.get("ids") or [])][:2000]
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

    @app.post("/api/blacklist/clear")
    def api_blacklist_clear():
        db.blacklist_clear()
        return jsonify(ok=True)

    @app.post("/api/clear")
    def api_clear():
        c = db.conn()
        # Чёрный список переживает очистку намеренно: он про решения
        # человека, а не про найденные данные.
        for t in ("contacts", "signals", "companies", "logs", "tasks"):
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
