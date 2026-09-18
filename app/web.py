# -*- coding: utf-8 -*-
"""Flask-приложение. Крутится на 127.0.0.1 внутри окна программы.

Это не сайт: снаружи порт не слушается, авторизации нет и не нужно —
программа работает на машине пользователя и видна только ему.
"""
import json
import os
import threading

from flask import Flask, Response, jsonify, render_template, request

from . import ai, db, diag, export, settings, update, worker
from .sources import gis2, hh


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
        )

    # ── Задачи ───────────────────────────────────────────
    @app.post("/api/search")
    def api_search():
        d = request.get_json(silent=True) or {}
        # Запросы приходят списком: в поле их можно написать по одному на
        # строку, а пресеты добавляются галочками.
        queries = [q.strip() for q in (d.get("queries") or []) if q.strip()]
        if not queries:
            queries = [(d.get("text") or "").strip() or hh.PRESETS["Отдел продаж"]]
        areas = [str(a) for a in (d.get("areas") or []) if str(a).strip()]
        task_id = db.create_task("hh_search", {
            "queries": queries[:12],
            "areas": areas[:8] or [d.get("area") or "113"],
            "period": int(d.get("period") or 30),
            "pages": max(1, min(20, int(d.get("pages") or 5))),
            "then_enrich": bool(d.get("then_enrich")),
        })
        return jsonify(ok=True, task_id=task_id, queries=len(queries))

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
    @app.get("/api/companies")
    def api_companies():
        c = db.conn()
        q = (request.args.get("q") or "").strip()
        only = request.args.get("only") or ""
        where, args = [], []
        if q:
            where.append("(name LIKE ? OR director LIKE ? OR inn LIKE ? OR site LIKE ?)")
            args += ["%%%s%%" % q] * 4
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
        sql = "SELECT * FROM companies"
        if where:
            sql += " WHERE " + " AND ".join(where)
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
                    "update_repo", "update_token", "update_url"):
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
    @app.get("/api/export.<fmt>")
    def api_export(fmt):
        rows = export.rows_for_export(db.conn())
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
