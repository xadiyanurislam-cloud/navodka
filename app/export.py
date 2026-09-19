# -*- coding: utf-8 -*-
"""Выгрузка в CSV и XLSX.

CSV пишется с BOM и точкой с запятой: без первого Excel на русской
локали открывает кириллицу кракозябрами, без второго — сваливает всю
строку в одну ячейку. Обе беды выглядят как «программа сломалась».
"""
import csv
import io

COLUMNS = [
    ("name", "Компания"), ("inn", "ИНН"), ("director", "Руководитель"),
    ("director_post", "Должность"), ("region", "Регион"), ("site", "Сайт"),
    ("score", "Оценка"), ("lpr_status", "Контакт ГД"),
    ("email_director", "Почта ГД (найдена)"),
    ("email_guess", "Почта ГД (предположение)"),
    ("phone_director", "Телефон ГД"),
    ("email_other", "Другие почты"), ("phone", "Телефоны"),
    ("telegram", "Telegram"), ("social_director", "Соцсети руководителя"),
    ("social", "Соцсети компании"), ("tech", "Технологии"),
    ("callcenter", "Телефонные продажи"), ("cc_why", "На основании чего"),
    ("activity", "Чем занимается"), ("employees", "Сотрудников"),
    ("founded", "Год регистрации"), ("revenue", "Выручка, млн ₽"),
    ("growth", "Динамика"), ("profit", "Прибыль, млн ₽"),
    ("revenue_prev", "Выручка год назад"), ("status", "Статус в ЕГРЮЛ"),
    ("capital", "Уставный капитал"), ("branches", "Филиалов"),
    ("founders_count", "Учредителей"), ("founders", "Учредители"),
    ("self_year", "Работает с (по сайту)"), ("sales_model", "Модель продаж"),
    ("salary", "Зарплаты в вакансиях"), ("last_post", "Последняя публикация"),
    ("okveds_extra", "Доп. ОКВЭД"), ("cms", "Движок сайта"),
    ("ai_fit", "ИИ: соответствие"), ("ai_why", "ИИ: почему"),
    ("ai_summary", "ИИ: суть бизнеса"), ("ai_segment", "ИИ: сегмент"),
    ("ai_hook", "ИИ: зацепка"), ("ai_opener", "ИИ: первое сообщение"),
    ("vacancies", "Вакансий в продажи"), ("okved_name", "Отрасль"),
    ("address", "Адрес"), ("stage", "Стадия"), ("note", "Заметка"),
]


def _prev_year(series):
    """Выручка за предыдущий год — чтобы динамика была видна и в Excel,
    где столбиков из карточки нет."""
    rows = [p for p in (series or "").split(";") if ":" in p]
    if len(rows) < 2:
        return ""
    year, rev = rows[1].split(":", 1)
    try:
        return "%s: %.1f" % (year, int(rev) / 1e6)
    except Exception:
        return ""


def rows_for_export(conn, where="", args=()):
    sql = "SELECT * FROM companies"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY score DESC, id"
    out = []
    for c in conn.execute(sql, args).fetchall():
        cts = conn.execute("SELECT * FROM contacts WHERE company_id=? ORDER BY confidence DESC",
                           (c["id"],)).fetchall()
        sig = {r["key"]: r["value"] for r in
               conn.execute("SELECT key, value FROM signals WHERE company_id=?", (c["id"],))}
        # Найденный адрес и выведенный по схеме разводим по колонкам:
        # в одной ячейке продавец не разберёт, какому можно доверять.
        def _is_guess(x):
            return "схеме" in (x["source"] or "")
        dir_mail = [x["value"] for x in cts
                    if x["kind"] == "email" and x["owner"] == "director" and not _is_guess(x)]
        dir_guess = [x["value"] for x in cts
                     if x["kind"] == "email" and x["owner"] == "director" and _is_guess(x)]
        dir_phone = [x["value"] for x in cts
                     if x["kind"] == "phone" and x["owner"] == "director"]
        oth_mail = [x["value"] for x in cts if x["kind"] == "email" and x["owner"] != "director"]
        out.append({
            "name": c["name"], "inn": c["inn"] or "", "director": c["director"] or "",
            "director_post": c["director_post"] or "", "region": c["region"] or "",
            "site": c["site"] or "", "score": c["score"],
            "lpr_status": sig.get("lpr_contact", ""),
            "email_director": "; ".join(dir_mail[:3]),
            "email_guess": "; ".join(dir_guess[:3]),
            "phone_director": "; ".join(dir_phone[:2]),
            "email_other": "; ".join(oth_mail[:5]),
            "phone": "; ".join(x["value"] for x in cts if x["kind"] == "phone")[:200],
            "telegram": "; ".join(x["value"] for x in cts if x["kind"] == "telegram")[:120],
            "social_director": "; ".join(x["value"] for x in cts
                                         if x["kind"] == "social" and x["owner"] == "director")[:300],
            "social": "; ".join(x["value"] for x in cts
                                if x["kind"] == "social" and x["owner"] != "director")[:300],
            "tech": "; ".join(v for k, v in sig.items() if k.startswith("tech_") and v),
            "callcenter": c["callcenter"] or "",
            "cc_why": sig.get("cc_why", ""),
            "activity": c["activity"] or "",
            "employees": c["employees"] or "",
            "founded": c["founded"] or "",
            "revenue": round(int(sig["revenue"]) / 1e6, 1) if sig.get("revenue") else "",
            "growth": c["growth"] or "",
            "profit": round(int(sig["profit"]) / 1e6, 1) if sig.get("profit") else "",
            "revenue_prev": _prev_year(sig.get("revenue_series")),
            "status": c["status"] or "",
            "capital": c["capital"] or "",
            "branches": c["branches"] or "",
            "founders_count": c["founders_count"] or "",
            "founders": c["founders"] or "",
            "self_year": sig.get("self_year", ""),
            "sales_model": sig.get("sales_model", ""),
            "salary": sig.get("hh_salary", ""),
            "last_post": sig.get("last_post", ""),
            "okveds_extra": c["okveds_extra"] or "",
            "ai_fit": c["ai_fit"] if c["ai_fit"] is not None else "",
            "ai_why": c["ai_why"] or "",
            "ai_summary": c["ai_summary"] or "",
            "ai_segment": c["ai_segment"] or "",
            "ai_hook": c["ai_hook"] or "",
            "ai_opener": c["ai_opener"] or "",
            "cms": c["cms"] or "",
            "vacancies": sig.get("hh_vacancies", ""),
            "okved_name": c["okved_name"] or "", "address": c["address"] or "",
            "stage": c["stage"] or "", "note": c["note"] or "",
        })
    return out


# Знаки, с которых Excel начинает считать ячейку формулой.
_FORMULA_HEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    """Значение для таблицы, которое останется значением.

    Excel читает ячейку, начинающуюся со знака равенства, как формулу и
    выполняет её. Названия и заметки приходят с чужих сайтов, то есть их
    пишет кто угодно: компания, назвавшаяся =cmd|'/c calc'!A1, при
    открытии выгрузки предложит выполнить команду. Апостроф в начале
    заставляет Excel считать содержимое текстом; в самой ячейке он не
    виден.

    Побочно чинится потеря плюса у телефонов: +74951234567 Excel читал
    как формулу сложения и показывал число без кода страны.
    """
    if isinstance(value, str) and value[:1] in _FORMULA_HEAD:
        return "'" + value
    return value


def to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow([title for _, title in COLUMNS])
    for r in rows:
        w.writerow([_cell(r.get(key, "")) for key, _ in COLUMNS])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(rows):
    """XLSX через openpyxl. Библиотеки нет — вернём None, вызывающий отдаст CSV."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except Exception:
        return None
    wb = Workbook()
    ws = wb.active
    ws.title = "Лиды"
    ws.append([title for _, title in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append([_cell(r.get(key, "")) for key, _ in COLUMNS])
    # Ширины подбираются под содержимое: колонок много, и таблица, которую
    # приходится растягивать руками, до продавца доезжает закрытой.
    widths = [34, 13, 26, 20, 18, 26, 8, 13, 30, 30, 18, 34, 26, 18, 34, 34,
              24, 18, 34, 44, 12, 14, 14, 16, 14, 18, 16, 16, 12, 12, 34,
              16, 26, 22, 16, 34, 16, 12, 44, 44, 14, 44, 60,
              10, 26, 40, 12, 30]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
