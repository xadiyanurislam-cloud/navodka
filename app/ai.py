# -*- coding: utf-8 -*-
"""ИИ поверх собранных фактов.

Главное правило, на котором держится весь модуль: модель не добывает
данные, а объясняет уже добытые. Всё, что она видит, — это карточка из
базы. Спросить её «а сколько у этой компании выручка» нельзя: она
ответит, причём уверенно и неправильно, и отличить выдумку от факта в
таблице будет уже невозможно.

Поэтому результат работы модели лежит в отдельных полях с приставкой ai_,
никогда не перезаписывает ЕГРЮЛ и ФНС и помечен в интерфейсе как
сгенерированный.

Подключение — к любому API, совместимому с OpenAI: официальный OpenAI,
DeepSeek, OpenRouter, локальная модель, российский прокси. Адрес и ключ
задаёт пользователь: зашивать своего провайдера в программу, которую
ставят на чужую машину, — значит платить за чужие запросы.
"""
import json
import re
import time

import requests

from . import db, settings

# Версия протокола Anthropic. Заголовок обязательный: без него сервер
# отвечает отказом, не объясняя причины.
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# Ответ просим в JSON: свободный текст пришлось бы разбирать регулярками,
# а модель каждый раз оформляет его чуть иначе.
SYSTEM = (
    "Ты помощник менеджера по продажам в B2B. Тебе дают карточку компании, "
    "собранную из открытых источников. Отвечай СТРОГО валидным JSON без "
    "пояснений и без markdown.\n"
    "Важнейшее правило: используй ТОЛЬКО факты из карточки. Если данных для "
    "ответа не хватает — так и пиши, не додумывай. Никогда не придумывай "
    "выручку, сотрудников, клиентов, продукты или события, которых нет в "
    "карточке. Выдуманный факт в рабочей базе хуже, чем его отсутствие."
)


def config():
    return {
        "key": db.get_setting("ai_key", ""),
        "url": (db.get_setting("ai_url", "") or DEFAULT_URL).rstrip("/"),
        "model": db.get_setting("ai_model", "") or DEFAULT_MODEL,
        "kind": kind(db.get_setting("ai_kind", ""),
                     db.get_setting("ai_url", "")),
    }


def kind(saved, url):
    """Какой формат у этого адреса: OpenAI или Anthropic.

    Два формата несовместимы во всём: разные заголовки, разное место
    системной подсказки, разная форма ответа. Обычно выбор задан явно в
    настройках; если нет — угадываем по адресу, потому что человек,
    вставивший ссылку от посредника Claude, о форматах не думает и
    думать не должен.
    """
    saved = (saved or "").strip().lower()
    if saved in ("openai", "anthropic"):
        return saved
    low = (url or "").lower()
    if "/v1/messages" in low or "anthropic" in low or "claude" in low:
        return "anthropic"
    return "openai"


def enabled():
    return bool(config()["key"])


def ask(messages, cfg=None, timeout=90, max_tokens=700, session=None):
    """Один запрос к модели. Возвращает (текст, ошибка)."""
    cfg = cfg or config()
    if not cfg["key"]:
        return "", "ключ не задан"
    if cfg.get("kind") == "anthropic":
        return _ask_anthropic(messages, cfg, timeout, max_tokens, session)
    s = session or requests.Session()
    try:
        r = s.post(
            cfg["url"] + "/chat/completions",
            headers={"Authorization": "Bearer " + cfg["key"],
                     "Content-Type": "application/json",
                     "User-Agent": settings.USER_AGENT},
            json={"model": cfg["model"], "messages": messages,
                  "temperature": 0.2, "max_tokens": max_tokens},
            timeout=timeout)
        if r.status_code != 200:
            return "", "HTTP %s: %s" % (r.status_code, r.text[:200])
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip(), ""
    except Exception as e:
        return "", str(e)[:200]


def _ask_anthropic(messages, cfg, timeout, max_tokens, session=None):
    """Запрос в формате Anthropic Messages.

    Отличий от OpenAI три, и каждое ломает запрос целиком: ключ идёт не
    в Authorization, а в x-api-key; системная подсказка вынесена из
    списка сообщений в отдельное поле; ответ лежит не в choices, а в
    content — списком кусков, из которых нам нужны текстовые.

    Адрес принимаем в любом виде: и «https://router.cheap», и
    «https://router.cheap/v1», и сразу «…/v1/messages». Человек копирует
    то, что дал посредник, и подгонять ссылку под наш вкус не обязан.
    """
    s = session or requests.Session()
    base = cfg["url"].rstrip("/")
    if base.endswith("/v1/messages"):
        url = base
    elif base.endswith("/v1"):
        url = base + "/messages"
    else:
        url = base + "/v1/messages"

    system = " ".join(m["content"] for m in messages if m.get("role") == "system")
    rest = [{"role": ("assistant" if m.get("role") == "assistant" else "user"),
             "content": m.get("content") or ""}
            for m in messages if m.get("role") != "system"]
    body = {"model": cfg["model"], "max_tokens": max_tokens,
            "temperature": 0.2, "messages": rest}
    if system:
        body["system"] = system
    try:
        r = s.post(url,
                   headers={"x-api-key": cfg["key"],
                            "anthropic-version": ANTHROPIC_VERSION,
                            "Content-Type": "application/json",
                            "User-Agent": settings.USER_AGENT},
                   json=body, timeout=timeout)
        if r.status_code != 200:
            return "", "HTTP %s: %s" % (r.status_code, (r.text or "")[:200])
        data = r.json() or {}
        parts = [b.get("text") or "" for b in (data.get("content") or [])
                 if b.get("type") in (None, "text")]
        text = "".join(parts).strip()
        if not text:
            return "", "модель вернула пустой ответ"
        return text, ""
    except Exception as e:
        return "", str(e)[:200]


def models(cfg=None, timeout=25, session=None):
    """Список моделей, доступных этому ключу. Возвращает (список, ошибка).

    Нужен потому, что название модели у посредников своё: угадывать его
    за человека нельзя, а лезть в чужую документацию ради одной строки
    он не должен.
    """
    cfg = cfg or config()
    if not cfg["key"]:
        return [], "ключ не задан"
    s = session or requests.Session()
    base = cfg["url"].rstrip("/")
    for tail in ("/v1/messages", "/messages"):
        if base.endswith(tail):
            base = base[:-len(tail)]
    url = base + ("/models" if base.endswith("/v1") else "/v1/models")
    head = {"User-Agent": settings.USER_AGENT}
    if cfg.get("kind") == "anthropic":
        head.update({"x-api-key": cfg["key"],
                     "anthropic-version": ANTHROPIC_VERSION})
    else:
        head["Authorization"] = "Bearer " + cfg["key"]
    try:
        r = s.get(url, headers=head, timeout=timeout)
    except Exception as e:
        return [], str(e)[:160]
    if r.status_code != 200:
        return [], "HTTP %s: %s" % (r.status_code, (r.text or "")[:160])
    try:
        data = r.json() or {}
    except Exception:
        return [], "ответ не в JSON"
    items = data.get("data") or data.get("models") or []
    out = []
    for it in items:
        name = it.get("id") or it.get("name") if isinstance(it, dict) else str(it)
        if name:
            out.append(name)
    return out[:200], ""


def _json(text):
    """Достать JSON из ответа.

    Модели регулярно оборачивают его в ```json ... ``` или добавляют фразу
    перед объектом, даже когда просили не добавлять. Ломаться на этом —
    значит терять половину ответов на ровном месте.
    """
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ── Карточка для модели ──────────────────────────────────
def company_brief(company, signals, contacts, limit=2600):
    """Факты о компании в компактном виде.

    Не отдаём модели сырой HTML сайта: он съест весь лимит контекста, а
    полезного в нём столько же. Отдаём то, что уже разобрано.
    """
    c = company
    L = []
    add = lambda k, v: L.append("%s: %s" % (k, v)) if v not in (None, "", 0) else None

    add("Название", c["name"])
    add("Чем занимается (с сайта)", c["activity"])
    add("ОКВЭД", "%s %s" % (c["okved"] or "", c["okved_name"] or ""))
    add("Доп. виды деятельности", c["okveds_extra"])
    add("Регион", c["region"])
    add("Сайт", c["site"])
    add("Руководитель", "%s, %s" % (c["director"] or "", c["director_post"] or ""))
    add("В ЕГРЮЛ с", c["founded"])
    add("Статус", c["status"])
    add("Сотрудников (ФНС)", c["employees"])
    add("Филиалов", c["branches"])
    add("Учредителей", c["founders_count"])
    add("Уставный капитал", c["capital"])
    add("Динамика выручки", c["growth"])
    add("Движок сайта", c["cms"])
    add("Телефонные продажи", c["callcenter"])

    keys = [("revenue", "Выручка, ₽"), ("revenue_year", "Год выручки"),
            ("profit", "Прибыль, ₽"), ("size", "Размер"),
            ("hh_vacancies", "Вакансий в продажи"), ("hh_titles", "Названия вакансий"),
            ("hh_salary", "Зарплаты в вакансиях"), ("hh_about", "О себе на hh"),
            ("cc_why", "Признаки телефонных продаж"), ("sales_model", "Модель продаж"),
            ("self_year", "Работает с (по сайту)"), ("self_staff", "Сотрудников (по сайту)"),
            ("self_branches", "Точек (по сайту)"), ("gis_rubric", "Рубрика 2ГИС"),
            ("last_post", "Последняя публикация на сайте")]
    for key, label in keys:
        add(label, signals.get(key))

    kinds = sorted({x["kind"] for x in (contacts or [])})
    add("Есть контакты", ", ".join(kinds))
    return "\n".join(L)[:limit]


# ── Что умеет ────────────────────────────────────────────
def analyze(brief, icp="", offer="", cfg=None, session=None):
    """Разбор компании: краткая суть, соответствие и с чего начать разговор.

    Три задачи одним запросом, а не тремя. Не ради экономии: модель,
    которая уже прочитала карточку, отвечает согласованно, а три отдельных
    вызова дают три слегка разных представления об одной компании.
    """
    task = [
        "Вот карточка компании:", "", brief, "",
        "Верни JSON с полями:",
        '  "summary" — одна фраза (до 140 знаков): чем компания зарабатывает.',
        '  "segment" — одно из: "B2B", "B2C", "B2B и B2C", "не ясно".',
        '  "signals" — массив до 4 строк: что в карточке говорит о том, '
        'что компании нужны продажи по телефону. Только из карточки.',
    ]
    if icp:
        task += [
            '  "fit" — число 0..100: насколько компания похожа на описание '
            'нужного клиента ниже. Если данных мало — ставь ниже 50.',
            '  "fit_why" — до 160 знаков, почему такая оценка.',
            "", "Нужный клиент: " + icp[:600],
        ]
    if offer:
        task += [
            '  "hook" — до 160 знаков: конкретная зацепка из карточки, '
            'с которой начать разговор. Ссылайся на факт, а не на общие слова.',
            '  "opener" — до 320 знаков: первое сообщение руководителю. '
            'Без «инновационных решений» и восклицательных знаков. '
            'Обращение на «вы», по имени, если оно известно.',
            "", "Что мы продаём: " + offer[:600],
        ]
    text, err = ask([{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": "\n".join(task)}],
                    cfg=cfg, session=session)
    if err:
        return {}, err
    data = _json(text)
    if not isinstance(data, dict):
        return {}, "модель вернула не JSON: " + text[:120]
    return data, ""


def suggest_queries(icp, cfg=None, session=None):
    """Запросы под описание клиента: для hh.ru и рубрик 2ГИС.

    Человек описывает клиента словами, а искать надо запросами. Между
    «нам нужны частные клиники» и запросом к hh лежит шаг, на котором
    обычно и промахиваются.
    """
    prompt = (
        "Опиши, как искать таких клиентов. Верни JSON:\n"
        '  "hh" — массив до 6 поисковых запросов к hh.ru по вакансиям, '
        'которые публикуют ТАКИЕ компании (не вакансии наших продавцов). '
        'Короткие, как их пишут в поиске.\n'
        '  "gis" — массив до 8 рубрик справочника 2ГИС.\n'
        '  "okved" — массив до 6 кодов ОКВЭД с названиями.\n'
        '  "note" — до 200 знаков: по какому признаку отсеивать неподходящих.\n\n'
        "Нужный клиент: " + (icp or "")[:800])
    text, err = ask([{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
                    cfg=cfg, session=session, max_tokens=600)
    if err:
        return {}, err
    data = _json(text)
    if not isinstance(data, dict):
        return {}, "модель вернула не JSON"
    return data, ""


LETTER_SYSTEM = (
    "Ты пишешь первое письмо руководителю компании от лица продавца. "
    "Пиши по-русски, коротко и по-деловому, без восторгов и без «мы "
    "динамично развивающаяся компания». Опирайся ТОЛЬКО на факты из "
    "карточки: если чего-то в ней нет, не придумывай. Не ври, что "
    "изучил их сайт, если в карточке нет описания. "
    "Формат ответа — строго JSON: "
    '{"subject": "тема письма", "body": "текст письма"}. '
    "Тема — не больше восьми слов, без восклицательных знаков. "
    "Тело — четыре-шесть предложений: одно про то, почему пишем именно "
    "им (с опорой на факт из карточки), одно-два про то, что предлагаем, "
    "и вопрос в конце, на который легко ответить одним предложением. "
    "Подпись не ставь — её добавит человек."
)


def letter(company, signals, contacts, offer="", icp="", cfg=None, session=None):
    """Первое письмо этой компании. Возвращает (письмо, ошибка).

    Пишется по тем же фактам, что уже собраны, и ничего не добывает
    заново. Это сознательно: письмо, где угадано название продукта или
    придумана деталь про клиента, хуже отсутствия письма — на него
    отвечают «вы нас с кем-то путаете».
    """
    brief = company_brief(company, signals, contacts)
    what = (offer or "").strip() or "Опиши предложение в общих словах."
    who = (icp or "").strip()
    msg = [
        {"role": "system", "content": LETTER_SYSTEM},
        {"role": "user", "content":
            "Карточка компании:\n%s\n\nЧто мы продаём: %s\n%s"
            % (brief, what,
               ("Кого мы обычно ищем: %s" % who) if who else "")},
    ]
    text, err = ask(msg, cfg=cfg, max_tokens=700, session=session)
    if err:
        return {}, err
    d = _json(text)
    if not d or not d.get("body"):
        return {}, "модель ответила не тем форматом"
    return {"subject": (d.get("subject") or "").strip()[:160],
            "body": (d.get("body") or "").strip()[:4000]}, ""


def check(cfg=None):
    """Проверка связи — чтобы ключ не выяснялся посреди обхода тысячи компаний."""
    t0 = time.time()
    text, err = ask([{"role": "user", "content": 'Ответь JSON: {"ok":true}'}],
                    cfg=cfg, max_tokens=20, timeout=30)
    if err:
        return False, err
    return True, "ответ за %.1f с" % (time.time() - t0)
