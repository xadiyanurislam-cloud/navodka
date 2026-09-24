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

from . import db, net, settings

# Версия протокола Anthropic. Заголовок обязательный: без него сервер
# отвечает отказом, не объясняя причины.
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
# Посреднику Claude модель gpt-4o-mini не известна, и запрос с ней
# отвечает отказом про неизвестную модель — а человек при этом уверен,
# что не задавал никакой модели вовсе. Своё имя у каждого посредника, но
# это встречается чаще прочих; точное берётся кнопкой «Показать
# доступные модели».
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"

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
        "model": model(db.get_setting("ai_model", ""),
                       kind(db.get_setting("ai_kind", ""),
                            db.get_setting("ai_url", ""))),
        "kind": kind(db.get_setting("ai_kind", ""),
                     db.get_setting("ai_url", "")),
    }


def model(saved, kind_):
    """Имя модели — заданное или разумное по формату.

    Имя из чужого формата отбрасываем молча. В базе оно остаётся от
    прежних настроек — человек однажды сохранил gpt-4o-mini, потом
    переключился на посредника Claude, а поле так и стоит. Отправить
    такое значит получить отказ про неизвестную модель и искать вину в
    ключе, которого этот отказ не касается.
    """
    saved = (saved or "").strip()
    if kind_ == "anthropic":
        if saved and not saved.lower().startswith(("gpt-", "o1-", "o3-",
                                                   "text-", "davinci")):
            return saved
        return DEFAULT_ANTHROPIC_MODEL
    if saved and not saved.lower().startswith("claude-"):
        return saved
    return DEFAULT_MODEL


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


# Начало объяснения обрыва. Вынесено в имя, потому что по нему же
# ошибку потом и узнают: дальше по пути она ходит словами.
RESET_NOTE = "связь разорвана по дороге"


def _is_reset(err):
    """Соединение оборвали, а не отказали.

    Обрыв и отказ лечатся по-разному, и путать их нельзя: отказ значит
    «сервер тебя услышал и сказал нет», обрыв — «до сервера не дошло».
    """
    t = str(err).lower()
    return any(m in t for m in (
        "10054", "connection aborted", "connection reset", "connectionreset",
        "remotedisconnected", "eof occurred", "connection broken",
        "recv failure", "ssl", "handshake",
        # Наш же текст: проверять приходится и его, потому что дальше по
        # пути ошибка ходит уже словами, а не исключением.
        RESET_NOTE))


def _no_socks(err):
    """requests без PySocks не умеет socks5 и говорит об этом по-английски."""
    return "socks" in str(err).lower() and "depend" in str(err).lower()


def _proxy_failed(err):
    t = str(err).lower()
    # curl о прокси говорит своими номерами: 5 — не разрешается имя
    # прокси, 7 — не соединиться с ним, 97 — отказ прокси.
    return "proxy" in t or "curl: (5)" in t or "curl: (97)" in t


def _explain(err):
    """Ошибку связи — словами, а не текстом исключения Python."""
    if _proxy_failed(err) and not _no_socks(err):
        return ("прокси %s не отвечает или не пускает. Проверьте адрес, "
                "порт, логин и пароль — до самого сервера дело не дошло."
                % (net.proxy_label() or "указанный"))
    if _no_socks(err):
        return ("прокси socks5 не поддерживается этой сборкой. Обновите "
                "программу; если уже последняя — впишите прокси как "
                "http://… вместо socks5://.")
    if not _is_reset(err):
        return str(err)[:200]
    tail = "" if net.HAVE_CURL else (
        " Ещё: не установлена библиотека curl_cffi — без неё программа не "
        "умеет менять отпечаток рукопожатия. Переустановите программу.")
    return (RESET_NOTE + ". Это не отказ сервера: он не успел "
            "ответить. Так ведёт себя фильтр между вами и провайдером. "
            "Проверьте адрес, попробуйте включить VPN или спросите у "
            "продавца запасной адрес." + tail)


def _transports(session=None):
    """Чем идти в сеть: сначала обычно, потом с отпечатком браузера.

    Посредник отвечал обрывом соединения, а не отказом, — так выглядит
    не поломка сервера, а фильтр по дороге: он смотрит на отпечаток
    TLS-рукопожатия и рвёт связь раньше, чем дело дойдёт до ответа. У
    requests отпечаток свой, ни на один браузер не похожий; curl_cffi
    делает рукопожатие как у Chrome, и тот же запрос проходит.
    """
    if session is not None:
        return [session]
    out = [requests.Session()]
    if net.HAVE_CURL:
        try:
            out.append(net.curl_requests.Session(impersonate=net.IMPERSONATE))
        except Exception:
            pass
    return [net.apply_proxy(s) for s in out]


def _send(method, url, headers, timeout, body=None, session=None):
    """Запрос с запасным путём. Возвращает (ответ, ошибка)."""
    errs = []
    for s in _transports(session):
        try:
            if method == "GET":
                return s.get(url, headers=headers, timeout=timeout), ""
            return s.post(url, headers=headers, json=body, timeout=timeout), ""
        except Exception as e:
            errs.append(e)
            # Отказ по существу повторять незачем: ответ будет тот же.
            # А вот нехватка поддержки socks — не отказ сервера, а наша
            # беда, и у следующего способа её может не быть.
            if not (_is_reset(e) or _no_socks(e) or _proxy_failed(e)):
                break
    return None, _explain(_worth_telling(errs))


def _worth_telling(errs):
    """Какую из ошибок показать.

    Способов связи несколько, и последний по счёту не значит самый
    внятный: curl сообщает о той же беде номером своей ошибки, а
    requests — словами. Ищем ту, из которой понятно, что делать.
    """
    if not errs:
        return None
    for e in errs:
        if _proxy_failed(e) and not _no_socks(e):
            return e
    for e in errs:
        if _is_reset(e):
            return e
    return errs[-1]


def ask(messages, cfg=None, timeout=90, max_tokens=700, session=None):
    """Один запрос к модели. Возвращает (текст, ошибка)."""
    cfg = cfg or config()
    if not cfg["key"]:
        return "", "ключ не задан"
    if cfg.get("kind") == "anthropic":
        return _ask_anthropic(messages, cfg, timeout, max_tokens, session)
    r, err = _send("POST", cfg["url"] + "/chat/completions",
                   {"Authorization": "Bearer " + cfg["key"],
                    "Content-Type": "application/json",
                    "User-Agent": settings.USER_AGENT},
                   timeout,
                   body={"model": cfg["model"], "messages": messages,
                         "temperature": 0.2, "max_tokens": max_tokens},
                   session=session)
    if r is None:
        return "", err
    try:
        if r.status_code != 200:
            return "", "HTTP %s: %s" % (r.status_code, r.text[:200])
        data = r.json()
        return (data["choices"][0]["message"]["content"] or "").strip(), ""
    except Exception as e:
        return "", str(e)[:200]


def endpoint(cfg):
    """Куда уйдёт запрос. Показываем это в проверке связи: «не работает»
    без адреса — гадание, а ошибка в адресе здесь самая частая.

    Адрес принимаем в любом виде: и «https://router.cheap», и
    «https://router.cheap/v1», и сразу «…/v1/messages». Человек копирует
    то, что дал посредник, и подгонять ссылку под наш вкус не обязан.
    """
    base = (cfg.get("url") or "").rstrip("/")
    if cfg.get("kind") != "anthropic":
        return base + "/chat/completions"
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


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
    url = endpoint(cfg)

    system = " ".join(m["content"] for m in messages if m.get("role") == "system")
    rest = [{"role": ("assistant" if m.get("role") == "assistant" else "user"),
             "content": m.get("content") or ""}
            for m in messages if m.get("role") != "system"]
    body = {"model": cfg["model"], "max_tokens": max_tokens,
            "temperature": 0.2, "messages": rest}
    if system:
        body["system"] = system
    r, err = _send("POST", url,
                   {"x-api-key": cfg["key"],
                    "anthropic-version": ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                    "User-Agent": settings.USER_AGENT},
                   timeout, body=body, session=session)
    if r is None:
        return "", err
    try:
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
    r, err = _send("GET", url, head, timeout, session=session)
    if r is None:
        return [], err
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


SETUP_SYSTEM = (
    "Ты настраиваешь программу поиска B2B-клиентов под конкретный бизнес. "
    "По описанию компании и её покупателя ты подбираешь, КАК таких "
    "покупателей найти в открытых источниках России: справочниках 2ГИС и "
    "Яндекса, ЕГРЮЛ и сайтах вакансий. Отвечай СТРОГО валидным JSON без "
    "пояснений и без markdown."
)


def _strs(value, limit, width=80):
    out = []
    for x in value if isinstance(value, list) else []:
        x = str(x or "").strip().strip("«»\"'").strip()
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x[:width])
        if len(out) >= limit:
            break
    return out


def project_setup(about, buyer, cfg=None, session=None):
    """Настройки поиска под бизнес. Возвращает (настройки, ошибка).

    Человек описывает свой бизнес и покупателя словами, а программе нужны
    запросы: виды деятельности для справочников, должности для вакансий,
    описание клиента для оценки и способ оценки. Между «мы продаём ПВХ»
    и запросом «сварщик ПВХ» — ровно тот шаг, на котором промахиваются.
    """
    about = (about or "").strip()
    buyer = (buyer or "").strip()
    if not about or not buyer:
        return {}, "опишите и свою компанию, и покупателя — без этого подбирать не из чего"
    prompt = "\n".join([
        "Наша компания: " + about[:800],
        "Наш конечный покупатель: " + buyer[:800],
        "",
        "Верни JSON с полями:",
        '  "find" — массив до 10 видов деятельности покупателя, как их пишут '
        'на вывесках и в справочниках («производство тентов», «пошив палаток», '
        '«надувные лодки ПВХ»). Короткие, 1–3 слова, без названий городов.',
        '  "vacancies" — массив до 6 должностей, которые НАНИМАЮТ такие '
        'покупатели и по которым видно, что им нужен наш товар или услуга '
        '(для продавца ПВХ — «сварщик ПВХ», «оператор ТВЧ»; для аналитики '
        'звонков — «оператор колл-центра», «менеджер по продажам»). '
        'Не вакансии нашей компании.',
        '  "icp" — до 400 знаков: портрет покупателя для оценки компаний — '
        'чем занимается, какого размера, по каким признакам его узнать.',
        '  "offer" — до 300 знаков: что мы продаём, одним абзацем, '
        'по-деловому, без рекламы.',
        '  "phone_sales" — true, если нашему покупателю важны телефонные '
        'продажи и звонки (мы продаём что-то для отдела продаж или '
        'телефонии), иначе false.',
        '  "okved" — массив до 6 кодов ОКВЭД покупателя с названиями.',
        '  "note" — до 200 знаков: по какому признаку отсеивать неподходящих.',
        '  "name" — короткое название проекта, 1–3 слова.',
    ])
    text, err = ask([{"role": "system", "content": SETUP_SYSTEM},
                     {"role": "user", "content": prompt}],
                    cfg=cfg, session=session, max_tokens=900)
    if err:
        return {}, err
    d = _json(text)
    if not isinstance(d, dict):
        return {}, "модель ответила не тем форматом — попробуйте ещё раз"
    out = {
        "find": _strs(d.get("find"), 10, 60),
        "vacancies": _strs(d.get("vacancies"), 6, 60),
        "icp": str(d.get("icp") or "").strip()[:600],
        "offer": str(d.get("offer") or "").strip()[:600] or about[:600],
        "score_mode": "phone_sales" if d.get("phone_sales") in (True, "true", 1)
                      else "generic",
        "okved": _strs(d.get("okved"), 6, 120),
        "note": str(d.get("note") or "").strip()[:300],
        "name": str(d.get("name") or "").strip()[:60],
    }
    if not out["find"] and not out["vacancies"]:
        return {}, "модель не предложила ни одного запроса — уточните описание"
    return out, ""


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


KP_SYSTEM = (
    "Ты составляешь коммерческое предложение для конкретной компании от "
    "лица продавца. Пиши по-русски, по-деловому, без рекламных штампов: "
    "никаких «динамично развивающихся», «инновационных решений» и "
    "«индивидуального подхода».\n"
    "ЖЕЛЕЗНОЕ ПРАВИЛО: ни одной цифры, которой нет во входных данных. "
    "Цены, сроки, гарантии, состав работ бери ТОЛЬКО из раздела «Наши "
    "условия». Если там пусто — так и напиши: «условия обсуждаются», а не "
    "придумывай. Про саму компанию — только факты из карточки. "
    "Выдуманная цифра в КП хуже, чем её отсутствие: за неё потом "
    "спрашивают.\n"
    "Формат ответа — строго JSON:\n"
    '{"title": "заголовок", "intro": "почему пишем именно им", '
    '"problem": "какая задача у них видна по карточке", '
    '"solution": "что предлагаем и как это решает именно их задачу", '
    '"terms": "состав, сроки и цены — из наших условий", '
    '"next": "следующий шаг: одно конкретное действие", '
    '"doubts": ["до трёх возражений, которые у них возникнут"]}\n'
    "Каждый раздел — связный текст, два-четыре предложения, без списков "
    "внутри. Обращение на «вы». Если известно имя руководителя — обращайся "
    "по имени и отчеству."
)


def kp(company, signals, contacts, offer="", terms="", icp="",
       cfg=None, session=None):
    """Коммерческое предложение под эту компанию. Возвращает (КП, ошибка).

    Отличается от письма не длиной, а назначением: письмо открывает
    разговор, КП его продолжает — его пересылают внутрь компании и
    читают без вас. Поэтому здесь есть состав работ, условия и разбор
    возражений, а в письме их быть не должно.

    Условия и цены приходят отдельным полем и только от человека.
    Модель, которой разрешили самой назвать срок и сумму, называет их
    уверенно и мимо: потом по ним спрашивают.
    """
    brief = company_brief(company, signals, contacts, limit=4000)
    what = (offer or "").strip()
    if not what:
        return {}, ("не заполнено «Что продаём» — без этого КП будет про "
                    "ничто. Экран «Поиск», карточка «ИИ-анализ».")
    parts = ["Карточка компании:", brief, "", "Что мы продаём: " + what[:900]]
    parts += ["", "Наши условия: " + (terms.strip()[:900] if terms.strip()
                                      else "не заданы — напиши, что "
                                           "условия обсуждаются")]
    if (icp or "").strip():
        parts += ["", "Кого мы обычно ищем: " + icp.strip()[:400]]
    text, err = ask([{"role": "system", "content": KP_SYSTEM},
                     {"role": "user", "content": "\n".join(parts)}],
                    cfg=cfg, max_tokens=1400, session=session)
    if err:
        return {}, err
    d = _json(text)
    if not isinstance(d, dict) or not d.get("solution"):
        return {}, "модель ответила не тем форматом"
    out = {}
    for key in ("title", "intro", "problem", "solution", "terms", "next"):
        out[key] = str(d.get(key) or "").strip()[:1500]
    doubts = d.get("doubts")
    out["doubts"] = [str(x).strip()[:300] for x in doubts[:3]] \
        if isinstance(doubts, list) else []
    return out, ""


def kp_text(kp_dict, company_name=""):
    """КП одним куском — чтобы скопировать и вставить в письмо."""
    L = []
    if kp_dict.get("title"):
        L += [kp_dict["title"], ""]
    for key, head in (("intro", ""), ("problem", "Задача"),
                      ("solution", "Что предлагаем"),
                      ("terms", "Условия"), ("next", "Следующий шаг")):
        val = (kp_dict.get(key) or "").strip()
        if not val:
            continue
        if head:
            L.append(head)
        L += [val, ""]
    if kp_dict.get("doubts"):
        L.append("О чём спросят")
        L += ["— " + x for x in kp_dict["doubts"]]
    return "\n".join(L).strip()


def diagnose(cfg=None, timeout=8):
    """Где именно рвётся связь: имя, соединение, рукопожатие или ответ.

    Обрыв соединения выглядит одинаково, что бы его ни вызвало, а
    причины разные и лечатся по-разному: имя не разрешается — вопрос к
    DNS; не открывается порт — адрес или сеть; рвётся рукопожатие —
    фильтр по дороге; отвечает ошибкой — уже разговор по существу.
    Пока это не разделено, любое «не работает» остаётся гаданием.
    """
    import socket
    import ssl
    try:
        from urllib.parse import urlparse
    except ImportError:                                  # pragma: no cover
        from urlparse import urlparse

    cfg = cfg or config()
    host = (urlparse(endpoint(cfg)).hostname or "").strip()
    if not host:
        return ["Адрес не разобран — проверьте поле «Адрес API»."]

    out = []
    if net.proxies():
        out.append("Задан прокси — проверка ниже идёт мимо него, напрямую")
    try:
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})
    except Exception as e:
        out.append("Имя %s не разрешается (%s). Вопрос к DNS или к "
                   "написанию адреса." % (host, str(e)[:80]))
        return out
    out.append("Имя %s разрешается: %s" % (host, ", ".join(addrs[:3])))

    sock = None
    try:
        sock = socket.create_connection((host, 443), timeout=timeout)
        out.append("Порт 443 открыт")
    except Exception as e:
        out.append("Соединиться с %s:443 не удалось (%s). Сюда не пускают "
                   "вовсе — это сеть, а не программа." % (host, str(e)[:80]))
        return out

    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host):
            out.append("Рукопожатие прошло — значит рвут уже сам запрос")
    except Exception as e:
        out.append("Рукопожатие оборвано (%s). Так ведёт себя фильтр, "
                   "который смотрит на имя сайта в открытой части "
                   "рукопожатия: до самого сервера запрос не доходит. "
                   "Помогает VPN или прокси в «Настройках»." % str(e)[:80])
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return out


def model_missing(text):
    """Сервер отказал именно из-за имени модели.

    Отличать это от прочих отказов стоит потому, что лечится оно одним
    движением: список моделей посредник отдаёт сам, и спрашивать его у
    человека незачем — программа спросит.
    """
    t = (text or "").lower()
    about_model = "модель" in t or "model" in t
    denied = any(m in t for m in (
        "недоступна", "не доступна", "not found", "not_found",
        "does not exist", "unknown", "unsupported", "invalid model",
        "нет такой", "не поддерживается"))
    return about_model and denied


def check(cfg=None):
    """Проверка связи — чтобы ключ не выяснялся посреди обхода тысячи компаний."""
    cfg = cfg or config()
    t0 = time.time()
    text, err = ask([{"role": "user", "content": 'Ответь JSON: {"ok":true}'}],
                    cfg=cfg, max_tokens=20, timeout=30)
    if err:
        # Адрес и модель в тексте ошибки — не украшение: ошибка в них
        # самая частая, а увидеть, куда именно ушёл запрос, иначе негде.
        note = "%s\nЗапрос уходил на %s, модель «%s»." % (
            err, endpoint(cfg), cfg.get("model") or "—")
        # Обрыв разбираем по шагам сразу: заставлять человека жать вторую
        # кнопку ради ответа на вопрос «а почему» — лишний ход.
        if _is_reset(err):
            note += "\n\nПо шагам:\n· " + "\n· ".join(diagnose(cfg))
        return False, note
    return True, "ответ за %.1f с" % (time.time() - t0)
