// Голый JS намеренно: программа собирается в exe без сборщика фронтенда,
// а вся логика всё равно живёт на стороне Python.
const $ = (id) => document.getElementById(id);
// Название строки «весь список» приходит с сервера вместе с самим
// списком городов: две копии одной строки однажды разошлись бы. Здесь,
// а не рядом с городами, потому что восстановление прошлого поиска
// зовёт citiesNote() задолго до того места, и const из середины
// файла ещё не существует: страница ломалась бы у всех, кто уже искал.
const WHOLE_RU = window.WHOLE_RU || "Россия целиком";
// Здесь же и по той же причине: восстановление прошлого поиска
// читает словарь близких слов задолго до самого списка.
const TRADE_ALSO = window.TRADE_ALSO || {};
const MAX_WORDS = window.MAX_WORDS || 5;
const esc = (s) => String(s == null ? "" : s)
  .replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;",
                                '"': "&quot;", "'": "&#39;"}[c]));

// Адрес для href.
//
// Экранирование здесь не спасает: в javascript:alert(1) нет ни одного
// опасного для разметки знака, и ссылка проходит escape целиком, а по
// нажатию выполняется. Адреса приходят с чужих сайтов и из карточек
// справочников, то есть их пишет кто угодно, а окно программы имеет
// доступ к её же локальному API и к сохранённым ключам.
//
// Поэтому список разрешённых схем, а не список запрещённых: запрещать
// по одной значит однажды забыть про data: или vbscript:.
// Ссылка — только если по ней действительно можно пойти.
//
// safeUrl отбрасывает опасные схемы и возвращает пустую строку, но
// <a href=""> — это ссылка на саму страницу: выглядит как рабочий адрес
// сайта, а нажатие перезагружает программу. В поле «сайт» у компании
// из справочника лежало «javascript:alert(1)», и в списке это
// показывалось синим, как настоящий адрес.
//
// Поэтому отброшенный адрес не исчезает — он остаётся простым текстом:
// видеть мусор, пришедший из источника, полезно, а нажимать на него не
// надо.
function link(url, text, cls) {
  const href = safeUrl(url);
  const body = esc(text == null ? url : text);
  if (!href) return `<span class="dead" title="Адрес не похож на ссылку — ${
    esc(String(url || "").slice(0, 80))}">${body}</span>`;
  return `<a ${cls ? `class="${cls}" ` : ""}href="${href}" target="_blank">${body}</a>`;
}

function safeUrl(u) {
  const s = String(u == null ? "" : u).trim();
  if (/^(https?:|mailto:|tel:)/i.test(s)) return esc(s);
  // Адрес без схемы — обычное дело для сайта из справочника.
  if (/^[\w-]+(\.[\w-]+)+(\/|$)/.test(s)) return esc("https://" + s);
  return "";
}

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  });
  return r.json();
}
const get = (url) => fetch(url).then((r) => r.json()).catch(() => null);

// ── Всплывающее сообщение ────────────────────────────────
// Копирование и запуск задачи без отклика выглядят как «ничего не
// произошло», и человек жмёт второй раз.
let toastTimer;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("is-on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("is-on"), 1800);
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // Clipboard API требует защищённого контекста. Внутри окна программы
    // он есть не всегда, поэтому откат на старый способ обязателен.
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (_) {}
    ta.remove();
  }
  toast("Скопировано: " + text);
}

// Стадия работы с компанией. В базе хранится значение, на экране — слово
// по-русски: «new» в таблице среди русских строк читается как сбой, а
// переименовать значение нельзя — оно уже лежит в чужих базах.
const STAGES = [["new", "новая"], ["в работе", "в работе"],
                ["написали", "написали"], ["ответили", "ответили"],
                ["созвон", "созвон"], ["отказ", "отказ"]];

// ── Экраны ───────────────────────────────────────────────
const VIEWS = {
  today: ["Сегодня", "Кому звонить и что нового"],
  base: ["База", "Найденное и обогащённое"],
  board: ["Воронка", "Компании по стадиям работы"],
  mail: ["Рассылки", "Письма компаниям и ответы на них"],
  sources: ["Поиск", "Откуда брать компании"],
  settings: ["Настройки", "Ключи, обновления и папка с данными"],
};
document.querySelectorAll("[data-view]").forEach((b) => {
  b.onclick = () => showView(b.dataset.view);
});
document.addEventListener("click", (e) => {
  const go = e.target.closest && e.target.closest("[data-go]");
  if (go) showView(go.dataset.go);
});

// Программа открывается на «Сегодня», а не на поиске. Утром человек
// приходит не искать новые компании, а звонить тем, с кем договорился
// вчера; поиск нужен раз в неделю, звонки — каждый день.
let view = "today";
function showView(name) {
  view = name;
  document.querySelectorAll(".nav-btn[data-view]").forEach(
    (b) => b.classList.toggle("is-active", b.dataset.view === name));
  for (const key of Object.keys(VIEWS)) {
    const el = $("view-" + key);
    if (el) el.hidden = key !== name;
  }
  $("view-title").textContent = VIEWS[name][0];
  $("view-sub").textContent = VIEWS[name][1];
  // Выгрузка относится к базе: на других экранах кнопки только мешают.
  $("bar-export").hidden = name !== "base";
  $("stats").hidden = name === "today";
  if (name === "base") loadCompanies(true);
  if (name === "today") loadToday();
  if (name === "board") loadBoard();
  if (name === "mail") loadMail();
}

// ── Сегодня ──────────────────────────────────────────────
// Первый экран отвечает на единственный вопрос, с которым сюда
// приходят утром: кому звонить. Не «сколько всего компаний», а «кому
// звонить сегодня» — цифры без этого списка не помогают начать работу.
const STAGE_RU = {"new": "новые", "в работе": "в работе",
                  "написали": "написали", "ответили": "ответили",
                  "созвон": "созвон", "отказ": "отказ"};

function scoreBadge(n, size) {
  const v = Number(n) || 0;
  const cls = v >= 60 ? "hot" : v >= 35 ? "warm" : "cold";
  // В карточке значок крупный и с подписью: это первое, на что смотрят,
  // открыв компанию, и число без слова «балл» рядом с названием читается
  // как что угодно — от числа сотрудников до года основания.
  if (size === "big") {
    return `<div class="badge-big ${cls}"><b>${v}</b><span>балл</span></div>`;
  }
  return `<span class="badge ${cls}">${v}</span>`;
}

async function loadToday() {
  const d = await get("/api/today");
  if (!d || !d.ok) return;

  const st = d.stages || {};
  const total = Object.values(st).reduce((a, b) => a + b, 0);

  // Пустая база — это не «ноль компаний», а «ещё не начинали».
  //
  // Раньше первый запуск встречал человека четырьмя нулями в ряд и
  // двумя пустыми панелями на две трети экрана. Это всё впечатление о
  // программе, и оно было «здесь ничего нет» вместо «вот с чего
  // начать». Плитки со счётчиками появляются, когда есть что считать.
  const blank = $("first-run");
  blank.hidden = total > 0;
  $("tiles").hidden = !total;
  $("today-cols").hidden = !total;
  if (!total) {
    blank.innerHTML = `
      <h2>С чего начать</h2>
      <ol class="steps-lite">
        <li><b>Выберите тему, вид деятельности и город.</b>
          Сорок восемь тем и больше пятисот видов — от стоматологии до
          металлообработки.</li>
        <li><b>Программа обойдёт источники сама.</b> Карта, справочники,
          ЕГРЮЛ и работодатели hh — и сведёт найденное в один список без
          дублей.</li>
        <li><b>Звоните по списку.</b> Наверху — те, у кого нашёлся прямой
          контакт руководителя.</li>
      </ol>
      <p class="first-note"><b>OpenStreetMap и hh.ru работают без ключей</b>
        — начинать можно прямо сейчас. Остальные источники подключаются
        в «Настройках» и делают выдачу полнее.</p>
      <button class="btn primary" data-go="sources">Перейти к поиску</button>`;
    return;
  }

  const tiles = [
    ["Всего компаний", total, "base", ""],
    ["На сегодня", d.due.length, "today", d.due.length ? "hot" : ""],
    ["В работе", st["в работе"] || 0, "board", ""],
    ["Новые", st["new"] || 0, "board", ""],
  ];
  $("tiles").innerHTML = tiles.map(([name, val, go, tone]) => `
    <button class="tile ${tone}" data-go="${go}">
      <b>${val}</b><span>${name}</span>
    </button>`).join("");

  const nav = $("n-today");
  nav.hidden = !d.due.length;
  nav.textContent = d.due.length;

  $("due-note").textContent = d.due.length
    ? "просроченное тоже здесь" : "";
  $("due-list").innerHTML = d.due.length ? d.due.map((r) => `
    <div class="due" data-open="${r.id}">
      ${scoreBadge(r.score)}
      <span class="due-main">
        <b>${esc(r.name)}</b>
        <i>${esc(r.next_step || "без пояснения")}</i>
      </span>
      <span class="due-when ${r.next_date < today() ? "late" : ""}">
        ${r.next_date < today() ? "просрочено · " : ""}${ruDate(r.next_date)}</span>
      <button class="btn sm" data-done="${r.id}">Сделано</button>
    </div>`).join("")
    : suggestBlock(d.suggest || []);

  $("due-list").querySelectorAll("[data-take]").forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      // «В работу» — это и стадия, и напоминание на сегодня: компания
      // тут же переезжает из предложенных в назначенные, и видно, что
      // нажатие сработало.
      await post("/api/company/" + b.dataset.take,
                 {stage: "в работе", next_step: "связаться", next_date: today()});
      loadToday(); loadCompanies(); loadStats();
      toast("Взято в работу — теперь в списке на сегодня");
    };
  });

  $("due-list").querySelectorAll("[data-done]").forEach((b) => {
    b.onclick = async (e) => {
      e.stopPropagation();
      await post("/api/company/" + b.dataset.done, {next_date: "", next_step: ""});
      loadToday();
      toast("Снято с сегодня");
    };
  });
  $("due-list").querySelectorAll("[data-open]").forEach((el) => {
    el.onclick = () => { openFromOtherView(Number(el.dataset.open)); };
  });

  $("fresh-list").innerHTML = d.fresh.length ? d.fresh.map((r) => `
    <div class="due" data-open="${r.id}">
      ${scoreBadge(r.score)}
      <span class="due-main">
        <b>${esc(r.name)}</b>
        <i>${esc(r.activity || r.region || "")}</i>
      </span>
    </div>`).join("")
    : `<div class="blank">
         <p><b>База пуста.</b></p>
         <p>Начните с раздела «Поиск»: OpenStreetMap работает без ключей.</p>
         <p><button class="btn primary sm" data-go="sources">К поиску</button></p>
       </div>`;
  $("fresh-list").querySelectorAll("[data-open]").forEach((el) => {
    el.onclick = () => { openFromOtherView(Number(el.dataset.open)); };
  });
}

// Сегодня — по часам человека, а не по Гринвичу.
//
// toISOString() отдаёт дату в UTC, и для Москвы это вчера с полуночи до
// трёх ночи, а для Новосибирска — до семи утра. Сервер при этом считает
// срок по местному времени. Расхождение видно там, где и обидно: звонок,
// назначенный на сегодня, помечался просроченным, а вчерашний
// несделанный — нет.
const today = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 10);
};

// Открыть компанию из другого экрана: переходим в базу, находим строку и
// раскрываем её. Иначе «Сегодня» — список, из которого некуда нажать.
async function openFromOtherView(id) {
  showView("base");
  $("q").value = "";
  await loadCompanies(true);
  const tr = document.querySelector(`tr.row[data-id="${id}"]`);
  if (tr) {
    tr.scrollIntoView({block: "center", behavior: "smooth"});
    toggleCard(tr, id);
  } else {
    toast("Компания ниже по списку — найдите её поиском");
  }
}

// ── Воронка ──────────────────────────────────────────────
// Стадия есть у каждой компании, но в таблице она спрятана в выпадающем
// списке последней колонки. На доске видно всю работу разом: сколько
// новых, сколько в работе, где затык.
async function loadBoard() {
  const d = await get("/api/board");
  if (!d || !d.ok) return;
  $("board").innerHTML = d.stages.map((st) => {
    const col = d.board[st] || {cards: [], total: 0};
    return `<section class="col" data-stage="${esc(st)}">
      <header class="col-head">
        <b>${esc(STAGE_RU[st] || st)}</b>
        <span>${col.total}</span>
      </header>
      <div class="col-body">
        ${col.cards.map(boardCard).join("") ||
          `<p class="col-blank">пусто</p>`}
        ${col.total > col.cards.length
          ? `<p class="col-blank">и ещё ${col.total - col.cards.length}</p>` : ""}
      </div>
    </section>`;
  }).join("");
  bindBoard();
}

// Имя компании для узкой колонки воронки.
//
// «ООО «Компания 12»» в колонке шириной в двести пикселей обрезалось до
// «ООО «Компания 1…», и различить соседние карточки было нельзя. При
// этом первые шесть знаков у всех одинаковы и не значат ничего: какая
// это форма собственности, в воронке не решает.
const FORM_RE = /^(ООО|АО|ПАО|ЗАО|ОАО|НАО|ИП|АНО|НКО|ФГУП|МУП|ГУП|ТСЖ|СНТ|КФХ)\s+/i;

function shortName(name) {
  const s = String(name || "").trim();
  const cut = s.replace(FORM_RE, "").replace(/^[«"']|[»"']$/g, "").trim();
  return cut || s;
}

function boardCard(r) {
  return `<article class="bcard" draggable="true" data-id="${r.id}">
    <div class="bcard-top">
      ${scoreBadge(r.score)}
      <b title="${esc(r.name)}">${esc(shortName(r.name))}</b>
    </div>
    ${r.director ? `<p class="bcard-sub">${esc(r.director)}</p>` : ""}
    ${r.contact ? `<p class="bcard-contact">${esc(r.contact)}</p>` : ""}
    ${r.next_step ? `<p class="bcard-next">${esc(r.next_step)}${
      r.next_date ? ` · ${ruDate(r.next_date)}` : ""}</p>` : ""}
  </article>`;
}

function bindBoard() {
  let dragged = null;
  $("board").querySelectorAll(".bcard").forEach((card) => {
    card.ondragstart = (e) => {
      dragged = card;
      card.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", card.dataset.id);
    };
    card.ondragend = () => {
      card.classList.remove("is-dragging");
      dragged = null;
    };
    card.ondblclick = () => openFromOtherView(Number(card.dataset.id));
  });
  $("board").querySelectorAll(".col").forEach((col) => {
    col.ondragover = (e) => { e.preventDefault(); col.classList.add("is-over"); };
    col.ondragleave = () => col.classList.remove("is-over");
    col.ondrop = async (e) => {
      e.preventDefault();
      col.classList.remove("is-over");
      const id = e.dataTransfer.getData("text/plain");
      if (!id) return;
      const stage = col.dataset.stage;
      // Переносим сразу, не дожидаясь ответа: карточка должна лечь под
      // курсор в тот же миг, иначе перетаскивание ощущается сломанным.
      const card = $("board").querySelector(`.bcard[data-id="${id}"]`);
      if (card) col.querySelector(".col-body").prepend(card);
      await post("/api/company/" + id, {stage});
      loadBoard(); loadStats();
      toast(`Стадия: ${STAGE_RU[stage] || stage}`);
    };
  });
}

// ── Запуск задач ─────────────────────────────────────────
// Пресеты добавляются строкой, а не затирают поле: смысл в том, чтобы
// обойти несколько запросов за один прогон.
document.querySelectorAll("#f-presets .pick").forEach((b) => {
  b.onclick = () => {
    const box = $("f-text");
    const lines = box.value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (lines.includes(b.dataset.q)) {
      box.value = lines.filter((x) => x !== b.dataset.q).join("\n");
    } else {
      lines.push(b.dataset.q);
      box.value = lines.join("\n");
    }
    markPresets();
  };
});
function markPresets() {
  const lines = $("f-text").value.split("\n").map((x) => x.trim());
  document.querySelectorAll("#f-presets .pick").forEach(
    (b) => b.classList.toggle("is-on", lines.includes(b.dataset.q)));
}
$("f-text").oninput = markPresets;
markPresets();

async function run(url, body, what) {
  await post(url, body);
  toast(what + " — запущено");
  showView("base");
  poll();
}

// ── Поиск по виду деятельности ───────────────────────────
// Главный вход: «стоматология», «грузоперевозки», «АТИ». Три источника
// отвечают на этот вопрос по-разному, и спрашиваются они все сразу.
function findForm() {
  return {
    query: $("q-text").value.trim(),
    cities: pickedCities(),
    pages: $("q-pages").value,
    limit: $("q-limit").value,
    osm: $("q-osm").checked,
    gis: $("q-gis").checked,
    yandex: $("q-yandex").checked,
    dadata: $("q-dadata").checked,
    hh: $("q-hh").checked,
    fns: $("q-fns").checked,
    trudvsem: $("q-trud").checked,
    superjob: $("q-sj").checked,
    synonyms: $("q-also").checked,
    skip_empty: $("q-skip-empty").checked,
    then_enrich: $("q-then").checked,
    then_zakupki: $("q-then-zak").checked,
    then_ai: $("q-then-ai").checked,
  };
}

function fillFindForm(p) {
  if (!p || !p.query) return;
  $("q-text").value = p.query;
  const want = (p.cities || []).map(String);
  if (want.length) {
    $("q-cities").querySelectorAll(".city").forEach(
      (el) => el.classList.toggle("is-on", want.includes(el.dataset.city)));
    citiesNote();
  }
  if (p.pages) $("q-pages").value = String(p.pages);
  if (p.limit) $("q-limit").value = String(p.limit);
  const src = p.sources || {};
  $("q-osm").checked = src.osm !== false;
  $("q-gis").checked = src.gis !== false;
  $("q-yandex").checked = src.yandex !== false;
  $("q-dadata").checked = src.dadata !== false;
  $("q-hh").checked = src.hh !== false;
  $("q-fns").checked = src.fns !== false;
  $("q-trud").checked = src.trudvsem !== false;
  $("q-sj").checked = src.superjob !== false;
  $("q-also").checked = p.synonyms !== false;
  alsoNote();
  $("q-skip-empty").checked = p.skip_empty !== false;
  $("q-then").checked = !!p.then_enrich;
  $("q-then-zak").checked = !!p.then_zakupki;
  $("q-then-ai").checked = !!p.then_ai;
}

// Есть ли ключ прямо сейчас. Поле настроек — источник правды: оно
// заполнено при загрузке страницы и меняется сразу после сохранения,
// так что перезагружать ничего не нужно.
function hasKey(what) {
  const el = $({gis: "s-gis", yandex: "s-yandex", dadata: "s-dadata",
               sj: "s-sj"}[what]);
  return !!(el && el.value.trim());
}

// Сколько источников реально готово — видно до нажатия, а не после.
//
// Отмеченный источник без ключа поиск молча пропускает, и человек
// узнаёт об этом только из журнала. Поэтому в строке готовности такие
// источники не перечисляются как рабочие, а называются отдельно.
function findReady() {
  const f = findForm();
  const all = [[f.osm, "OSM", true], [f.gis, "2ГИС", hasKey("gis")],
               [f.yandex, "Яндекс", hasKey("yandex")],
               [f.dadata, "ЕГРЮЛ", hasKey("dadata")], [f.hh, "hh.ru", true],
               [f.trudvsem, "Работа России", true],
               [f.superjob, "SuperJob", hasKey("sj")],
               // ФНС нужен только без токена DaData — с токеном он молчит.
               [f.fns && !(f.dadata && hasKey("dadata")), "ЕГРЮЛ ФНС", true]];
  const on = all.filter((s) => s[0] && s[2]).map((s) => s[1]);
  const off = all.filter((s) => s[0] && !s[2]).map((s) => s[1]);
  const box = $("find-ready");
  const tail = off.length ? ` · без ключа, пропустим: ${off.join(", ")}` : "";
  box.textContent = on.length
    ? `ищем в: ${on.join(", ")}${tail}`
    : (off.length
       ? `у выбранных источников нет ключей: ${off.join(", ")}`
       : "ни один источник не выбран");
  box.classList.toggle("bad", !on.length);
  box.classList.toggle("warn", !!on.length && !!off.length);
  return on.length;
}
["q-osm", "q-gis", "q-yandex", "q-dadata", "q-hh", "q-fns", "q-trud", "q-sj"]
  .forEach((id) => { $(id).onchange = findReady; });
findReady();

$("btn-find").onclick = async () => {
  const f = findForm();
  if (!f.query) { $("q-text").focus(); toast("Впишите, кого ищем"); return; }
  if (!f.osm && !f.gis && !f.yandex && !f.dadata && !f.hh && !f.fns &&
      !f.trudvsem && !f.superjob) {
    toast("Выберите хотя бы один источник"); return; }
  // Отмечены только те, у кого нет ключа, — поиск вернётся пустым.
  if (!findReady()) {
    toast("У выбранных источников нет ключей — впишите их в «Ключи» "
          + "или отметьте OSM и hh.ru"); return; }
  const d = await post("/api/find", f);
  if (!d.ok) { toast(d.error || "не вышло"); return; }
  toast(`Ищу «${f.query}» — ${f.cities.length || 1} город(ов)`);
  showView("base");
  poll();
};

// Клавиатура в поле вида деятельности.
//
// Стрелки ведут по открытому списку, Enter берёт подсвеченное, а если
// ничего не подсвечено — запускает поиск: своё слово тоже ищется, и
// заставлять выбирать из списка было бы неправильно. Список при запуске
// закрывается, иначе он накрывает строку состояния, и человек не видит
// того, что сам только что начал.
$("q-text").addEventListener("keydown", (e) => {
  const box = $("dd-trade");
  const open = box.classList.contains("is-open");
  const vis = () => [...box.querySelectorAll(".dd-opt")].filter((o) => !o.hidden);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!open) { ddOpen(box); return; }
    const list = vis();
    if (!list.length) return;
    const i = list.findIndex((o) => o.classList.contains("is-cur"));
    const next = e.key === "ArrowDown"
      ? (i < 0 ? 0 : Math.min(i + 1, list.length - 1))
      : (i <= 0 ? 0 : i - 1);
    list.forEach((o) => o.classList.toggle("is-cur", o === list[next]));
    list[next].scrollIntoView({block: "nearest"});
    return;
  }
  if (e.key === "Escape") { ddClose(box); return; }
  if (e.key !== "Enter") return;
  e.preventDefault();
  const cur = open ? box.querySelector(".dd-opt.is-cur") : null;
  if (cur) { cur.click(); return; }
  ddClose(box);
  $("btn-find").click();
});

fillFindForm(window.LAST_FIND);

// Условия поиска собираются в одном месте — их и запускают, и сохраняют,
// и восстанавливают при следующем открытии программы.
function searchForm() {
  return {
    queries: $("f-text").value.split("\n").map((x) => x.trim()).filter(Boolean),
    areas: pickedAreas(),
    period: $("f-period").value,
    pages: $("f-pages").value,
    in_title: $("f-title").checked,
    skip_agencies: $("f-noagency").checked,
    max_open: $("f-maxopen").value,
    src_hh: $("f-src-hh").checked,
    src_trudvsem: $("f-src-trud").checked,
    src_superjob: $("f-src-sj").checked,
    then_enrich: $("f-then").checked,
    then_zakupki: $("f-then-zak").checked,
    then_ai: $("f-then-ai").checked,
  };
}

function fillSearchForm(p) {
  if (!p || !p.queries) return;
  $("f-text").value = (p.queries || []).join("\n");
  const areas = (p.areas || []).map(String);
  document.querySelectorAll(".area").forEach(
    (el) => el.classList.toggle("is-on", areas.includes(el.dataset.code)));
  if (p.period) $("f-period").value = String(p.period);
  if (p.pages) $("f-pages").value = String(p.pages);
  $("f-title").checked = p.in_title !== false;
  $("f-noagency").checked = p.skip_agencies !== false;
  $("f-maxopen").value = String(p.max_open || 0);
  const vs = p.sources || {};
  $("f-src-hh").checked = vs.hh !== false;
  $("f-src-trud").checked = vs.trudvsem !== false;
  $("f-src-sj").checked = vs.superjob !== false;
  $("f-then").checked = !!p.then_enrich;
  $("f-then-zak").checked = !!p.then_zakupki;
  $("f-then-ai").checked = !!p.then_ai;
  markPresets();
}

$("btn-search").onclick = () => {
  const f = searchForm();
  if (!f.queries.length) { toast("Впишите хотя бы один запрос"); return; }
  const chain = [f.then_enrich && "обогащение", f.then_ai && "ИИ"].filter(Boolean);
  run("/api/search", f,
    `Поиск: ${f.queries.length} запрос(ов) × ${f.areas.length || 1} регион(ов)` +
    (chain.length ? ` → ${chain.join(" → ")}` : ""));
};

// ── Сохранённые наборы ───────────────────────────────────
// Один и тот же набор условий повторяется еженедельно: вакансии новые,
// компании новые, условия те же. Набирать их заново — это ещё и разные
// условия от прогона к прогону, из-за которых непонятно, что изменилось.
async function loadSearches(rows) {
  const box = $("saved-list");
  const d = rows ? {rows} : await get("/api/searches");
  const list = (d && d.rows) || [];
  if (!list.length) { box.innerHTML = ""; return; }
  box.innerHTML = list.map((r) => `<span class="saved-item" data-id="${r.id}">
      <button class="saved-go" title="Повторить этот набор">${esc(r.name)}</button>
      <button class="saved-x" title="Удалить набор">×</button>
    </span>`).join("");
  box.querySelectorAll(".saved-item").forEach((el) => {
    const row = list.find((r) => String(r.id) === el.dataset.id);
    el.querySelector(".saved-go").onclick = () => {
      fillSearchForm(row.params);
      toast(`Набор «${row.name}» подставлен — проверьте и запускайте`);
    };
    el.querySelector(".saved-x").onclick = async () => {
      const d2 = await post(`/api/searches/${row.id}/delete`, {});
      loadSearches(d2.rows);
    };
  });
}

$("btn-save-search").onclick = async () => {
  const f = searchForm();
  if (!f.queries.length) { toast("Сначала впишите запросы"); return; }
  const name = prompt("Название набора", f.queries[0].slice(0, 40));
  if (!name) return;
  const d = await post("/api/searches", Object.assign({name}, f));
  if (!d.ok) { toast(d.error || "не сохранилось"); return; }
  loadSearches(d.rows);
  toast(`Набор «${name}» сохранён`);
};

fillSearchForm(window.LAST_SEARCH);
loadSearches();

$("btn-gis").onclick = () => run("/api/gis", {
  query: $("g-query").value, region: $("g-region").value,
  pages: $("g-pages").value}, "Поиск по 2ГИС");

// ── Telegram: вход и проверка номеров ────────────────────
//
// Вход идёт в два приёма, потому что так устроен сам Telegram: сначала
// он присылает код в приложение, и только потом принимает его обратно.
// Экран об этом говорит прямо, иначе «Войти» после «Прислать код»
// выглядит как вторая попытка того же действия.
async function tgState() {
  const d = await get("/api/tg/state");
  if (!d || !d.ok) return null;
  const tag = $("tag-tg"), tag2 = $("tag-tg2");
  let label = "аккаунт не подключён", ready = false;
  if (!d.lib) label = "библиотека не установлена";
  else if (!d.logged) label = "аккаунт не подключён";
  else if (!d.keys) label = "нет app_id и app_hash";
  else { label = "аккаунт подключён"; ready = true; }
  for (const el of [tag, tag2]) {
    if (!el) continue;
    el.textContent = ready ? "аккаунт подключён" : label;
    el.classList.toggle("ready", ready);
  }
  return d;
}

async function tgSaveKeys() {
  await post("/api/settings", {
    tg_api_id: $("s-tg-id").value.trim(),
    tg_api_hash: $("s-tg-hash").value.trim(),
    tg_phone: $("s-tg-phone").value.trim()});
}

if ($("btn-tg-code")) {
  const note = $("tg-state");
  $("btn-tg-code").onclick = async () => {
    await tgSaveKeys();
    note.textContent = "прошу код…";
    const d = await post("/api/tg/code", {phone: $("s-tg-phone").value.trim()});
    if (!d || !d.ok) {
      note.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
      return;
    }
    if (d.done) {
      note.textContent = `уже вошли: ${d.who || ""}`;
      tgState();
      return;
    }
    note.textContent = "код отправлен в Telegram — впишите его и нажмите «Войти»";
    $("s-tg-code").focus();
  };

  $("btn-tg-signin").onclick = async () => {
    await tgSaveKeys();
    note.textContent = "вхожу…";
    const d = await post("/api/tg/signin", {
      phone: $("s-tg-phone").value.trim(),
      code: $("s-tg-code").value.trim(),
      password: $("s-tg-pass").value.trim()});
    if (d && d.need_password) {
      $("tg-pass-wrap").hidden = false;
      $("s-tg-pass").focus();
      note.innerHTML = `<span class="bad">${esc(d.error)}</span>`;
      return;
    }
    if (!d || !d.ok) {
      note.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
      return;
    }
    $("s-tg-code").value = "";
    $("s-tg-pass").value = "";
    note.textContent = `вошли: ${d.who || ""}`;
    toast("Telegram подключён");
    tgState();
  };

  $("btn-tg-forget").onclick = async () => {
    if (!confirm("Забыть аккаунт Telegram? Для проверки номеров придётся "
                 + "войти заново.")) return;
    // Ответ смотрим, а не отчитываемся заранее: файл сессии может не
    // удалиться — например, его держит открытым другая копия программы.
    // Сказать «забыт» и оставить аккаунт подключённым хуже, чем честно
    // сообщить об отказе: человек уверен, что ключа больше нет.
    const d = await post("/api/tg/forget", {});
    if (d && d.ok) {
      note.textContent = "аккаунт забыт";
      toast("Аккаунт Telegram забыт");
    } else {
      note.innerHTML = `<span class="bad">не удалось удалить файл сессии — `
        + `закройте вторую копию программы и попробуйте снова</span>`;
    }
    tgState();
  };
}

// Готовый аккаунт: строка сессии или JSON от продавца, и отдельно
// файл .session, который к такому JSON обычно и прилагается.
if ($("btn-tg-account")) {
  const note = $("tg-acc-state");
  $("btn-tg-account").onclick = async () => {
    const text = $("s-tg-account").value.trim();
    if (!text) { toast("Вставьте JSON или строку сессии"); return; }
    note.textContent = "разбираю…";
    const d = await post("/api/tg/account", {text});
    if (!d || !d.ok) {
      note.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
      return;
    }
    // Строку сессии вычищаем сразу: это и есть ключ от аккаунта, и
    // висеть на экране ему незачем.
    $("s-tg-account").value = "";
    if (d.keys_only) {
      note.textContent = d.need_file
        ? "ключи приняты — теперь выберите файл .session"
        : "ключи приняты";
      if (d.error) note.innerHTML = `<span class="bad">${esc(d.error)}</span>`;
    } else {
      note.textContent = `аккаунт принят: ${d.who || ""}`;
      toast("Telegram подключён");
    }
    // Поля выше заполняем принятым: «ключи приняты» над пустой
    // строкой выглядит как «ничего не вышло».
    for (const [id, key] of [["s-tg-id", "api_id"], ["s-tg-hash", "api_hash"],
                             ["s-tg-phone", "phone"]]) {
      if (d[key] && $(id)) $(id).value = d[key];
    }
    tgState();
  };

}

// Оба файла аккаунта уходят одним запросом: .session и .json — это не
// два действия, а одно. Порядок и расширения неважны — что есть что,
// видно по самому содержимому.
if ($("s-tg-files")) {
  const note = $("tg-imp-state");
  $("s-tg-files").onchange = async () => {
    const files = [...$("s-tg-files").files];
    if (!files.length) return;
    note.textContent = files.length > 1
      ? `читаю ${files.length} файла…` : "читаю файл…";
    const form = new FormData();
    files.forEach((f) => form.append("files", f, f.name));
    $("s-tg-files").value = "";
    let d = null;
    try {
      const r = await fetch("/api/tg/import", {method: "POST", body: form});
      d = await r.json();
    } catch (e) { d = null; }
    if (!d || !d.ok) {
      note.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
      return;
    }
    note.textContent = `аккаунт подключён: ${d.who || ""}`;
    if (d.skipped) note.textContent += ` · пропущено: ${d.skipped}`;
    toast("Telegram подключён");
    tgState();
  };
}

if ($("btn-tg-proxy")) {
  $("btn-tg-proxy").onclick = async () => {
    const note = $("tg-proxy-state");
    note.textContent = "проверяю адрес…";
    const d = await post("/api/tg/proxy", {proxy: $("s-tg-proxy").value.trim()});
    note.innerHTML = (d && d.ok)
      ? (d.proxy ? `сохранено: ${esc(d.proxy)}` : "прокси выключен")
      : `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
  };
}

if ($("btn-tg-check")) {
  $("btn-tg-check").onclick = async () => {
    const d = await tgState();
    if (!d || !d.lib) { toast("Библиотека Telethon не установлена"); return; }
    if (!d.keys || !d.logged) {
      toast("Сначала войдите в Telegram — «Настройки»");
      showView("settings");
      return;
    }
    run("/api/tg/check", {limit: $("t-limit").value,
                          landlines: $("t-landlines").checked,
                          redo: $("t-redo").checked},
        "Проверка номеров в Telegram");
  };
}
tgState();

$("btn-import").onclick = () => {
  const text = $("i-text").value.trim();
  if (!text) { toast("Список пуст"); return; }
  run("/api/import", {text}, "Импорт");
};

// Три выпадающих списка: тема, вид деятельности, город
//
// Раньше здесь были два ряда одинаковых кнопок подряд, и это читалось
// как один выбор: человек нажимал тему «Строительство и ремонт», видел
// в поле «дизайн интерьера» с прошлого раза и считал, что программа
// ищет не то, что он выбрал. Теперь у каждого шага своё поле, в котором
// видно текущее значение, а список открывается только по нажатию.
//
// Список свой, а не системный <select>, по двум причинам: системный
// рисует операционная система, и посреди тёмной темы он оставался белым
// прямоугольником, — и в своём есть поиск по буквам, без которого
// полторы сотни городов листают глазами.

// Открытый список только один: два раскрытых меню перекрывают друг
// друга, и нажатие попадает не туда, куда человек смотрел.
function ddClose(box) {
  box.classList.remove("is-open");
  const menu = box.querySelector(".dd-menu");
  menu.hidden = true;
  box.querySelectorAll(".dd-opt.is-cur").forEach((o) => o.classList.remove("is-cur"));
  const head = box.querySelector(".dd-head");
  if (head) head.setAttribute("aria-expanded", "false");
}

function ddCloseAll(except) {
  document.querySelectorAll(".dd.is-open").forEach((b) => {
    if (b !== except) ddClose(b);
  });
}

function ddOpen(box) {
  ddCloseAll(box);
  box.classList.add("is-open");
  box.querySelector(".dd-menu").hidden = false;
  const head = box.querySelector(".dd-head");
  if (head) head.setAttribute("aria-expanded", "true");
  const find = box.querySelector(".dd-find");
  if (find) { find.value = ""; ddFilter(box); find.focus(); }
  // Выбранное должно быть видно сразу: список на полторы сотни строк
  // открывался всегда с начала, и выбранный Челябинск оставался
  // где-то ниже края.
  const on = box.querySelector(".dd-opt.is-on");
  if (on) on.scrollIntoView({block: "nearest"});
}

// Поиск по буквам внутри списка. Заголовок округа прячется вместе со
// всеми своими городами: пустой заголовок посреди выдачи выглядит как
// сбой отрисовки.
function ddFilter(box) {
  const find = box.querySelector(".dd-find");
  const q = find ? find.value.trim().toLowerCase() : "";
  let shown = 0;
  box.querySelectorAll(".dd-opt").forEach((o) => {
    const hit = !q || o.textContent.toLowerCase().includes(q);
    o.hidden = !hit;
    if (hit) shown += 1;
  });
  box.querySelectorAll(".dd-grp").forEach((g) => {
    const part = g.dataset.part;
    g.hidden = ![...box.querySelectorAll('.dd-opt[data-part="' + cssq(part) + '"]')]
      .some((o) => !o.hidden);
  });
  const none = box.querySelector(".dd-none");
  if (none) none.hidden = shown > 0;
}

// Название округа попадает в селектор, а кавычек и обратных слэшей в
// нём быть не должно. Своих кавычек там нет, но подставлять чужую
// строку в селектор без оглядки — привычка, которая однажды ломает
// страницу молча.
function cssq(v) { return String(v || "").replace(/["\\]/g, ""); }

document.querySelectorAll(".dd .dd-find").forEach((f) => {
  f.oninput = () => ddFilter(f.closest(".dd"));
  f.onkeydown = (e) => {
    if (e.key === "Escape") { ddClose(f.closest(".dd")); return; }
    if (e.key === "Enter") {
      e.preventDefault();
      const first = [...f.closest(".dd").querySelectorAll(".dd-opt")]
        .find((o) => !o.hidden);
      if (first) first.click();
    }
  };
});

document.querySelectorAll(".dd > .dd-head").forEach((h) => {
  if (h.tagName === "BUTTON") {
    h.onclick = () => {
      const box = h.closest(".dd");
      if (box.classList.contains("is-open")) ddClose(box); else ddOpen(box);
    };
  }
});

document.addEventListener("click", (e) => {
  if (!e.target.closest(".dd")) ddCloseAll(null);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") ddCloseAll(null);
});

// ── 1. Тема ──────────────────────────────────────────────
// Тема ничего не ищет: она сужает список видов. Об этом сказано и
// подписью к полю, и тем, что поиск запускается кнопкой, а не выбором.
let themePick = "";

function themeApply() {
  const box = $("dd-trade");
  box.querySelectorAll(".dd-opt").forEach((o) => {
    o.dataset.off = (themePick !== "" && o.dataset.theme !== themePick) ? "1" : "";
  });
  box.classList.toggle("by-theme", themePick !== "");
  // Спрятать по теме и по набранным буквам — одно и то же действие, и
  // делать его должен один кусок кода, иначе они начинают спорить.
  tradeFilter();
}

// Название темы по её номеру — чтобы сказать, где лежит спрятанное.
function themeTitle(idx) {
  const opt = $("dd-theme").querySelector(`.dd-opt[data-v="${cssq(idx)}"]`);
  return opt ? opt.textContent.trim().split("\n")[0].trim() : "";
}

function tradeFilter() {
  const box = $("dd-trade");
  const q = $("q-text").value.trim().toLowerCase();
  let shown = 0;
  let elsewhere = null;
  box.querySelectorAll(".dd-opt").forEach((o) => {
    const match = !q || o.dataset.q.toLowerCase().includes(q);
    const hit = !o.dataset.off && match;
    o.hidden = !hit;
    if (hit) shown += 1;
    else if (match && o.dataset.off && !elsewhere) elsewhere = o;
  });
  const none = box.querySelector(".dd-none");
  none.hidden = shown > 0;
  if (shown) return;
  // Вид есть, но спрятан выбранной темой.
  //
  // Самая обидная из возможных подписей — «такого нет» про то, что
  // есть. Человек набирает «лазерная резка», тема стоит «Медицина» с
  // прошлого раза, и список честно пуст. С полусотней тем это
  // случалось бы постоянно, поэтому говорим, где слово лежит, и даём
  // снять тему одним нажатием.
  if (elsewhere) {
    none.innerHTML = `«${esc(elsewhere.dataset.q)}» есть, но в теме
      «${esc(themeTitle(elsewhere.dataset.theme))}» —
      <button type="button" class="lnk" data-all-themes>показать все темы</button>`;
  } else {
    none.textContent = "Такого вида в списке нет — ищите по своему слову, "
      + "оно тоже работает";
  }
}

// Снять тему, не теряя набранного.
document.addEventListener("click", (e) => {
  if (!e.target.closest("[data-all-themes]")) return;
  e.preventDefault();
  e.stopPropagation();
  themePick = "";
  $("dd-theme").querySelectorAll(".dd-opt").forEach(
    (o) => o.classList.toggle("is-on", !o.dataset.v));
  $("dd-theme").querySelector(".dd-val").textContent = "Все темы";
  themeNote(null);
  themeApply();
});

$("dd-theme").querySelector(".dd-list").onclick = (e) => {
  const opt = e.target.closest(".dd-opt");
  if (!opt) return;
  themePick = opt.dataset.v;
  $("dd-theme").querySelectorAll(".dd-opt").forEach(
    (o) => o.classList.toggle("is-on", o === opt));
  $("dd-theme").querySelector(".dd-val").textContent =
    opt.textContent.trim().split("\n")[0].trim();
  themeApply();
  themeNote(opt);
  ddClose($("dd-theme"));
  // Тему выбрали, а поле вида осталось от прошлого раза — и это та самая
  // путаница, из-за которой «выбрал строительство, а ищет дизайн».
  // Открываем список видов сразу: следующий шаг очевиден.
  if (themePick !== "" && !tradeInTheme()) {
    $("q-text").value = "";
    markTrade();
    tradeFilter();
  }
  ddOpen($("dd-trade"));
};

// Чем ищется выбранная тема — картой или названием.
//
// OpenStreetMap описывает места, куда заходят: магазин, клинику,
// автосервис. Металлобаз, цехов и оптовых поставщиков в карте нет и не
// будет — их ищут по названию, и в таком деле название как раз всё и
// говорит: «Уралметаллопрокат» не назовёт себя иначе.
//
// Сказать это надо до запуска. Иначе человек выбирает «Металл и
// металлообработку», получает вдвое меньше, чем по «стоматологии», и
// решает, что программа стала хуже искать.
function themeNote(opt) {
  const note = $("theme-note");
  if (!opt || !opt.dataset.v) {
    note.textContent = "Только сужает список ниже. Можно не выбирать — "
      + "тогда доступны все виды сразу";
    note.classList.remove("warn-note");
    return;
  }
  if (opt.dataset.kind === "бизнес") {
    note.innerHTML = `Эта тема ищется <b>по названию</b>: производства и
      оптовых поставщиков в карте нет. Работают справочники, ЕГРЮЛ и hh —
      карта принесёт мало. Зато в таком деле название говорит само за
      себя.`;
    note.classList.add("warn-note");
  } else {
    note.innerHTML = `Ищется по карте: у ${esc(opt.dataset.onmap)} видов
      этой темы есть тег, и по ним найдутся даже те компании, у которых
      это не написано в названии.`;
    note.classList.remove("warn-note");
  }
}

// Лежит ли нынешнее слово в выбранной теме.
function tradeInTheme() {
  const cur = $("q-text").value.trim().toLowerCase();
  if (!cur) return true;
  return [...$("dd-trade").querySelectorAll(".dd-opt")].some(
    (o) => o.dataset.q.toLowerCase() === cur && o.dataset.theme === themePick);
}

// ── 2. Вид деятельности ──────────────────────────────────
// Поле остаётся полем ввода: своё слово программа искать умеет, и
// запирать человека в списке из трёхсот строк было бы хуже, чем помочь
// ему этим списком. Подпись честно говорит, что слово не из списка.
function markTrade() {
  const cur = $("q-text").value.trim().toLowerCase();
  let known = null;
  $("dd-trade").querySelectorAll(".dd-opt").forEach((el) => {
    const on = el.dataset.q.toLowerCase() === cur;
    el.classList.toggle("is-on", on);
    if (on) known = el;
  });
  const note = $("trade-note");
  if (!cur) {
    note.textContent = "Выберите из списка или впишите своё слово. "
      + "Короче — лучше: «стоматология» найдёт больше, чем "
      + "«стоматологическая клиника премиум-класса»";
    note.classList.remove("warn-note");
  } else if (!known) {
    note.innerHTML = `Ищем по своему слову <b>${esc($("q-text").value.trim())}</b> —
      в списке такого нет. Это работает, но по карте найдутся только те,
      у кого слово стоит в названии.`;
    note.classList.add("warn-note");
  } else if (known.classList.contains("weak")) {
    note.innerHTML = `<b>${esc(known.dataset.q)}</b> ищется только по названию —
      тега на карте у него нет, и компания, не назвавшая себя так,
      не найдётся. Справочники и ЕГРЮЛ ищут по тексту и помогут.`;
    note.classList.add("warn-note");
  } else {
    note.innerHTML = `<b>${esc(known.dataset.q)}</b> ищется по тегам карты —
      найдутся и те, у кого это не написано в названии.`;
    note.classList.remove("warn-note");
  }
}

// Сколько слов уйдёт в поиск и какие именно.
//
// Каждое лишнее слово — полный обход всех источников по всем городам
// заново. Молча умножить время прогона на четыре нельзя: человек
// решит, что программа зависла. Поэтому слова названы до запуска.
function alsoWords() {
  const q = $("q-text").value.trim();
  if (!q) return [];
  const extra = TRADE_ALSO[q] || [];
  return [q, ...extra.filter((w) => w.toLowerCase() !== q.toLowerCase())]
    .slice(0, MAX_WORDS);
}

function alsoNote() {
  const words = alsoWords();
  const note = $("also-note");
  const on = $("q-also").checked;
  if (words.length < 2) {
    note.textContent = "У этого слова близких в списке нет — "
      + "поиск пойдёт по нему одному";
    return;
  }
  note.innerHTML = on
    ? `Найдёт и ${words.slice(1).map((w) => "«" + esc(w) + "»").join(", ")}.
       Это ${words.length} обхода вместо одного — дольше, но компаний
       заметно больше`
    : `Выключено: ищем только «${esc(words[0])}». Мимо пройдут
       ${words.slice(1).map((w) => "«" + esc(w) + "»").join(", ")}`;
}

$("q-also").onchange = alsoNote;

$("dd-trade").querySelector(".dd-list").onclick = (e) => {
  const opt = e.target.closest(".dd-opt");
  if (!opt) return;
  $("q-text").value = opt.dataset.q;
  markTrade();
  alsoNote();
  tradeFilter();
  ddClose($("dd-trade"));
  toast("Вид: " + opt.dataset.q + ". Проверьте города и жмите «Найти компании»");
};

$("q-text").onfocus = () => ddOpen($("dd-trade"));
$("q-text").oninput = () => {
  if (!$("dd-trade").classList.contains("is-open")) ddOpen($("dd-trade"));
  markTrade();
  alsoNote();
  tradeFilter();
};
$("dd-trade").querySelector(".dd-arrow").onclick = () => {
  const box = $("dd-trade");
  if (box.classList.contains("is-open")) ddClose(box); else $("q-text").focus();
};

// ── 3. Города ────────────────────────────────────────────
// Города берём только из своего списка.
//
// Раньше это был поиск по всей странице, а кнопки округов в фильтре
// списка носили тот же класс — и «выбранным городом» поиска молча
// оказывалась кнопка с другой вкладки, без названия вовсе.
function pickedCities() {
  return [...$("q-cities").querySelectorAll(".city.is-on")]
    .map((el) => el.dataset.city);
}

function cityHead() {
  const picked = pickedCities();
  const val = $("city-val");
  if (!picked.length) val.textContent = "не выбран ни один город";
  else if (picked.length <= 2) val.textContent = picked.join(", ");
  else val.textContent = picked.slice(0, 2).join(", ")
    + " и ещё " + (picked.length - 2);
}

function citiesNote() {
  const picked = pickedCities();
  const note = $("cities-note");
  cityHead();
  if (!picked.length) {
    note.innerHTML = `<span class="warn-note">Не выбран ни один город —
      поиск не пойдёт.</span>`;
    return;
  }
  if (picked.includes(WHOLE_RU)) {
    note.innerHTML = `<span class="warn-note">«${esc(WHOLE_RU)}» отключает
      справочники: они ищут по карте, а не по стране. Останутся ЕГРЮЛ и
      hh, а карточки выйдут без телефонов и сайтов.</span>`;
    return;
  }
  // Города, которых нет в справочниках 2ГИС и hh, ищутся по карте и по
  // ЕГРЮЛ. Сказать это надо до запуска: «нашлось вдвое меньше» без
  // объяснения читается как поломка программы.
  const thin = picked.filter((n) => {
    const el = $("q-cities").querySelector('.city[data-city="' + cssq(n) + '"]');
    return el && !el.querySelector(".ok-em");
  });
  let t = "Выбрано: " + picked.length + " — поиск обойдёт каждый";
  if (thin.length) {
    t += ". Без 2ГИС и hh: " + thin.slice(0, 3).join(", ")
      + (thin.length > 3 ? " и ещё " + (thin.length - 3) : "")
      + " — этих городов нет в их справочниках, останутся карта и ЕГРЮЛ";
  }
  note.textContent = t;
}

$("q-cities").onclick = (e) => {
  const all = e.target.closest(".dd-all");
  if (all) {
    // Округ целиком: «выбрать Поволжье» — это одно нажатие вместо
    // двадцати шести, и обратно тоже одно.
    const opts = [...$("q-cities").querySelectorAll(
      '.city[data-part="' + cssq(all.dataset.part) + '"]')];
    const turnOn = opts.some((o) => !o.classList.contains("is-on"));
    if (turnOn) {
      const w = $("q-cities").querySelector('.city[data-city="' + cssq(WHOLE_RU) + '"]');
      if (w) w.classList.remove("is-on");
    }
    opts.forEach((o) => o.classList.toggle("is-on", turnOn));
    citiesNote();
    return;
  }
  const el = e.target.closest(".city");
  if (!el) return;
  if (el.dataset.city === WHOLE_RU) {
    // «Россия целиком» и города — взаимоисключающие: вместе они значат
    // то же, что «Россия целиком», только дольше.
    const on = !el.classList.contains("is-on");
    $("q-cities").querySelectorAll(".city").forEach(
      (x) => x.classList.remove("is-on"));
    el.classList.toggle("is-on", on);
  } else {
    const w = $("q-cities").querySelector('.city[data-city="' + cssq(WHOLE_RU) + '"]');
    if (w) w.classList.remove("is-on");
    el.classList.toggle("is-on");
  }
  citiesNote();
};

// Города, выбранные в фильтре списка, — другой набор кнопок и другая
// вкладка, но правило «Россия против отдельных регионов» то же.
function pickedAreas() {
  return [...document.querySelectorAll(".area.is-on")].map((el) => el.dataset.code);
}

$("f-area").onclick = (e) => {
  const el = e.target.closest(".area");
  if (!el) return;
  const all = el.dataset.code === "113";
  if (all) {
    const on = !el.classList.contains("is-on");
    document.querySelectorAll(".area").forEach((x) => x.classList.remove("is-on"));
    el.classList.toggle("is-on", on);
  } else {
    const r = document.querySelector('.area[data-code="113"]');
    if (r) r.classList.remove("is-on");
    el.classList.toggle("is-on");
  }
};

themeApply();
markTrade();
alsoNote();
citiesNote();

// ── Повтор поиска по расписанию ──────────────────────────
//
// Зачем. Повторный поиск по той же теме приносит ту же тысячу
// компаний, и десять новых в ней глазами не найти. Программа и так
// знает, кого добавила в последний прогон, — осталось дать это
// показать и не заставлять человека нажимать кнопку каждую неделю.
//
// Честно про условие: повтор идёт, только пока программа открыта. В
// службы она не ставится, по будильнику не просыпается, и об этом
// сказано прямо под выбором срока, а не в справке.
function planWord(days) {
  const d = Number(days) || 0;
  if (!d) return "вручную";
  if (d === 1) return "раз в день";
  if (d === 7) return "раз в неделю";
  if (d === 14) return "раз в 2 недели";
  if (d === 30) return "раз в месяц";
  return `раз в ${d} ${plural(d, "день", "дня", "дней")}`;
}

function whenNext(ts) {
  if (!ts) return "";
  const left = ts * 1000 - Date.now();
  if (left <= 0) return "вот-вот";
  const h = Math.round(left / 3600000);
  if (h < 24) return `через ${h} ${plural(h, "час", "часа", "часов")}`;
  const d = Math.round(h / 24);
  return `через ${d} ${plural(d, "день", "дня", "дней")}`;
}

function drawPlans(rows) {
  const box = $("q-plans");
  const mine = (rows || []).filter((r) => r.kind === "find");
  if (!mine.length) { box.innerHTML = ""; return; }
  box.innerHTML = mine.map((r) => `
    <div class="plan${r.enabled ? "" : " off"}" data-id="${r.id}">
      <span class="plan-n">${esc(r.name)}</span>
      <span class="plan-q">«${esc(r.params.query || "")}» ·
        ${esc((r.params.cities || []).slice(0, 2).join(", "))}${
          (r.params.cities || []).length > 2
            ? " и ещё " + ((r.params.cities || []).length - 2) : ""}</span>
      <span class="plan-w">${esc(planWord(r.every_days))}${
        r.enabled && r.every_days && r.next_run
          ? " · " + esc(whenNext(r.next_run)) : ""}${
        r.runs ? " · прогонов " + r.runs : ""}</span>
      <span class="plan-do">
        <button class="lnk" data-do="run">запустить</button>
        <button class="lnk" data-do="toggle">${r.enabled ? "выключить" : "включить"}</button>
        <button class="lnk warn" data-do="del">удалить</button>
      </span>
    </div>`).join("");
}

async function loadPlans() {
  const d = await (await fetch("/api/searches")).json();
  if (d.ok) drawPlans(d.rows);
}

$("btn-plan-save").onclick = async () => {
  const name = $("q-plan-name").value.trim();
  if (!name) { $("q-plan-name").focus(); toast("Назовите набор — иначе его не найти потом"); return; }
  const f = findForm();
  if (!f.query) { toast("Впишите, кого ищем"); return; }
  if (!f.cities.length) { toast("Выберите хотя бы один город"); return; }
  const d = await post("/api/searches", Object.assign({}, f, {
    name, kind: "find", every_days: $("q-plan-every").value}));
  if (!d.ok) { toast(d.error || "не вышло"); return; }
  drawPlans(d.rows);
  $("q-plan-name").value = "";
  const every = Number($("q-plan-every").value) || 0;
  toast(every ? `Сохранено. Повтор ${planWord(every)}, пока программа открыта`
              : "Сохранено. Повторять само не будет — запускайте кнопкой");
};

$("q-plans").onclick = async (e) => {
  const btn = e.target.closest(".lnk");
  if (!btn) return;
  const row = btn.closest(".plan");
  const id = row.dataset.id;
  if (btn.dataset.do === "del") {
    if (!confirm("Удалить набор? Найденные компании останутся, уйдёт только повтор.")) return;
    const d = await post(`/api/searches/${id}/delete`, {});
    if (d.ok) { drawPlans(d.rows); toast("Набор удалён"); }
    return;
  }
  if (btn.dataset.do === "toggle") {
    const d = await post(`/api/searches/${id}/plan`,
                         {enabled: row.classList.contains("off")});
    if (d.ok) { drawPlans(d.rows); }
    return;
  }
  const d = await post(`/api/searches/${id}/run`, {});
  if (!d.ok) { toast(d.error || "не вышло"); return; }
  drawPlans(d.rows);
  toast("Запустил");
  showView("base");
  poll();
};

loadPlans();

$("btn-socials").onclick = () => run("/api/socials",
  {limit: $("e-limit").value, only_empty: true}, "Поиск соцсетей");
$("btn-enrich").onclick = () => run("/api/enrich", {
  limit: $("e-limit").value, verify: $("e-verify").checked,
  fns: $("e-fns").checked, zakupki: $("e-zakupki").checked,
  vk: $("e-vk").checked,
  only_lpr: $("e-only-lpr").checked}, "Обогащение");

$("btn-ai").onclick = () => run("/api/ai", {
  icp: $("a-icp").value, offer: $("a-offer").value,
  terms: $("a-terms").value, threads: $("a-threads").value,
  limit: $("a-limit").value, redo: $("a-redo").checked}, "ИИ-анализ");

$("btn-ai-check").onclick = async () => {
  toast("Проверяю связь…");
  const d = await post("/api/ai/check", {});
  toast(d.ok ? `Модель ${d.model} отвечает — ${d.note}` : `Не отвечает: ${d.note}`);
};

// Человек описывает клиента словами, а искать надо запросами. Между этим
// лежит шаг, на котором обычно и промахиваются.
$("btn-ai-q").onclick = async () => {
  const icp = $("a-icp").value.trim();
  if (!icp) { toast("Сначала опишите, кого ищете"); return; }
  const box = $("ai-tips");
  box.hidden = false;
  box.innerHTML = `<div class="skeleton" style="height:60px"></div>`;
  const d = await post("/api/ai/queries", {icp});
  if (!d.ok) { box.innerHTML = `<p class="note">${esc(d.error || "не вышло")}</p>`; return; }
  const group = (title, items, kind) => (items || []).length ? `
    <h5>${title}</h5>${items.map(
      (x) => `<button class="pick" data-kind="${kind}" data-v="${esc(x)}">${esc(x)}</button>`
    ).join("")}` : "";
  box.innerHTML = group("Запросы для hh.ru", d.hh, "hh") +
                  group("Рубрики 2ГИС", d.gis, "gis") +
                  group("ОКВЭД", d.okved, "") +
                  (d.note ? `<p class="note">${esc(d.note)}</p>` : "");
  // Подсказка бесполезна, если её надо перепечатывать руками.
  box.querySelectorAll(".pick").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.kind === "hh") { $("f-text").value = b.dataset.v; toast("Подставлено в запрос hh.ru"); }
      else if (b.dataset.kind === "gis") { $("g-query").value = b.dataset.v; toast("Подставлено в рубрику 2ГИС"); }
      else copy(b.dataset.v);
    };
  });
};

$("btn-diag").onclick = async () => {
  const box = $("diag");
  box.hidden = false;
  box.innerHTML = `<div class="skeleton" style="height:120px"></div>`;
  const d = await post("/api/diag", {});
  if (!d || !d.ok) { box.innerHTML = `<p class="hint-sm">Проверка не выполнилась.</p>`; return; }
  box.innerHTML = d.rows.map((r) => {
    const cls = r.ok === true ? "ok" : r.ok === false ? "bad" : "skip";
    const mark = r.ok === true ? "✓" : r.ok === false ? "✕" : "—";
    return `<div class="diag-row ${cls}">
      <span class="diag-mark">${mark}</span>
      <span class="diag-name">${esc(r.name)}</span>
      <span class="diag-note">${esc(r.note)}${r.ms ? ` · ${r.ms} мс` : ""}</span>
      ${r.hint ? `<span class="diag-tip">${esc(r.hint)}</span>` : ""}
    </div>`;
  }).join("");
};

$("btn-stop").onclick = () => { post("/api/stop"); toast("Останавливаю…"); };
// Журнал и история — одно место, две вкладки: две раскрытые панели
// подряд уже не читаются.
$("btn-log").onclick = () => {
  $("hist").hidden = true;
  const l = $("log");
  l.hidden = !l.hidden;
  $("btn-log").textContent = l.hidden ? "Журнал" : "Скрыть журнал";
};

$("btn-dedupe").onclick = async () => {
  const found = await post("/api/dedupe", {dry: true});
  if (!found || !found.ok) { toast("не вышло"); return; }
  if (!found.found) {
    // Чистка телефонов полезна и без дублей.
    const d0 = await post("/api/dedupe", {});
    toast(d0.phones ? `Дублей нет, выброшено телефонов-заглушек: ${d0.phones}`
                    : "Дублей не нашлось, телефоны чистые");
    if (d0.phones) { loadCompanies(true); loadStats(); }
    return;
  }
  if (!confirm(`Похожих пар: ${found.found}. Склеить? Контакты, заметки и `
             + `реквизиты перейдут на одну запись, вторая исчезнет.`)) return;
  const d = await post("/api/dedupe", {});
  toast(`Склеено: ${d.merged}`
    + (d.phones ? `, выброшено телефонов-заглушек: ${d.phones}` : ""));
  loadCompanies(true); loadStats();
};

$("btn-clear").onclick = async () => {
  if (!confirm("Удалить все найденные компании и контакты? Отменить нельзя.")) return;
  await post("/api/clear", {});
  loadCompanies(); loadStats(); poll();
  toast("База очищена");
};

// ── Настройки ────────────────────────────────────────────
// Ключи и пароли закрыты точками, пока их не попросят показать.
//
// Половина полей была открытым текстом, половина — точками, без всякой
// причины: токен справочника такой же ключ, как ключ модели, а в строке
// прокси стоит логин с паролем. Достаточно снять экран настроек — и всё
// это уходит вместе со снимком. Показать по-прежнему можно: набранный
// ключ надо чем-то проверить, и «покажите, что я ввёл» — законная
// просьба.
document.querySelectorAll("[data-secret]").forEach((inp) => {
  // Обёртка ставится здесь, а не в разметке: поля с ключами лежат в
  // карточках разного устройства, и полагаться на то, что соседним
  // элементом окажется нужный, нельзя — кнопка уезжала под поле и
  // накрывала подсказку.
  const wrap = document.createElement("span");
  wrap.className = "secret-wrap";
  inp.parentNode.insertBefore(wrap, inp);
  wrap.appendChild(inp);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "peek";
  btn.textContent = "показать";
  btn.title = "Показать и снова скрыть";
  btn.onclick = () => {
    const open = inp.type === "text";
    inp.type = open ? "password" : "text";
    btn.textContent = open ? "показать" : "скрыть";
    btn.classList.toggle("is-on", !open);
  };
  wrap.appendChild(btn);
});

$("s-save").onclick = async () => {
  await post("/api/settings", {
    dadata_token: $("s-dadata").value, gis_key: $("s-gis").value,
    ai_key: $("s-ai-key").value, ai_url: $("s-ai-url").value,
    ai_model: $("s-ai-model").value, ai_kind: $("s-ai-kind").value,
    proxy_url: $("s-proxy").value,
    hh_token: $("s-hh-token").value,
    yandex_key: $("s-yandex").value, vk_token: $("s-vk").value,
    sj_key: $("s-sj").value,
    update_repo: $("s-upd-repo").value, update_token: $("s-upd-token").value,
    ...mailFields()});
  markKeys();
  findReady();
  const note = $("save-note");
  note.textContent = "сохранено";
  setTimeout(() => { note.textContent = ""; }, 2500);
  toast("Ключи сохранены");
};
function markKeys() {
  // Карточка источника должна сама сообщать, готова она к работе: иначе
  // человек жмёт «Найти» и получает ошибку вместо результата.
  const mailReady = !!($("s-mail-address").value.trim() &&
                        $("s-mail-password").value.trim());
  $("tag-mail").classList.toggle("ready", mailReady);
  $("tag-mail").textContent = mailReady ? "ящик указан" : "не настроена";
  for (const [input, tag] of [["s-gis", "tag-gis"], ["s-ai-key", "tag-ai"]]) {
    const ready = !!$(input).value.trim();
    const el = $(tag);
    el.classList.toggle("ready", ready);
    el.textContent = ready ? "ключ есть" : "нужен ключ";
  }
  // Карточки источников на «Поиске» — там же, где человек их отмечает.
  const sjNote = $("f-sj-note");
  if (sjNote) sjNote.textContent = hasKey("sj") ? "ключ есть"
    : "нужен бесплатный ключ в «Настройках»";
  for (const [what, yes, no] of [["gis", "ключ есть", "нет ключа"], ["sj", "ключ есть", "нет ключа"],
                                 ["yandex", "ключ есть", "нет ключа"],
                                 ["dadata", "токен есть", "нет токена"]]) {
    const ready = hasKey(what), badge = $("src-key-" + what);
    const hint = $("src-hint-" + what);
    if (badge) {
      badge.textContent = ready ? yes : no;
      badge.className = ready ? "ok" : "need";
    }
    if (hint) hint.innerHTML = ready ? hint.dataset.yes : hint.dataset.no;
  }
}
markKeys();

// ── Обновления ───────────────────────────────────────────
let pending = null;

async function updCheck(quiet) {
  const state = $("upd-state");
  if (!quiet) state.textContent = "проверяю…";
  // Сначала сохраняем поля: проверять со старым адресом, пока в форме
  // уже вписан новый, — верный способ запутать.
  if (!quiet) await post("/api/settings", {
    update_repo: $("s-upd-repo").value, update_token: $("s-upd-token").value});
  const d = await get("/api/update/check");
  if (!d || !d.ok) {
    if (!quiet) state.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
    return;
  }
  pending = d.newer ? d : null;
  $("rail-upd").hidden = !d.newer;
  if (d.newer) {
    // Установленная версия обновляется установщиком и сама закрывается,
    // запуск из исходников — распаковкой. Предупреждать об этом нужно
    // до нажатия, а не после того, как окно исчезло.
    const how = d.kind === "installer"
      ? "программа закроется и обновится сама"
      : "файлы обновятся на месте";
    // Ссылка на файл — рядом, а не только в тексте ошибки. Маршрут
    // браузера до github.com отличается от нашего, и там, где программа
    // не прошла, он часто скачивает без вопросов.
    const direct = d.kind === "installer" ? (d.setup || "") : (d.zip || "");
    state.innerHTML = `Доступна <b>${esc(d.latest)}</b> — у вас ${esc(d.current)}
      <button class="btn primary sm" id="s-upd-go">Обновить</button>
      <span class="hint">${esc(how)}</span>` +
      (direct ? ` <a class="hint" href="${safeUrl(direct)}" target="_blank">скачать вручную</a>` : "");
    $("s-upd-go").onclick = updApply;
  } else if (!quiet) {
    state.textContent = `установлена последняя версия (${d.current})`;
  }
}

async function updApply() {
  const state = $("upd-state");
  const btn = $("s-upd-go");
  if (btn) { btn.disabled = true; btn.textContent = "обновляю…"; }
  state.innerHTML = `<span class="muted">скачиваю… не закрывайте программу</span>`;
  const d = await post("/api/update/apply", {});
  if (d && d.ok) {
    state.innerHTML = `<span class="good">${esc(d.message)}</span>`;
    if (d.restart) {
      // Сервер уже назначил себе выход. Дальше показывать живой
      // интерфейс нечестно: кнопки перестанут отвечать через секунду.
      document.body.classList.add("closing");
      toast("Обновляюсь — окно закроется само");
    } else {
      toast("Обновлено — закройте и откройте программу");
    }
  } else {
    if (btn) { btn.disabled = false; btn.textContent = "Обновить"; }
    const text = (d && (d.error || d.message)) || "не вышло";
    // В сообщении об отказе есть адрес файла. Заставлять выделять его
    // мышью из красного текста — издевательство: делаем ссылкой.
    state.innerHTML = `<span class="bad">${esc(text).replace(
      /(https?:\/\/[^\s]+)/g, '<a href="$1" target="_blank">$1</a>')}</span>`;
  }
}

// Формат и адрес должны сходиться. Раньше человек выбирал «Anthropic
// (Claude)», а в поле адреса оставался api.openai.com от значений по
// умолчанию — и запрос уходил на api.openai.com/v1/messages, которого
// там нет. Ответом был голый 404, по которому не догадаться, что
// виноват адрес.
const OPENAI_URL = "https://api.openai.com/v1";
function aiKindNote() {
  const u = $("s-ai-url").value.trim().toLowerCase();
  const k = $("s-ai-kind").value;
  const note = $("ai-cfg-note");
  if (!u) { note.textContent = ""; return; }
  const looksAnthropic = /\/v1\/messages|anthropic|claude|router\.cheap/.test(u);
  const looksOpenAI = /openai\.com|deepseek|openrouter/.test(u);
  const m = $("s-ai-model").value.trim().toLowerCase();
  const say = [];
  if (k === "anthropic" && looksOpenAI) {
    say.push("Формат Anthropic, а адрес ведёт на OpenAI — запрос не пройдёт."
      + " Впишите адрес от посредника Claude.");
  } else if (k === "openai" && looksAnthropic) {
    say.push("Адрес похож на посредника Claude — выберите формат"
      + " «Anthropic (Claude)».");
  }
  // Имя модели остаётся в поле от прежних настроек, и человек видит одно,
  // а уходит другое. Сказать про это надо до отправки, а не в ошибке.
  if (k === "anthropic" && /^(gpt-|o1-|o3-|text-|davinci)/.test(m)) {
    say.push("Модель «" + $("s-ai-model").value.trim() + "» посреднику Claude"
      + " не известна — программа возьмёт claude-…. Выберите свою кнопкой"
      + " «Показать доступные модели».");
  }
  if (k === "openai" && /^claude-/.test(m)) {
    say.push("Модель «" + $("s-ai-model").value.trim() + "» не из формата"
      + " OpenAI — программа возьмёт свою по умолчанию.");
  }
  note.innerHTML = say.length
    ? `<span class="bad">${esc(say.join(" "))}</span>` : "";
}

$("s-ai-url").onchange = () => {
  const u = $("s-ai-url").value.toLowerCase();
  if (/\/v1\/messages|anthropic|claude|router\.cheap/.test(u)) {
    $("s-ai-kind").value = "anthropic";
  }
  aiKindNote();
};

// Смена формата чистит чужой адрес и чужую модель. Оставлять
// gpt-4o-mini при формате Anthropic незачем: такой модели у посредника
// Claude нет, и первый же запрос вернёт ошибку про неизвестную модель.
$("s-ai-model").onchange = aiKindNote;
$("s-ai-kind").onchange = () => {
  const k = $("s-ai-kind").value;
  const url = $("s-ai-url");
  const model = $("s-ai-model");
  if (k === "anthropic") {
    if (!url.value.trim() || /openai\.com/i.test(url.value)) url.value = "";
    url.placeholder = "https://router.cheap";
    if (/^gpt-/i.test(model.value.trim())) model.value = "";
    model.placeholder = "claude-… — возьмите из списка ниже";
  } else {
    if (/\/v1\/messages|anthropic|router\.cheap/i.test(url.value)) url.value = "";
    url.placeholder = OPENAI_URL;
    if (/^claude-/i.test(model.value.trim())) model.value = "";
    model.placeholder = "gpt-4o-mini";
  }
  aiKindNote();
};
aiKindNote();

// Ключ в поле-пароле нельзя ни проверить глазами, ни сверить с тем, что
// прислал продавец: видно только точки. Пока поле в фокусе — показываем.
document.querySelectorAll('input[type=password]').forEach((el) => {
  el.addEventListener("focus", () => { el.type = "text"; });
  el.addEventListener("blur", () => { el.type = "password"; });
});

$("btn-ai-models").onclick = async () => {
  const note = $("ai-cfg-note");
  note.textContent = "спрашиваю…";
  // Сначала сохраняем: спрашивать по старому адресу, когда в поле уже
  // новый, — верный способ запутать.
  await post("/api/settings", {ai_key: $("s-ai-key").value,
    ai_url: $("s-ai-url").value, ai_kind: $("s-ai-kind").value,
    proxy_url: $("s-proxy").value});
  const d = await get("/api/ai/models");
  if (!d || !d.ok) {
    note.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
    return;
  }
  $("ai-models").innerHTML = d.models.map(
    (m) => `<option value="${esc(m)}">`).join("");
  note.textContent = d.models.length
    ? `моделей: ${d.models.length} — начните печатать в поле «Модель»`
    : "список пуст, впишите название из документации роутера";
};

$("btn-ai-check2").onclick = async () => {
  const note = $("ai-cfg-note");
  note.textContent = "проверяю…";
  await post("/api/settings", {ai_key: $("s-ai-key").value,
    ai_url: $("s-ai-url").value, ai_kind: $("s-ai-kind").value,
    ai_model: $("s-ai-model").value, proxy_url: $("s-proxy").value});
  const d = await post("/api/ai/check", {});
  if (d && d.models && d.models.length) {
    $("ai-models").innerHTML = d.models.map(
      (m) => `<option value="${esc(m)}">`).join("");
    // Поле пустое — подставляем первую из списка: человек модели не
    // выбирал, выбираем всё равно мы, так пусть это будет та, которая
    // у посредника есть.
    if (!$("s-ai-model").value.trim()) $("s-ai-model").value = d.models[0];
  }
  note.innerHTML = d.ok
    ? `<span class="good">${esc(d.model)} (${esc(d.kind)}) — ${esc(d.note)}</span>`
    : `<span class="bad">${esc(d.note)}</span>`;
};

// Кабинет hh выдаёт не токен, а пару Client Id и Client Secret, и поле
// «Токен hh.ru» её не принимает. Обмен делаем здесь, одной кнопкой:
// посылать человека в командную строку ради одного запроса незачем.
if ($("btn-hh-token")) {
  $("btn-hh-token").onclick = async () => {
    const note = $("hh-token-state");
    const cid = $("s-hh-cid").value.trim();
    const secret = $("s-hh-secret").value.trim();
    if (!cid || !secret) {
      note.innerHTML = `<span class="bad">нужны оба: Client Id и Client Secret</span>`;
      return;
    }
    note.textContent = "спрашиваю hh…";
    const d = await post("/api/hh/token", {client_id: cid, client_secret: secret});
    // Секрет стираем в любом случае: на экране ему делать нечего.
    $("s-hh-secret").value = "";
    if (!d || !d.ok) {
      note.innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
      return;
    }
    $("s-hh-cid").value = "";
    // Токен ставим в поле сразу: иначе следующее «Сохранить» отправит
    // пустое поле и затрёт только что полученный токен.
    $("s-hh-token").value = d.token || "";
    note.textContent = `токен получен и сохранён (${d.masked})`;
    toast("Токен hh.ru сохранён");
  };
}

$("s-open-data").onclick = async () => {
  const d = await post("/api/reveal", {});
  if (!d || !d.ok) toast((d && d.error) || "папка не открылась");
};

$("s-upd-check").onclick = () => updCheck(false);
$("rail-upd").onclick = () => { showView("settings"); updCheck(false); };
// Тихая проверка при старте: значок в углу появится сам, но ничем не
// помешает, если обновления нет.
setTimeout(() => updCheck(true), 3000);

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCard();
});

// ── Ход работы ───────────────────────────────────────────
const STATUS = {queued: "в очереди", running: "выполняется", done: "готово",
                error: "ошибка", stopped: "остановлено"};
const KINDS = {find: "Поиск компаний", hh_search: "Поиск по вакансиям",
               socials: "Поиск соцсетей",
               gis_search: "Поиск по 2ГИС", import: "Импорт списка",
               enrich: "Обогащение", ai: "ИИ-анализ",
               tg: "Проверка номеров в Telegram"};
let lastTask = null;

// Сколько ещё ждать.
//
// Обход пятисот компаний идёт полчаса, и всё это время «120 из 500» не
// отвечает на единственный вопрос: уходить пить чай или досмотреть до
// конца. Считаем по скорости самого прогона, а не по средней от балды:
// источники отвечают по-разному, и одна и та же задача идёт то пять
// минут, то сорок.
const rate = {id: null, t0: 0, done0: 0};
function eta(t) {
  const now = Date.now();
  if (rate.id !== t.id) { rate.id = t.id; rate.t0 = now; rate.done0 = t.done; return ""; }
  const passed = (now - rate.t0) / 1000, made = t.done - rate.done0;
  if (!t.total || made < 3 || passed < 8) return "";
  const left = Math.round((t.total - t.done) * passed / made);
  if (left < 45) return " · осталось меньше минуты";
  if (left < 3600) return ` · осталось ~${Math.round(left / 60)} мин`;
  return ` · осталось ~${(left / 3600).toFixed(1)} ч`;
}

async function poll() {
  const d = await get("/api/task");
  if (!d) return;
  const t = d.task;
  const box = $("run");
  const other = d.elsewhere;
  const otherText = other
    ? `в проекте «${other.project}»: ${KINDS[other.kind] || other.kind}` +
      (other.total ? ` · ${other.done} из ${other.total}` : "")
    : "";
  if (!t) {
    // Здесь ничего не идёт, но идёт в другом проекте — это надо видеть,
    // иначе новая задача будто бы «не стартует»: она ждёт своей очереди.
    box.hidden = !other;
    if (other) {
      box.classList.add("is-live");
      box.classList.remove("is-bad");
      $("fill").style.width = (other.total ? Math.round(100 * other.done / other.total) : 6) + "%";
      $("run-status").textContent = "Идёт " + otherText;
      $("btn-stop").hidden = false;
    }
    return;
  }

  box.hidden = false;
  const live = t.status === "running" || t.status === "queued";
  box.classList.toggle("is-live", live);
  box.classList.toggle("is-bad", t.status === "error");

  const pct = t.total ? Math.round(100 * t.done / t.total) : (live ? 6 : 100);
  $("fill").style.width = pct + "%";
  $("run-status").textContent =
    `${KINDS[t.kind] || t.kind} — ${STATUS[t.status] || t.status}` +
    (t.total ? ` · ${t.done} из ${t.total}` : "") +
    (live ? eta(t) : "") +
    (d.queued > 1 ? ` · в очереди ещё ${d.queued - 1}` : "") +
    (t.message ? ` · ${t.message}` : "") +
    (otherText ? ` · сейчас работает ${otherText}` : "");
  $("btn-stop").hidden = !live;

  drawQueue(d.queue || []);

  $("log").innerHTML = (d.logs || [])
    .map((l) => `<span class="${l.level}">${esc(l.text)}</span>`).join("\n");
  if (!$("log").hidden) $("log").scrollTop = $("log").scrollHeight;

  // Сообщаем о завершении один раз: человек в это время смотрит в другое
  // окно и возвращается, только когда что-то произошло.
  if (lastTask && lastTask.id === t.id && lastTask.status !== t.status && !live) {
    toast(`${KINDS[t.kind] || t.kind}: ${STATUS[t.status] || t.status}`);
    loadCompanies(); loadStats();
  }
  lastTask = {id: t.id, status: t.status};
  // Таблицу во время работы обновляем не чаще раза в пять секунд и
  // только когда что-то прибавилось. Перезапрашивать пятьсот строк
  // каждые полторы секунды — значит соревноваться за базу с тем, что
  // как раз в неё пишет; снаружи это выглядит как зависшая программа.
  // Пока задача идёт, таблицу трогаем редко. Раньше она обновлялась на
  // каждой пройденной компании — то есть каждые пару секунд, и всё это
  // время программа качала и разбирала ответ вместо того, чтобы
  // отвечать на нажатия. Данные за пятнадцать секунд не устаревают.
  if (live && Date.now() - liveSeen.at > 15000) {
    liveSeen = {done: t.done, at: Date.now()};
    loadCompanies(); loadStats();
  }
}
let liveSeen = {done: -1, at: 0};

// ── Очередь: что будет после того, что идёт сейчас ───────
//
// Раньше здесь было только число: «в очереди ещё 2». Оно не говорило ни
// что это за задачи, ни как их отменить. Человек, передумавший на
// середине, мог только ждать, пока программа доделает то, чего он уже
// не хочет, — а обогащение с разбором ИИ идут часами.
function taskWhat(t) {
  const p = t.params || {};
  if (p.query) return `«${p.query}»`;
  if (p.queries && p.queries.length) return `«${p.queries[0]}»`;
  if (p.limit) return `до ${p.limit}`;
  return "";
}

function drawQueue(rows) {
  const box = $("queue");
  box.hidden = !rows.length;
  if (!rows.length) return;
  box.innerHTML = `<span class="q-lbl">Дальше в очереди:</span>`
    + rows.map((t) => `<span class="q-item">${esc(KINDS[t.kind] || t.kind)}${
        taskWhat(t) ? " " + esc(taskWhat(t)) : ""}<button class="q-x"
        data-cancel="${t.id}" title="Убрать из очереди">×</button></span>`).join("")
    + (rows.length > 1
        ? `<button class="lnk" data-clear-queue>отменить всё</button>` : "");
}

$("queue").onclick = async (e) => {
  const one = e.target.closest("[data-cancel]");
  const all = e.target.closest("[data-clear-queue]");
  if (!one && !all) return;
  const d = all ? await post("/api/queue/clear", {})
                : await post(`/api/task/${one.dataset.cancel}/cancel`, {});
  if (!d.ok) return;
  drawQueue(d.queue || []);
  toast(all ? "Очередь очищена" : "Убрано из очереди");
};

// ── Что было: последние задачи ───────────────────────────
//
// Задача, упавшая час назад, была невидима совсем: строка состояния
// показывает только текущую. Человек помнит, что «что-то запускал», а
// что именно и чем кончилось — нет.
async function drawHist() {
  const d = await get("/api/tasks");
  if (!d || !d.ok) return;
  $("hist").innerHTML = d.rows.length ? d.rows.map((t) => {
    const spent = (t.updated_at && t.created_at)
      ? Math.max(0, t.updated_at - t.created_at) : 0;
    const dur = spent >= 60 ? `${Math.round(spent / 60)} мин` : `${spent} с`;
    return `<div class="h-row h-${t.status}">
      <span class="h-kind">${esc(KINDS[t.kind] || t.kind)}</span>
      <span class="h-when">${ruStamp(t.created_at)}</span>
      <span class="h-res">${esc(STATUS[t.status] || t.status)}${
        t.total ? ` · ${t.done} из ${t.total}` : ""} · ${dur}</span>
      ${t.message ? `<span class="h-msg">${esc(t.message)}</span>` : ""}
    </div>`;
  }).join("") : `<p class="nobody">Задач ещё не было.</p>`;
}

$("btn-hist").onclick = () => {
  const box = $("hist");
  box.hidden = !box.hidden;
  $("log").hidden = true;
  if (!box.hidden) drawHist();
};

// Кому звонить, когда на сегодня ничего не назначено.
//
// Главный экран при полной базе сообщал «ничего не назначено» и
// оставлял человека одного — дальше он шёл в базу и сортировал её
// глазами. Но кому звонить первым, программа знает: за это и считался
// балл. Показываем сразу с телефоном, чтобы между «открыл программу» и
// «набрал номер» не было ни одного лишнего шага.
function suggestBlock(rows) {
  if (!rows.length) {
    return `<div class="blank">
      <p><b>На сегодня ничего не назначено.</b></p>
      <p>И предложить некого: в базе нет компаний, до которых ещё не
         дошли руки и у которых есть чем связаться. Начните с поиска —
         вкладка «Поиск» слева.</p></div>`;
  }
  return `<p class="due-lede">На сегодня ничего не назначено. Программа
     предлагает начать с этих — у них наибольший балл среди тех, до кого
     ещё не дошли руки.</p>` + rows.map((r) => `
    <div class="due" data-open="${r.id}">
      ${scoreBadge(r.score)}
      <span class="due-main">
        <b>${esc(r.name)}</b>
        <i>${r.director ? esc(r.director) + " · " : ""}${esc(r.region || "")}</i>
      </span>
      <span class="due-tel">${r.phone
        ? `<button class="val copyable tel" data-copy="${esc(r.phone)}"
             title="Нажмите, чтобы скопировать">${esc(prettyPhone(r.phone))}</button>`
        : (r.email ? `<span class="val">${esc(r.email)}</span>` : "")}</span>
      <button class="btn sm" data-take="${r.id}">В работу</button>
    </div>`).join("");
}

// ── Сводка: цифры кликабельны и ставят фильтр ────────────
const STAT_FILTER = {lpr_found: "lpr_found", callcenter: "callcenter",
                     with_dir_mail: "director", ai_fit: "ai_fit"};

async function loadStats() {
  const s = await get("/api/stats");
  if (!s) return;
  $("stats").innerHTML = [
    ["companies", s.companies, "компаний"],
    ["with_director", s.with_director, "известен ГД"],
    ["lpr_found", s.lpr_found, "контакт ГД найден"],
    ["callcenter", s.callcenter, "продают по телефону"],
    ["ai_fit", s.ai_fit, "ИИ: подходят"],
  ].filter(([key]) => !(window.SCORE_MODE === "generic" && key === "callcenter")).map(([key, n, t]) => {
    const f = STAT_FILTER[key];
    return `<button class="stat${f ? " is-link" : ""}"${f ? ` data-only="${f}"` : ""}>
      <b>${n}</b><span>${t}</span></button>`;
  }).join("");
  $("stats").querySelectorAll(".stat.is-link").forEach((el) => {
    el.onclick = () => { showView("base"); setFilter(el.dataset.only); };
  });
}

// ── Фильтры ──────────────────────────────────────────────
let only = "";
function setFilter(value) {
  only = value || "";
  shownRows = PAGE_ROWS;
  document.querySelectorAll(".chip").forEach(
    (c) => c.classList.toggle("is-active", c.dataset.only === only));
  loadCompanies();
}
document.querySelectorAll(".chip").forEach((c) => {
  c.onclick = () => setFilter(c.dataset.only);
});

// ── Иконки соцсетей ──────────────────────────────────────
// Подписи словами занимали полстроки. Значок короче и узнаётся быстрее,
// а название остаётся в подсказке.
const NET_ICON = {
  vk: '<path d="M2 5.5h3.1c.3 3.4 1.8 5.3 2.9 5.6V5.5h2.9v4.4c1.1-.1 2.2-1.4 2.6-4.4H16c-.3 2.1-1.2 3.6-2.1 4.2 1 .5 2.1 1.8 2.5 4.3h-3c-.3-1.6-1.2-2.9-2.5-3v3H8.5C5 14 2.5 10.6 2 5.5z"/>',
  telegram: '<path d="M16.6 3.4 2.4 8.9c-.8.3-.8.9-.1 1.1l3.5 1.1 1.4 4.2c.2.5.4.6.8.2l1.9-1.6 3.6 2.7c.7.4 1.1.2 1.3-.6l2.3-10.7c.2-.9-.3-1.3-1-1z"/>',
  tenchat: '<path d="M4 1.5h10A2.5 2.5 0 0 1 16.5 4v10a2.5 2.5 0 0 1-2.5 2.5H4A2.5 2.5 0 0 1 1.5 14V4A2.5 2.5 0 0 1 4 1.5zm.8 3.2v2.1h2.9v6.5h2.4V6.8h2.9V4.7H4.8z"/>',
  instagram: '<path d="M6 2h6a4 4 0 0 1 4 4v6a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4zm0 1.8A2.2 2.2 0 0 0 3.8 6v6A2.2 2.2 0 0 0 6 14.2h6A2.2 2.2 0 0 0 14.2 12V6A2.2 2.2 0 0 0 12 3.8H6zM9 5.6A3.4 3.4 0 1 1 9 12.4 3.4 3.4 0 0 1 9 5.6zm0 1.8a1.6 1.6 0 1 0 0 3.2 1.6 1.6 0 0 0 0-3.2zm3.7-2.3a.9.9 0 1 1 0 1.8.9.9 0 0 1 0-1.8z"/>',
  whatsapp: '<path d="M9 2a7 7 0 0 0-6 10.6L2 17l4.5-1a7 7 0 1 0 2.5-14zm3.9 9.6c-.2.5-1 .9-1.4 1-.4 0-.8.2-2.6-.6-2.2-1-3.6-3.3-3.7-3.5-.1-.2-.9-1.2-.9-2.3s.6-1.6.8-1.8c.2-.2.4-.3.6-.3h.4c.2 0 .4 0 .5.4l.7 1.7c.1.2 0 .4-.1.5l-.3.4c-.1.1-.2.2-.1.4.1.2.5.9 1.1 1.4.8.7 1.4.9 1.6 1 .2.1.3.1.4-.1l.6-.7c.1-.2.3-.2.5-.1l1.6.8c.2.1.4.2.4.3v.8z"/>',
  ok: '<path d="M9 2a3.4 3.4 0 1 0 0 6.8A3.4 3.4 0 0 0 9 2zm0 1.9a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3zM5 9.6c-.4.5-.3 1.2.3 1.5 0 0 1 .6 2.2.8l-2 2c-.4.4-.4 1 0 1.4.4.4 1 .4 1.4 0L9 13.3l2.1 2c.4.4 1 .4 1.4 0 .4-.4.4-1 0-1.4l-2-2c1.2-.2 2.2-.8 2.2-.8.6-.3.7-1 .3-1.5-.3-.4-.9-.5-1.4-.2 0 0-1 .6-2.6.6s-2.6-.6-2.6-.6c-.5-.3-1.1-.2-1.4.2z"/>',
  youtube: '<path d="M17 5.4a2 2 0 0 0-1.4-1.4C14.3 3.6 9 3.6 9 3.6s-5.3 0-6.6.4A2 2 0 0 0 1 5.4C.6 6.7.6 9 .6 9s0 2.3.4 3.6A2 2 0 0 0 2.4 14c1.3.4 6.6.4 6.6.4s5.3 0 6.6-.4a2 2 0 0 0 1.4-1.4c.4-1.3.4-3.6.4-3.6s0-2.3-.4-3.6zM7.3 11.5v-5L11.6 9l-4.3 2.5z"/>',
  dzen: '<path d="M9 1c.3 4.2 1.3 6.4 3.4 7 2.1.6 4 .7 5.6.6-.2 4.2-1.2 6.5-3.3 7.1-2.1.6-4 .7-5.7.6-.3-4.2-1.3-6.4-3.4-7-2.1-.6-4-.7-5.6-.6.2-4.2 1.2-6.5 3.3-7.1C5.4.9 7.3.8 9 1z"/>',
  rutube: '<path d="M2 3h11a3 3 0 0 1 0 6H2V3zm2 2v2h8.5a1 1 0 0 0 0-2H4zM2 11h2v4H2v-4zm9.5 0h2.3l2.2 4h-2.4l-2.1-4z"/>',
};

function netIcon(net) {
  const path = NET_ICON[net];
  if (!path) return "";
  return `<svg class="ni" viewBox="0 0 18 18" fill="currentColor" aria-hidden="true">${path}</svg>`;
}

const HOST_NET = {"vk.com": "vk", "t.me": "telegram", "tenchat.ru": "tenchat",
                  "instagram.com": "instagram", "wa.me": "whatsapp",
                  "ok.ru": "ok", "youtube.com": "youtube", "dzen.ru": "dzen",
                  "rutube.ru": "rutube"};
const NET_NAME = {vk: "ВКонтакте", telegram: "Telegram", tenchat: "TenChat",
                  instagram: "Instagram", whatsapp: "WhatsApp",
                  ok: "Одноклассники", youtube: "YouTube", dzen: "Дзен",
                  rutube: "Rutube"};

// Короткая подпись сети по адресу. В строке таблицы места на
// «ВКонтакте» нет, а «ВК» читается так же однозначно.
const NET_SHORT = [[/vk\.com/i, "ВК"], [/t\.me|telegram/i, "TG"],
                   [/tenchat/i, "TenChat"], [/ok\.ru/i, "ОК"],
                   [/youtube/i, "YouTube"], [/dzen/i, "Дзен"],
                   [/rutube/i, "Rutube"], [/instagram/i, "Inst"],
                   [/wa\.me|whatsapp/i, "WhatsApp"]];
function netName(url) {
  for (const [rx, name] of NET_SHORT) if (rx.test(url || "")) return name;
  return "ссылка";
}

// ── Контакты ─────────────────────────────────────────────
function contactRow(c) {
  const v = esc(c.value);
  // Показываем в удобочитаемом виде, а копируем как лежит: в чужую CRM
  // номер вставляют цифрами, и пробелы там только мешают.
  const shown = c.kind === "phone" ? esc(prettyPhone(c.value)) : v;
  if (c.kind === "social") {
    const host = (c.value.split("/")[2] || "").replace("www.", "");
    const net = HOST_NET[host] || "";
    const lpr = c.owner === "director";
    const tail = c.value.split("/").slice(3).join("/") || host;
    return `<div class="ct ${lpr ? "lpr" : ""}">
      <span class="who">${lpr ? "ГД" : ""}</span>
      <a class="val net" href="${safeUrl(c.value)}" target="_blank"
         title="${esc(NET_NAME[net] || host)}">${netIcon(net)}${esc(tail)}</a>
      ${lpr ? `<span class="mk ok">найден</span>` : ""}</div>`;
  }
  if (c.kind !== "email") {
    const lpr = c.owner === "director";
    const kind = c.kind === "phone" ? phoneKind(c.value) : "";
    // Номер, на который заведён Telegram, — это другой разговор: туда
    // пишут, а не звонят, и читает написанное чаще сам владелец.
    // «Нет» здесь значит «не нашёлся»: закрытый профиль не находится.
    // Только у телефона: на строке «@имя» отметка «есть TG» повторяет
    // саму строку и занимает место, которого в карточке и так нет.
    let tgMk = "";
    if (c.kind !== "phone") tgMk = "";
    else if (c.verified === "tg")
      tgMk = `<span class="mk tg" title="На этот номер заведён Telegram">есть TG</span>`;
    else if (c.verified === "no_tg")
      tgMk = `<span class="mk cold" title="Проверяли — аккаунт не нашёлся. `
           + `Закрытый профиль не находится по номеру">TG не нашёлся</span>`;
    return `<div class="ct ${lpr ? "lpr" : ""}">
      <span class="who">${lpr ? "ГД тел" : (c.kind === "phone" ? "тел" : "tg")}</span>
      <button class="val copyable${c.kind === "phone" ? " tel" : ""}"
              data-copy="${v}">${shown}</button>
      ${lpr ? `<span class="mk ok">найден</span>` : ""}${tgMk}
      ${ctFrom(c, kind)}</div>`;
  }
  // Найденный адрес и выведенный по схеме — вещи разной надёжности, и это
  // должно читаться с первого взгляда.
  const guess = /схеме|догадк/i.test(c.source || "");
  let mk = "";
  if (c.verified === "ok") mk = `<span class="mk ok">живой</span>`;
  else if (c.verified === "catch_all") mk = `<span class="mk ca">домен ловит всё</span>`;
  else if (guess) mk = `<span class="mk guess">догадка ${c.confidence}%</span>`;
  else if (c.owner === "director") mk = `<span class="mk ok">найден</span>`;
  const lpr = c.owner === "director";
  return `<div class="ct ${lpr ? "lpr" : ""} ${guess ? "is-guess" : ""}">
    <span class="who">${lpr ? "ГД" : esc(OWNER_RU[c.owner] || "")}</span>
    <button class="val copyable" data-copy="${v}">${v}</button>${mk}
    ${ctFrom(c, "")}</div>`;
}

// Список контактов: по группам и без простыни.
//
// У агентства недвижимости на сайте висит по номеру на каждого
// сотрудника — сорок штук, и все они вываливались в карточку подряд.
// Найти среди них тот, с которого стоит начать, было нельзя: они
// отличались только цифрами.
//
// Поэтому три вещи. Первая: наверх то, что ведёт к решающему —
// контакты руководителя, потом мобильные, потом всё остальное. Вторая:
// у каждого написано, какой он и откуда. Третья: показываем пять,
// остальные под кнопкой — они никуда не делись, но и не мешают.
const CT_SHOWN = 5;

function contactsBlock(list, id) {
  if (!list.length) return "<p class='nobody'>Ни телефона, ни почты не нашлось.</p>";
  const rank = (c) => {
    if (c.owner === "director") return 0;
    if (c.kind === "phone" && phoneKind(c.value) === "мобильный") return 1;
    if (c.kind === "email") return 2;
    if (c.kind === "phone" && phoneKind(c.value) === "бесплатный") return 4;
    return 3;
  };
  const sorted = list.slice().sort((a, b) => rank(a) - rank(b));
  const head = sorted.slice(0, CT_SHOWN);
  const tail = sorted.slice(CT_SHOWN);
  const count = (n, kind) => {
    const k = tail.filter((c) => c.kind === kind).length;
    return k ? `${k} ${kind === "phone"
      ? "номер" + plural(k, "", "а", "ов")
      : "почт" + plural(k, "а", "ы", "")}` : "";
  };
  return head.map(contactRow).join("") + (tail.length ? `
    <details class="ct-more">
      <summary>Ещё ${[count(0, "phone"), count(0, "email")]
        .filter(Boolean).join(" и ") || tail.length + " контактов"}</summary>
      <div class="ct-rest">${tail.map(contactRow).join("")}</div>
    </details>` : "");
}

// Кому принадлежит контакт — по-русски и коротко. В базе это английские
// слова, и «general» в строке рядом с номером не объясняет ничего.
const OWNER_RU = {
  director: "ГД", sales: "продажи", support: "поддержка",
  hr: "кадры", person: "сотрудник", general: "общий", unknown: "",
};

// Номер для чтения глазами и для диктовки вслух.
//
// В базе он лежит одной цепочкой цифр — так его вернул разбор, и так
// его проще сравнивать. Но читать «+79161234567» человек не должен:
// пока найдёшь границу кода, забудешь начало, а продиктовать по
// телефону такое не выйдет вовсе.
function prettyPhone(v) {
  const d = String(v || "").replace(/\D/g, "");
  if (d.length !== 11 || (d[0] !== "7" && d[0] !== "8")) return v;
  return "+7 %s %s-%s-%s".replace("%s", d.slice(1, 4))
    .replace("%s", d.slice(4, 7)).replace("%s", d.slice(7, 9))
    .replace("%s", d.slice(9, 11));
}

// Мобильный, городской или бесплатный.
//
// Разница не косметическая, и это единственное, что программа может
// сказать о принадлежности номера, не выдумывая: мобильный — чей-то
// личный аппарат, и отвечает на него человек, а не приёмная. У компании
// с сорока номерами именно это и решает, с какого начинать.
function phoneKind(v) {
  const d = String(v || "").replace(/\D/g, "");
  if (d.length !== 11) return "";
  const code = d.slice(1, 4);
  if (code === "800") return "бесплатный";
  if (code[0] === "9") return "мобильный";
  return "городской";
}

// Откуда контакт взялся. «Рядом с ФИО на сайте» и «из справочника» —
// это разная надёжность, и человек должен видеть разницу до звонка.
function ctFrom(c, kind) {
  const bits = [];
  if (kind) bits.push(kind);
  const src = (c.source || "").trim();
  if (src) bits.push(src.replace(/^сайт компании$/, "с сайта"));
  if (!bits.length) return "";
  return `<span class="ct-from">${esc(bits.join(" · "))}</span>`;
}

const CC_CLASS = {"да": "yes", "вероятно": "maybe"};

function callCell(r) {
  const label = r.callcenter || "нет данных";
  const why = (r.signals || {}).cc_why || "";
  return `<div class="cc">
    <span class="cc-label ${CC_CLASS[label] || "no"}">${esc(label)}</span>
    ${why ? `<span class="cc-why">${esc(why)}</span>` : ""}</div>`;
}

function signalChips(sig) {
  const out = [];
  if (sig.hh_vacancies) out.push([`вакансий ${esc(sig.hh_vacancies)}`, true]);
  // Свежесть вакансии — срок годности повода для звонка. «Вчера искали
  // третьего продавца» работает, «месяц назад» — уже нет.
  if (sig.hh_fresh_days !== undefined && sig.hh_fresh_days !== "") {
    const d = Number(sig.hh_fresh_days);
    if (!isNaN(d)) out.push([d <= 1 ? "вакансия сегодня" :
      d <= 7 ? `вакансия ${d} дн. назад` : `вакансии ${d} дн.`, d <= 7]);
  }
  if (sig.tech_calltracking) out.push([esc(sig.tech_calltracking), true]);
  for (const k of ["tech_crm", "tech_telephony", "gis_rubric"])
    if (sig[k]) out.push([esc(sig[k]), false]);
  if (sig.size) out.push([esc(sig.size), ["малый", "средний"].includes(sig.size)]);
  if (sig.revenue) out.push([money(sig.revenue), false]);
  if (sig.zakupki_person) out.push(["в закупках", true]);
  if (sig.found_by) out.push([`найдено по «${esc(sig.found_by)}»`, false]);
  return out.map(([t, hot]) => `<span class="sig ${hot ? "hot" : ""}">${t}</span>`).join("");
}

function notesHtml(list) {
  if (!list.length) return `<p class="nobody">Пока пусто.</p>`;
  return list.map((n) => `<div class="note-row">
      <span class="note-when">${ruStamp(n.created_at)}</span>
      <span class="note-text">${esc(n.text)}</span>
      <button class="note-x" data-drop="${n.id}" title="Удалить">×</button>
    </div>`).join("");
}

// ── Карточка компании ────────────────────────────────────
// Какая карточка открыта. Нужно помнить между перерисовками: пока идёт
// задача, таблица обновляется каждые полторы секунды, и без этого
// карточка захлопывалась сама собой прямо под руками.
let openId = null;

function closeCard(forget) {
  document.querySelectorAll("tr.card-row").forEach((r) => r.remove());
  document.querySelectorAll("tr.row.is-open").forEach((r) => r.classList.remove("is-open"));
  if (forget !== false) openId = null;
}

function fact(value, label, cls) {
  return value ? `<div class="fact"><b class="${cls || ""}">${esc(value)}</b>
                  <span>${label}</span></div>` : "";
}

// Деньги в том порядке величины, в каком их называют вслух.
//
// Всё считалось в миллионах, и выручка крупной компании выглядела как
// «172000.0 млн ₽»: чтобы понять, что это сто семьдесят два миллиарда,
// надо было считать нули глазами. А именно у крупных компаний эта
// цифра и решает, звонить ли вообще.
function money(v) {
  const n = Number(v) || 0;
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1) + " млрд ₽";
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + " млн ₽";
  if (a >= 1e3) return Math.round(n / 1e3) + " тыс ₽";
  return n + " ₽";
}
// Заметка хранит время числом, а не строкой: по нему они и
// упорядочиваются.
const ruStamp = (ts) => {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

const ruDate = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
  return m ? `${m[3]}.${m[2]}.${m[1]}` : (iso || "");
};

// Столбики выручки по годам. Не график ради графика: по одной цифре не
// видно, растёт компания или доедает прошлые контракты, а это решает,
// будет ли разговор про развитие или про экономию.
function revenueBars(series, growth) {
  if (!series || series.length < 2) return "";
  const rows = series.slice().reverse();          // старые слева
  const max = Math.max(...rows.map((r) => r.revenue)) || 1;
  // Последний год окрашен по направлению: синий столбик при падающей
  // выручке читается как достижение, чем он не является.
  const tone = /спад/.test(growth || "") ? " is-down"
             : /рост/.test(growth || "") ? " is-up" : "";
  return `<div class="rbars${tone}">${rows.map((r) => `
    <div class="rbar" title="${r.year}: ${money(r.revenue)}">
      <em>${money(r.revenue)}</em>
      <span class="col"><i style="height:${Math.max(5, Math.round(100 * r.revenue / max))}%"></i></span>
      <u>${r.year}</u>
    </div>`).join("")}</div>`;
}

function parseSeries(raw) {
  return (raw || "").split(";").filter(Boolean).map((p) => {
    const [year, rev] = p.split(":");
    return {year: year, revenue: parseInt(rev, 10) || 0};
  });
}

const GROWTH_CLASS = (g) => /рост/.test(g) ? "good" : /спад/.test(g) ? "bad" : "";

// Из чего сложился балл.
//
// Число без разбора человек либо принимает на веру, либо не верит ему
// вовсе, и оба исхода одинаково бесполезны. Показываем и засчитанное, и
// незасчитанное: второе объясняет балл не хуже первого, а заодно
// говорит, чем его поднять — и это уже список дел, а не жалоба.
function scoreBlock(c, parts, now) {
  if (!parts.length) return "";
  const got = parts.filter((p) => p.got);
  const miss = parts.filter((p) => !p.got && p.points >= 5);
  const row = (p, plus) => `<li class="${plus ? "got" : "miss"}"
      title="${esc(p.why || "")}">
      <b>${plus ? "+" : ""}${p.points}</b><span>${esc(p.text)}</span></li>`;
  // Считаем по слагаемым, а не берём число из таблицы. В таблице лежит
  // балл с последнего обогащения, и если правила с тех пор поменялись,
  // он разойдётся с разбором, который стоит прямо под ним.
  const total = now != null ? now : Math.min(100,
    got.reduce((a, p) => a + p.points, 0));
  const could = Math.min(miss.reduce((a, p) => a + p.points, 0), 100 - total);
  const stale = c.score != null && c.score !== total;
  // Свёрнут по умолчанию. Разбор — справка: её читают один раз, когда
  // не верят числу, а места он занимал больше, чем контакты и заметки
  // вместе. Сводка в заголовке отвечает на вопрос сразу.
  return `<details class="score-block">
    <summary>
      <span class="sb-h">Из чего сложился балл</span>
      <b class="score-total">${total}</b>
      <span class="sb-sum">${got.length} признак${plural(got.length, "", "а", "ов")}${
        could ? ` · можно ещё +${could}` : ""}</span>
    </summary>
    ${stale ? `<p class="score-stale">В таблице ${c.score} — балл с
      прошлого обогащения. Обновится, когда компанию обогатят снова.</p>` : ""}
    <ul class="score-list">${got.map((p) => row(p, true)).join("")}</ul>
    ${miss.length ? `<p class="score-could">Не засчитано — это ещё
      ${could} баллов, если появится:</p>
      <ul class="score-list">${miss.map((p) => row(p, false)).join("")}</ul>` : ""}
    <p class="score-note">Балл нужен не для точности, а для порядка
      обзвона: список проходят сверху вниз и до середины обычно не
      доходят. Наведите на строку — объяснение, почему она считается.</p>
  </details>`;
}

// Окончание по числу: «3 признака», но «5 признаков».
function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

// Всё сгенерированное помечено и лежит отдельным блоком: смешать его с
// разобранными фактами значит потерять возможность отличить одно от другого.
function aiBlock(c, sig) {
  if (!c.ai_summary && c.ai_fit == null && !c.ai_hook) return "";
  const fit = c.ai_fit;
  const cls = fit >= 60 ? "hi" : fit >= 35 ? "mid" : "";
  return `<div class="ai-block">
    <h4>Разбор <span class="ai-mark">ИИ</span></h4>
    ${fit != null ? `<div class="ai-fit ${cls}"><b>${fit}</b>
      <span>${esc(c.ai_why || "соответствие вашему описанию клиента")}</span></div>` : ""}
    ${c.ai_summary ? `<p>${esc(c.ai_summary)}${c.ai_segment ? ` \u00b7 ${esc(c.ai_segment)}` : ""}</p>` : ""}
    ${sig.ai_signals ? `<p><b>Зацепки:</b> ${esc(sig.ai_signals)}</p>` : ""}
    ${c.ai_hook ? `<p><b>С чего начать:</b> ${esc(c.ai_hook)}</p>` : ""}
    ${c.ai_opener ? `<div class="ai-opener copyable" data-copy="${esc(c.ai_opener)}"
        title="Нажмите, чтобы скопировать">${esc(c.ai_opener)}</div>` : ""}
  </div>`;
}

// Риски — то, из-за чего звонок окажется потраченным впустую. Лучше
// увидеть это до разговора, чем узнать в нём.
// Что известно о компании из реестров.
//
// Раньше этого блока не было вовсе: статус, год регистрации, уставный
// капитал, число учредителей и филиалов лежали в базе, но на глаза не
// попадались, а численность и выручка прятались в «Финансах» и
// исчезали вместе с ним, когда выручки не нашлось. Карточка крупной
// компании выглядела так, будто о ней не известно ничего.
//
// Пустой блок тоже говорит — но не «данных нет», а почему их нет. Это
// разные сообщения: в первом случае человек думает, что компания
// такая, во втором понимает, что надо запустить обогащение.
function aboutBlock(c, sig) {
  const bad = c.status && c.status !== "ACTIVE";
  const extra = String(c.okveds_extra || "").split(";")
    .map((x) => x.trim()).filter(Boolean).length;
  const facts = [
    fact(c.status ? (c.status === "ACTIVE" ? "действующая" : c.status) : "",
         "статус в ЕГРЮЛ", bad ? "bad" : "ok"),
    fact(c.founded, "в ЕГРЮЛ с"),
    fact(c.capital ? money(+c.capital) : "", "уставный капитал"),
    fact(c.employees, "сотрудников по ФНС"),
    fact(sig.self_staff, "сотрудников по сайту"),
    fact(c.founders_count, "учредителей"),
    fact(c.branches, "филиалов"),
  ].filter(Boolean).join("");

  const ids = [
    c.inn ? `<span class="copyable" data-copy="${esc(c.inn)}"
      title="Нажмите, чтобы скопировать">ИНН ${esc(c.inn)}</span>` : "",
    c.ogrn ? `<span class="copyable" data-copy="${esc(c.ogrn)}"
      title="Нажмите, чтобы скопировать">ОГРН ${esc(c.ogrn)}</span>` : "",
  ].filter(Boolean).join("");

  let blank = "";
  if (!c.inn) {
    // Самая частая причина пустой карточки, и она поправима.
    blank = `<p class="nobody">ИНН не известен — без него ни ЕГРЮЛ, ни ФНС
      не спросить. Так бывает, когда в справочнике стоит вывеска
      («Fesco»), а в реестре компания записана иначе (ПАО «ДВМП»).
      Запустите обогащение: программа прочитает реквизиты в подвале
      сайта и переспросит реестры по номеру.</p>`;
  } else if (!facts && !c.director) {
    blank = `<p class="nobody">ИНН есть, но в реестрах ещё не спрашивали.
      Запустите обогащение — придут руководитель, статус, год, капитал и
      выручка.</p>`;
  }

  return `<section class="cs">
    <h4>О компании</h4>
    ${ids ? `<div class="ids">${ids}</div>` : ""}
    ${c.director ? `<p class="cs-boss"><b>${esc(c.director)}</b>${
      c.director_post ? ` · ${esc(c.director_post)}` : ""}</p>` : ""}
    ${c.okved_name || c.okved ? `<p class="small">${
      esc(c.okved_name || "")}${c.okved ? ` <span class="dim">(ОКВЭД ${
      esc(c.okved)})</span>` : ""}${extra ? ` <span class="dim">и ещё ${
      extra} ${plural(extra, "вид", "вида", "видов")}</span>` : ""}</p>` : ""}
    ${c.address ? `<p class="small">${esc(c.address)}</p>` : ""}
    ${facts ? `<div class="facts mt">${facts}</div>` : ""}
    ${blank}
  </section>`;
}

function risks(c, sig) {
  const out = [];
  if (c.status && c.status !== "ACTIVE")
    out.push(["Статус в ЕГРЮЛ: " + c.status, "bad"]);
  if ((c.growth || "").indexOf("спад") >= 0)
    out.push(["Выручка падает: " + c.growth, "warn"]);
  if (sig.size === "микро") out.push(["Микробизнес — бюджета может не быть", "warn"]);
  // Отсутствие отчётности — признак, только если её искали. Компания
  // без известного ИНН ни в чём не провинилась: её просто не о чем было
  // спросить, и ставить ей это в минус — значит врать в карточке.
  if (c.inn && !sig.revenue) out.push(["Отчётности в ФНС нет", "warn"]);
  if (c.founders_count > 3)
    out.push([`Учредителей ${c.founders_count} — решение согласовывают`, "warn"]);
  if (sig.last_post && sig.last_post < "2024-01-01")
    out.push(["Сайт не обновлялся с " + ruDate(sig.last_post), "warn"]);
  return out;
}

async function toggleCard(tr, id) {
  const wasOpen = tr.classList.contains("is-open");
  closeCard();                 // одна карточка за раз: две подряд уже не читаются
  if (wasOpen) return;

  openId = String(id);
  tr.classList.add("is-open");
  const holder = document.createElement("tr");
  holder.className = "card-row";
  holder.innerHTML = `<td class="card-cell" colspan="7"><div class="skeleton"></div></td>`;
  tr.after(holder);

  const d = await get("/api/company/" + id);
  // Пока ходили за карточкой, таблицу могли перерисовать — тогда holder
  // уже не в документе, и писать в него нечего.
  if (!d || !d.ok || !holder.isConnected) { holder.remove(); return; }
  const c = d.company, sig = d.signals || {};
  const why = (sig.cc_why || "").split(", ").filter(Boolean);
  const rev = sig.revenue ? money(sig.revenue) : "";
  const cts = d.contacts || [];
  const socials = cts.filter((x) => x.kind === "social");
  const rest = cts.filter((x) => x.kind !== "social");
  const links = [
    c.site ? link(c.site, "сайт") : "",
    sig.hh_url ? link(sig.hh_url, "на hh.ru") : "",
    sig.zakupki_url ? link(sig.zakupki_url, "в закупках") : "",
    c.inn ? `<a href="https://bo.nalog.ru/search?query=${esc(c.inn)}" target="_blank">отчётность</a>` : "",
  ].filter(Boolean).join("");

  holder.querySelector("td").innerHTML = `<div class="detail">
    <header class="card-top">
      ${scoreBadge(c.score, "big")}
      <div class="ct-id">
        <h3>${esc(c.name)}</h3>
        <div class="ct-meta">
          ${c.inn ? `<span class="copyable" data-copy="${esc(c.inn)}"
            title="Нажмите, чтобы скопировать">ИНН ${esc(c.inn)}</span>` : ""}
          ${c.ogrn ? `<span class="copyable" data-copy="${esc(c.ogrn)}"
            title="Нажмите, чтобы скопировать">ОГРН ${esc(c.ogrn)}</span>` : ""}
          ${c.site ? link(c.site,
            c.site.replace(/^https?:\/\//, "").replace(/\/$/, "")) : ""}
          ${c.region ? `<span>${esc(c.region)}</span>` : ""}
          ${c.okved_name ? `<span title="${esc(c.okved || "")}">${esc(c.okved_name)}</span>` : ""}
        </div>
        ${c.director ? `<div class="ct-boss"><b>${esc(c.director)}</b>${
          c.director_post ? ` · ${esc(c.director_post)}` : ""}</div>` : ""}
      </div>
      <div class="ct-right">
        <select class="stage" data-id="${c.id}">
          ${STAGES.map(([v, t]) =>
            `<option value="${v}" ${c.stage === v ? "selected" : ""}>${t}</option>`).join("")}
        </select>
      </div>
    </header>

    <div class="card-do">
      <button class="btn primary sm" data-analyze="${c.id}">Разобрать через ИИ</button>
      <button class="btn sm" data-tgcheck="${c.id}">Проверить в Telegram</button>
      <button class="btn sm" data-letter="${c.id}">Письмо</button>
      <button class="btn sm" data-kp="${c.id}">Коммерческое предложение</button>
      <span class="do-links">${links}</span>
    </div>

    <div class="card-grid">
      <div class="card-main">
        <section class="cs">
          <h4>Как связаться</h4>
          ${contactsBlock(rest, c.id)}
          ${socials.length ? `<div class="soc-row">${socials.map(contactRow).join("")}</div>`
            : `<p class="nobody sm">Соцсетей не нашлось. Программа берёт только
               те ссылки, которые компания опубликовала сама.</p>`}
          ${(d.search || []).length ? `<div class="hand-find">
            <span>${c.director
              ? "Найти страницы руководителя вручную:"
              : "ФИО неизвестно — поискать людей компании вручную:"}</span>
            ${d.search.map((x) => link(x.url, x.title)).join("")}
            <em>Программа по этим ссылкам не ходит и ничего не сохраняет:
              решение и проверка — за вами. В TenChat человек сам
              указывает, где работает, поэтому поиск по компании находит
              тех, кто это о себе заявил.</em></div>` : ""}
        </section>

        <section class="cs">
          <h4>Что дальше</h4>
          <div class="next">
            <input type="text" class="next-step" data-id="${c.id}"
                   placeholder="позвонить, отправить письмо…"
                   value="${esc(c.next_step || "")}">
            <input type="date" class="next-date" data-id="${c.id}"
                   value="${esc(c.next_date || "")}">
          </div>
        </section>

        <div class="letter" data-letter-box="${c.id}" hidden></div>
        <div class="letter" data-kp-box="${c.id}"${c.ai_kp ? "" : " hidden"}>${
          c.ai_kp ? `<div class="kp"><p class="kp-title"><b>Составленное КП</b>
            <span class="ai-mark">ИИ</span></p><pre class="letter-body">${
            esc(c.ai_kp)}</pre></div>
          <div class="detail-links">
            <button class="btn sm" data-copy-saved-kp>Скопировать целиком</button>
          </div>` : ""}</div>

        <section class="cs">
          <h4>Заметки</h4>
          <div class="notes" data-notes="${c.id}">${notesHtml(d.notes || [])}</div>
          <textarea class="note-new" data-note="${c.id}" rows="2"
                    placeholder="что сказали, о чём договорились — Ctrl+Enter"></textarea>
        </section>
      </div>

      <aside class="card-side">
        ${aiBlock(c, sig)}

        ${aboutBlock(c, sig)}

        <section class="cs">
          <h4>Чем занимается</h4>
          <p class="cs-text">${c.activity ? esc(c.activity)
            : "<span class='nobody'>Описания не нашлось — сайт не открылся или его там нет.</span>"}</p>
          <div class="cc-line ${CC_CLASS[c.callcenter] || "no"}">
            <b>Телефонные продажи:</b> ${esc(c.callcenter || "нет данных")}</div>
          ${why.length ? `<ul class="why-list">${why.map(
            (w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
          ${sig.sales_model ? `<p class="small">Модель продаж: ${esc(sig.sales_model)}</p>` : ""}
          ${c.address ? `<p class="small">${esc(c.address)}</p>` : ""}
        </section>

        <section class="cs">
          <h4>Финансы${c.growth ? ` <span class="${GROWTH_CLASS(c.growth)}">${
            esc(c.growth)}</span>` : ""}</h4>
          ${revenueBars(parseSeries(sig.revenue_series), c.growth) ||
            `<p class="nobody">${rev ? "Данные за один год: " + rev
              : (c.inn ? "Отчётности в ФНС не нашлось."
                       : "ИНН не известен — в ФНС не спрашивали.")}</p>`}
          <div class="facts mt">
            ${fact(sig.profit ? money(+sig.profit) : "", "прибыль",
                   +sig.profit < 0 ? "bad" : "")}
            ${fact(sig.size, "размер")}
            ${fact(sig.hh_salary, "зарплаты в вакансиях")}
          </div>
        </section>

        ${(() => { const r = risks(c, sig); return r.length ? `
        <section class="cs risks">
          <h4>На что обратить внимание</h4>
          <ul class="risk-list">${r.map(
            ([t, k]) => `<li class="${k}">${esc(t)}</li>`).join("")}</ul>
        </section>` : ""; })()}

        ${scoreBlock(c, d.score_parts || [], d.score_now)}

        <div class="card-foot">
          <a href="#" class="danger-link" data-del="${c.id}">Удалить и больше не показывать</a>
        </div>
      </aside>
    </div>
  </div>`;
  bindCopy(holder);
  bindWork(holder, c);
  const del = holder.querySelector("[data-del]");
  if (del) del.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!confirm(`Удалить «${c.name}»? Повторный поиск её не вернёт.`)) return;
    await post("/api/company/" + c.id + "/delete", {blacklist: true});
    toast("Удалено");
    loadCompanies(); loadStats();
  };
}

// Работа с компанией: следующий шаг, заметки, письмо.
//
// Всё сохраняется само, без кнопки «сохранить»: человек в этот момент
// держит трубку, и лишнее нажатие он просто не сделает.
function bindWork(root, c) {
  const step = root.querySelector(".next-step");
  const date = root.querySelector(".next-date");
  const save = async () => {
    await post("/api/company/" + c.id, {
      next_step: step.value.trim(), next_date: date.value});
    c.next_step = step.value.trim();
    c.next_date = date.value;
    loadStats();
  };
  step.onchange = save;
  date.onchange = save;

  const box = root.querySelector(".notes");
  const field = root.querySelector(".note-new");

  function bindDrops() {
    box.querySelectorAll("[data-drop]").forEach((b) => {
      b.onclick = async (e) => {
        e.stopPropagation();
        const d = await post("/api/company/" + c.id + "/note",
                             {delete: Number(b.dataset.drop)});
        if (d && d.ok) { box.innerHTML = notesHtml(d.notes); bindDrops(); }
      };
    });
  }
  bindDrops();

  field.onkeydown = async (e) => {
    if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
    const text = field.value.trim();
    if (!text) return;
    field.value = "";
    const d = await post("/api/company/" + c.id + "/note", {text});
    if (d && d.ok) { box.innerHTML = notesHtml(d.notes); bindDrops(); toast("Записано"); }
    else toast((d && d.error) || "не записалось");
  };

  const letterBtn = root.querySelector("[data-letter]");
  const letterBox = root.querySelector("[data-letter-box]");
  letterBtn.onclick = async (e) => {
    e.stopPropagation();
    letterBtn.disabled = true;
    letterBtn.textContent = "пишу…";
    letterBox.hidden = false;
    letterBox.innerHTML = `<div class="skeleton" style="height:90px"></div>`;
    const d = await post("/api/company/" + c.id + "/letter", {});
    letterBtn.disabled = false;
    letterBtn.textContent = "Письмо через ИИ";
    if (!d || !d.ok) {
      letterBox.innerHTML = `<p class="bad">${esc((d && d.error) || "не вышло")}</p>`;
      return;
    }
    // Письмо не отправляется отсюда и не сохраняется: это черновик,
    // который человек прочитает и поправит под себя.
    letterBox.innerHTML = `
      <p class="letter-subj"><b>Тема:</b> ${esc(d.subject)}</p>
      <pre class="letter-body">${esc(d.body)}</pre>
      <div class="detail-links">
        <button class="btn sm" data-copy-letter>Скопировать</button>
        ${d.to ? `<a class="btn sm" href="mailto:${esc(d.to)}?subject=${
            encodeURIComponent(d.subject)}&body=${
            encodeURIComponent(d.body)}">Открыть в почте (${esc(d.to)})</a>` : ""}
      </div>`;
    letterBox.querySelector("[data-copy-letter]").onclick = (ev) => {
      ev.stopPropagation();
      copy(d.subject + "\n\n" + d.body);
    };
  };

  // Телефоны этой компании — сейчас. Их два-три, дневной предел от
  // них не страдает, а ответ нужен до звонка, а не после общего
  // прогона. Городские здесь спрашиваются тоже: человек попросил про
  // эту компанию, а не про всю базу.
  const tgBtn = root.querySelector("[data-tgcheck]");
  if (tgBtn) tgBtn.onclick = async (e) => {
    e.stopPropagation();
    const back = tgBtn.textContent;
    tgBtn.disabled = true;
    tgBtn.textContent = "спрашиваю…";
    const d = await post("/api/company/" + c.id + "/tg", {});
    tgBtn.disabled = false;
    tgBtn.textContent = back;
    if (!d || !d.ok) { toast((d && d.error) || "не вышло"); return; }
    // Отметки живут прямо на строках контактов — перерисовываем их, а
    // не всю карточку: открытые «показать ещё» не должны схлопнуться.
    const fresh = await get("/api/company/" + c.id);
    if (fresh && fresh.ok) {
      const rest2 = (fresh.contacts || []).filter((x) => x.kind !== "social");
      const cs = root.querySelector(".cs");
      if (cs) cs.innerHTML = `<h4>Как связаться</h4>${contactsBlock(rest2, c.id)}`;
    }
    toast(d.found
      ? `Telegram есть у ${d.found} из ${d.checked}`
      : `Проверено ${d.checked} — ни одного не нашлось`);
    if (d.stopped) toast(d.stopped);
  };

  // Разбор одной компании сейчас. Общий прогон идёт по тридцати
  // карточкам и занимает минуты; когда открыта одна и звонить по ней
  // надо сегодня, ждать незачем.
  const anBtn = root.querySelector("[data-analyze]");
  anBtn.onclick = async (e) => {
    e.stopPropagation();
    anBtn.disabled = true;
    anBtn.textContent = "разбираю…";
    const d = await post("/api/company/" + c.id + "/analyze", {});
    anBtn.disabled = false;
    anBtn.textContent = "Разобрать компанию";
    if (!d || !d.ok) {
      toast((d && d.error) || "не вышло");
      return;
    }
    // Разбор ложится в те же поля, что и при общем прогоне, и
    // показывать его отдельно значит завести второе место для одного и
    // того же. Поэтому обновляем существующий блок, а если его не было
    // (компанию разбирают впервые) — вставляем перед «Что дальше».
    const fresh = await get("/api/company/" + c.id);
    if (fresh && fresh.ok) {
      const html = aiBlock(fresh.company, fresh.signals || {});
      const old_ = root.querySelector(".ai-block");
      if (old_) {
        old_.outerHTML = html;
      } else if (html) {
        anBtn.closest(".detail-links").insertAdjacentHTML("beforebegin", html);
      }
      bindCopy(root);
    }
    loadCompanies();
  };

  const kpBtn = root.querySelector("[data-kp]");
  const kpBox = root.querySelector("[data-kp-box]");
  // Уже составленное КП показано сразу: составлять его заново — лишний
  // запрос к модели за то, что уже лежит в базе.
  const savedKp = root.querySelector("[data-copy-saved-kp]");
  if (savedKp) {
    savedKp.onclick = (ev) => {
      ev.stopPropagation();
      copy(c.ai_kp || "");
    };
    kpBtn.textContent = "Составить КП заново";
  }
  kpBtn.onclick = async (e) => {
    e.stopPropagation();
    kpBtn.disabled = true;
    kpBtn.textContent = "составляю…";
    kpBox.hidden = false;
    kpBox.innerHTML = `<div class="skeleton" style="height:140px"></div>`;
    const d = await post("/api/company/" + c.id + "/kp", {});
    kpBtn.disabled = false;
    kpBtn.textContent = "Коммерческое предложение";
    if (!d || !d.ok) {
      kpBox.innerHTML = `<p class="bad">${esc((d && d.error) || "не вышло")}</p>`;
      return;
    }
    const block = (head, val) => val
      ? `<h5>${esc(head)}</h5><p>${esc(val)}</p>` : "";
    kpBox.innerHTML = `
      <div class="kp">
        <p class="kp-title"><b>${esc(d.title || "Коммерческое предложение")}</b>
          <span class="ai-mark">ИИ</span></p>
        ${d.intro ? `<p>${esc(d.intro)}</p>` : ""}
        ${block("Задача", d.problem)}
        ${block("Что предлагаем", d.solution)}
        ${block("Условия", d.terms)}
        ${block("Следующий шаг", d.next)}
        ${(d.doubts || []).length ? `<h5>О чём спросят</h5><ul>${
          d.doubts.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      </div>
      <div class="detail-links">
        <button class="btn sm" data-copy-kp>Скопировать целиком</button>
      </div>`;
    kpBox.querySelector("[data-copy-kp]").onclick = (ev) => {
      ev.stopPropagation();
      copy(d.text);
    };
  };
}

function bindCopy(root) {
  root.querySelectorAll(".copyable").forEach((el) => {
    el.onclick = (e) => { e.stopPropagation(); copy(el.dataset.copy); };
  });
}

// ── Таблица ──────────────────────────────────────────────
const picked = new Set();

function syncBulk() {
  const box = $("bulk");
  box.hidden = picked.size === 0;
  $("bulk-count").textContent = picked.size
    ? `выбрано ${picked.size}` : "";
  document.querySelectorAll("tbody .pick-one").forEach(
    (el) => { el.checked = picked.has(el.dataset.id); });
  fixExport();
}

async function bulk(body, note) {
  const d = await post("/api/bulk", Object.assign({ids: [...picked]}, body));
  if (!d.ok) { toast(d.error || "не вышло"); return; }
  toast(`${note}: ${d.count}`);
  picked.clear();
  loadCompanies(); loadStats();
}

$("bulk-none").onclick = () => { picked.clear(); syncBulk(); loadCompanies(); };
$("bulk-stage").onchange = (e) => {
  if (!e.target.value) return;
  const stage = e.target.value;
  const label = (STAGES.find(([v]) => v === stage) || [stage, stage])[1];
  e.target.value = "";
  bulk({action: "stage", stage}, `Стадия «${label}»`);
};
$("bulk-black").onclick = () => {
  if (!confirm(`Больше не показывать эти компании (${picked.size})? ` +
               "Они останутся в базе, но повторный поиск их не вернёт.")) return;
  bulk({action: "blacklist"}, "В чёрном списке");
};
$("bulk-del").onclick = () => {
  if (!confirm(`Удалить ${picked.size} компаний из базы? ` +
               "Заодно добавлю их в чёрный список, чтобы не вернулись.")) return;
  bulk({action: "delete", blacklist: true}, "Удалено");
};
$("pick-all").onclick = (e) => {
  document.querySelectorAll("tbody .pick-one").forEach((el) => {
    if (e.target.checked) picked.add(el.dataset.id); else picked.delete(el.dataset.id);
  });
  syncBulk();
};

let sortBy = "score", sortDir = -1;
document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.onclick = () => {
    if (sortBy === th.dataset.sort) sortDir = -sortDir;
    else { sortBy = th.dataset.sort; sortDir = th.dataset.sort === "name" ? 1 : -1; }
    loadCompanies();
  };
});

const CC_ORDER = {"да": 3, "вероятно": 2, "слабые признаки": 1};

function sortRows(rows) {
  const key = (r) => {
    if (sortBy === "name") return (r.name || "").toLowerCase();
    if (sortBy === "callcenter") return CC_ORDER[r.callcenter] || 0;
    if (sortBy === "ai_fit") return r.ai_fit == null ? -1 : r.ai_fit;
    if (sortBy === "director") return r.director ? 1 : 0;
    return r.score || 0;
  };
  return rows.slice().sort((a, b) => {
    const x = key(a), y = key(b);
    if (x < y) return -sortDir;
    if (x > y) return sortDir;
    return (b.score || 0) - (a.score || 0);
  });
}

// Выгрузка отдаёт то же, что на экране. Иначе человек отбирает двадцать
// подходящих компаний, жмёт «Excel» и получает всю базу — и фильтрует
// второй раз, уже в Excel.
function exportWhat() {
  return picked.size ? {ids: [...picked].join(",")} : {q: $("q").value, only};
}

function fixExport() {
  const n = picked.size;
  $("exp-xlsx").textContent = n ? `Excel (${n})` : "Excel";
  $("exp-xlsx").title = n ? `Сохранить ${n} отмеченных`
    : (($("q").value || only) ? "Сохранить то, что сейчас в списке"
                             : "Сохранить всю базу");
}

// Выгрузка — это сохранение файла, а не скачивание. Окно программы не
// браузер: ссылка на файл в нём приводила к «Windows не удаётся найти».
async function saveExport(fmt) {
  const btn = $(fmt === "csv" ? "exp-csv" : "exp-xlsx");
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = "сохраняю…";
  const d = await post("/api/save", Object.assign({fmt}, exportWhat()));
  btn.disabled = false; btn.textContent = was;
  if (!d || !d.ok) { toast((d && d.error) || "не сохранилось"); return; }
  toast(`Сохранено: ${d.name} — ${d.count} компаний${d.opened ? "" : " (папка: " + d.folder + ")"}`);
}
$("exp-xlsx").onclick = () => saveExport("xlsx");
$("exp-csv").onclick = () => saveExport("csv");

// Сколько строк рисуем за раз.
//
// Не ограничение выборки, а ограничение отрисовки: пятьсот строк со
// всеми контактами — это десятки тысяч узлов, и собирает их браузер
// внутри окна программы, в том же потоке, который окно рисует. На
// четырёхстах компаниях окно переставало отвечать — не Python был
// виноват, а вот это.
const PAGE_ROWS = 100;
let shownRows = PAGE_ROWS;
let lastSignature = "";

async function loadCompanies(force) {
  const d = await get("/api/companies?" + new URLSearchParams(
    {q: $("q").value, only, limit: shownRows}));
  if (!d) return;
  fixExport();
  // Если список не изменился, перерисовывать его незачем. Во время
  // работы задачи это главный источник тормозов: данные те же, а
  // браузер собирает таблицу заново.
  const sig = d.rows.map((r) => r.id + ":" + r.score + ":" + r.stage).join(",");
  if (!force && sig === lastSignature && $("tbody").children.length) return;
  lastSignature = sig;
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.toggle("is-sorted", th.dataset.sort === sortBy);
    th.dataset.dir = sortDir > 0 ? "up" : "down";
  });
  // Счётчик нужен только когда что-то отфильтровано: «6» рядом с
  // цифрой «6 компаний» в шапке — повтор, который ничего не добавляет.
  $("shown").textContent = d.shown < d.total ? `${d.shown} из ${d.total}` : "";

  const tb = $("tbody");
  if (!d.rows.length) {
    const empty = $("q").value || only
      ? `<b>Ничего не подошло</b> Снимите фильтр или измените запрос.`
      : `<b>Пока пусто</b> Начните с источника — вакансии hh.ru работают без ключей.
         <br>Если поиск уже запускали и он ничего не нашёл, откройте
         «Источники» и нажмите «Проверить источники»: там будет видно,
         что именно не отвечает.`;
    tb.innerHTML = `<tr><td colspan="7" class="empty">${empty}</td></tr>`;
    return;
  }
  const rows = sortRows(d.rows).slice(0, shownRows);
  tb.innerHTML = rows.map((r) => {
    // Балл — тот же значок, что в воронке и на «Сегодня». Одинаковая
    // вещь должна выглядеть одинаково везде, иначе её каждый раз
    // приходится узнавать заново.
    const host = r.site ? r.site.replace(/^https?:\/\//, "") : "";
    const meta = [r.inn ? `<span title="ИНН">${esc(r.inn)}</span>` : "",
                  (!r.inn && r.ogrn) ? `<span title="ОГРН">${esc(r.ogrn)}</span>` : "",
                  host ? link(r.site, host) : "",
                  r.region ? `<span>${esc(r.region)}</span>` : ""].filter(Boolean).join("");
    // Порядок: найденный контакт ГД, потом выведенный по схеме, потом всё
    // остальное. По сырой уверенности info@ с сайта обгонял бы оба.
    const rank = (c) => {
      if (c.owner !== "director") return 2;
      return (c.source || "").indexOf("схеме") >= 0 ? 1 : 0;
    };
    // В строке — три контакта, остальное в карточке. Пять телефонов
    // подряд растягивают строку вдвое, а звонят всё равно по первому:
    // список просматривают глазами сверху вниз, и высота строки решает,
    // сколько компаний видно разом.
    const all = (r.contacts || []).filter((x) => x.kind !== "social")
      .sort((a, b) => rank(a) - rank(b));
    // Сколько их всего — с сервера: в ответ приходит только начало
    // списка, а «и ещё 106» должно говорить правду.
    const hidden = Math.max(0, (r.contacts_total != null
      ? r.contacts_total : all.length) - 3 - (r.contacts || []).filter(
        (x) => x.kind === "social").length);
    const cts = all.slice(0, 3).map(contactRow).join("")
      + (hidden > 0 ? `<div class="ct c-more">и ещё ${hidden}</div>` : "");
    // Соцсети — отдельной строкой под названием, а не в общей очереди
    // из трёх контактов: там их всегда вытесняют телефоны, и найденная
    // группа компании остаётся невидимой до открытия карточки.
    const soc = (r.contacts || []).filter((x) => x.kind === "social");
    const socLine = soc.length
      ? `<div class="soc-line">${soc.slice(0, 4).map((x) =>
          `<a class="soc" href="${safeUrl(x.value)}" target="_blank"
              title="${esc(x.source || "")}">${esc(netName(x.value))}</a>`).join("")}${
          soc.length > 4 ? `<span class="soc more">+${soc.length - 4}</span>` : ""}</div>`
      : "";
    return `<tr class="row" data-id="${r.id}">
      <td class="c-pick"><input type="checkbox" class="pick-one" data-id="${r.id}"
        ${picked.has(String(r.id)) ? "checked" : ""}></td>
      <td>${scoreBadge(r.score)}</td>
      <td><div class="co">${esc(r.name)}</div><div class="co-meta">${meta}</div>${socLine}</td>
      <td>${r.director ? esc(r.director) : `<span class="nobody">—</span>`}
          <div class="co-meta">${esc(r.director_post || "")}</div></td>
      <td>${cts || `<span class="nobody">—</span>`}</td>
      <td>${callCell(r)}${signalChips(r.signals || {})}</td>
      <td><select class="stage" data-id="${r.id}">
        ${STAGES.map(([v, t]) =>
          `<option value="${v}" ${r.stage === v ? "selected" : ""}>${t}</option>`).join("")}
      </select></td>
    </tr>`;
  }).join("");

  tb.querySelectorAll(".stage").forEach((el) => {
    el.onchange = () => post("/api/company/" + el.dataset.id, {stage: el.value});
    el.onclick = (e) => e.stopPropagation();   // иначе раскрылась бы карточка
  });
  bindCopy(tb);
  tb.querySelectorAll(".pick-one").forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      if (el.checked) picked.add(el.dataset.id); else picked.delete(el.dataset.id);
      syncBulk();
    };
  });
  syncBulk();
  tb.querySelectorAll("tr.row").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.closest("a, select, button, input")) return;
      toggleCard(tr, tr.dataset.id);
    };
  });

  // Кнопка «показать ещё». Пятьсот компаний разом не читают, а рисовать
  // их браузер устаёт.
  const more = $("more-rows");
  const left = (d.shown || rows.length) - rows.length;
  if (left > 0) {
    more.hidden = false;
    more.textContent = `Показать ещё ${Math.min(PAGE_ROWS, left)} `
      + `(осталось ${left})`;
  } else {
    more.hidden = true;
  }

  // Восстановить открытую карточку после перерисовки.
  if (openId) {
    const tr = tb.querySelector(`tr.row[data-id="${openId}"]`);
    if (tr) { const id = openId; openId = null; toggleCard(tr, id); }
    else openId = null;
  }
}

$("more-rows").onclick = () => {
  shownRows += PAGE_ROWS;
  loadCompanies(true);
};

let timer;
$("q").oninput = () => {
  clearTimeout(timer);
  // Новый запрос — снова с первой сотни.
  shownRows = PAGE_ROWS;
  timer = setTimeout(() => loadCompanies(true), 280);
};



// Esc закрывает то, что открыто поверх: сначала настройки, потом
// карточку. Мышью до крестика тянуться каждый раз — лишнее движение.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (openId) {
    const tr = document.querySelector(`tr.row[data-id="${openId}"]`);
    if (tr) toggleCard(tr, openId);
  }
});

// Ctrl+Enter из поля запросов запускает поиск: руки уже на клавиатуре.
$("f-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) $("btn-search").click();
});

// Любая внешняя ссылка уходит в системный браузер. Внутри окна
// программы нет ни адресной строки, ни кнопки «назад»: открытый в нём
// чужой сайт — это тупик, из которого выход один — закрыть программу.
document.addEventListener("click", async (e) => {
  const a = e.target.closest && e.target.closest('a[href^="http"]');
  if (!a) return;
  e.preventDefault();
  const d = await post("/api/open", {url: a.href});
  if (d && d.ok) return;
  // Браузер не открылся. Тупик вместо ссылки — это хуже, чем ссылка,
  // поэтому кладём адрес в буфер: вставить его человек сможет сам.
  await copy(a.href);
  toast("Браузер не открылся — адрес скопирован, вставьте его сами");
});


// ── Рассылки ─────────────────────────────────────────────
// Письма уходят сами, по одному, в рабочие часы. Экран отвечает на
// три вопроса: идёт ли отправка (и если нет — почему), что в каждой
// кампании, и кто ответил.
function mailFields() {
  return {
    mail_address: $("s-mail-address").value,
    mail_password: $("s-mail-password").value,
    mail_name: $("s-mail-name").value,
    mail_sign: $("s-mail-sign").value,
    mail_smtp_host: $("s-mail-smtp-host").value,
    mail_smtp_port: $("s-mail-smtp-port").value,
    mail_imap_host: $("s-mail-imap-host").value,
    mail_imap_port: $("s-mail-imap-port").value,
    mail_day_limit: $("s-mail-limit").value,
    mail_hour_from: $("s-mail-from").value,
    mail_hour_to: $("s-mail-to").value,
    mail_warmup: $("s-mail-warmup").checked ? "1" : "0",
    mail_weekends: $("s-mail-weekends").checked ? "1" : "0",
  };
}

// Серверы подставляются по домену — показываем, какие именно, прямо в
// полях: «подставится сама» ничего не говорит, пока не видно, что.
function mailHints() {
  const dom = ($("s-mail-address").value.split("@")[1] || "").trim().toLowerCase();
  const p = (window.MAIL_PRESETS || {})[dom];
  $("s-mail-smtp-host").placeholder = p ? p.smtp.split(":")[0] : "smtp.вашдомен.ru";
  $("s-mail-imap-host").placeholder = p ? p.imap.split(":")[0] : "imap.вашдомен.ru";
}
$("s-mail-address").addEventListener("input", mailHints);
mailHints();

async function mailCheck(sendTest) {
  const out = $("mail-check-out");
  out.innerHTML = `<p class="hint-sm">проверяю — до полуминуты на каждый сервер…</p>`;
  await post("/api/settings", mailFields());
  markKeys();
  const d = await post("/api/mail/check", {send_test: sendTest});
  if (!d || !d.steps) { out.innerHTML = `<p class="bad">не вышло</p>`; return; }
  out.innerHTML = d.steps.map((x) => `
    <div class="mc-row ${x.ok ? "good" : "bad"}">
      <b>${x.ok ? "✓" : "✕"} ${esc(x.what)}</b><span>${esc(x.text)}</span>
    </div>`).join("");
}
$("btn-mail-check").onclick = () => mailCheck(false);
$("btn-mail-test").onclick = () => mailCheck(true);

let camps = [], campMeta = {placeholders: [], defaults: [], status_ru: {}};
let campOpen = null, campRowsFilter = "";

function ago(sec) {
  if (sec == null) return "ещё не проверяли";
  if (sec < 60) return "только что";
  if (sec < 3600) return Math.round(sec / 60) + " мин назад";
  return Math.round(sec / 3600) + " ч назад";
}

function whenTs(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const dd = d.toLocaleDateString("ru-RU", {day: "numeric", month: "short"});
  const tt = d.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
  return `${dd}, ${tt}`;
}

async function loadMailState() {
  const st = await get("/api/mail/state");
  if (!st || !st.ok) return;
  const nav = $("n-mail");
  nav.hidden = !st.answer_waiting;
  nav.textContent = st.answer_waiting;
  let head, tone = "";
  if (!st.configured) {
    head = `Ящик не настроен — ${esc(st.problem)}.
      <button class="btn sm primary" data-go="settings">Настроить почту</button>`;
    tone = "warn";
  } else if (st.last_error) {
    head = `Отправка приостановлена: ${esc(st.last_error)}`;
    tone = "bad";
  } else if (!st.queued) {
    head = "Очередь пуста. Добавьте компании в кампанию: «База» → отметьте → «В рассылку…».";
  } else if (st.sent_today >= st.day_limit) {
    head = `На сегодня всё: отправлено ${st.sent_today} из ${st.day_limit}. Продолжу завтра.`;
  } else if (!st.in_window) {
    head = `Сейчас не рабочее время (${esc(st.hours)}${st.weekends ? "" : ", по будням"}) — письма ждут.`;
  } else if (!st.due) {
    head = "Все письма на сегодня уже ушли, следующие шаги — по расписанию.";
  } else {
    head = `Идёт отправка: следующее письмо ${st.next_send_in
      ? "через " + Math.ceil(st.next_send_in / 60) + " мин" : "вот-вот"}.`;
    tone = "good";
  }
  $("mail-state").className = "mail-state " + tone;
  $("mail-state").innerHTML = `
    <div class="ms-head">${head}</div>
    <div class="ms-nums">
      <span><b>${st.sent_today}</b> из ${st.day_limit} сегодня</span>
      <span><b>${st.queued}</b> в очереди</span>
      <span><b>${st.replies_total}</b> ответили</span>
      <span>ответы: ${st.can_read ? ago(st.last_poll_ago) : "IMAP не настроен"}
        ${st.can_read ? `<button class="btn quiet sm" id="mail-poll">Проверить сейчас</button>` : ""}</span>
    </div>
    ${st.poll_error ? `<div class="ms-err">Ответы не читаются: ${esc(st.poll_error)}</div>` : ""}
    <div class="ms-foot">Письма уходят, пока программа открыта.</div>`;
  const pb = $("mail-poll");
  if (pb) pb.onclick = async () => {
    pb.disabled = true; pb.textContent = "проверяю…";
    const d = await post("/api/mail/poll", {});
    if (d && d.ok) {
      const f = d.found || {};
      toast(`Ответов: ${f.replied || 0}, отказов: ${f.unsub || 0}, возвратов: ${f.bounced || 0}`);
    } else toast((d && d.error) || "не вышло");
    loadMailState(); loadCampaigns();
  };
}

async function loadCampaigns() {
  const d = await get("/api/campaigns");
  if (!d || !d.ok) return;
  camps = d.campaigns;
  campMeta = d;
  fillBulkCamp();
  if (!camps.length) {
    $("camp-list").innerHTML = `<div class="blank">
      <p><b>Кампаний пока нет.</b></p>
      <p>Кампания — это первое письмо и до двух напоминаний. Создайте её,
        потом в «Базе» отметьте компании и выберите «В рассылку…».</p></div>`;
    return;
  }
  const n = (c, k) => (c.counts[k] || 0);
  $("camp-list").innerHTML = camps.map((c) => `
    <div class="camp ${c.status === "paused" ? "is-paused" : ""}">
      <div class="camp-main">
        <b>${esc(c.name)}</b>
        <i>${c.steps.length} ${c.steps.length === 1 ? "письмо" : "письма"}
          · ${c.status === "paused" ? "на паузе" : "идёт"}
          · всего компаний ${c.total}, писем ушло ${c.letters}</i>
      </div>
      <div class="camp-nums">
        <span title="ждут отправки">${n(c, "queued")}<em>в очереди</em></span>
        <span title="всё отправлено, ждём ответа">${n(c, "waiting")}<em>ждём</em></span>
        <span class="good">${n(c, "replied")}<em>ответили</em></span>
        <span>${n(c, "unsub")}<em>отказ</em></span>
        <span class="${n(c, "bounced") ? "bad" : ""}">${n(c, "bounced")}<em>возврат</em></span>
      </div>
      <div class="camp-do">
        <button class="btn sm" data-camp-rows="${c.id}">Компании</button>
        <button class="btn sm" data-camp-edit="${c.id}">Изменить</button>
        <button class="btn sm" data-camp-toggle="${c.id}">${
          c.status === "paused" ? "Продолжить" : "Пауза"}</button>
        <button class="btn quiet sm danger" data-camp-del="${c.id}">Удалить</button>
      </div>
    </div>`).join("");
  $("camp-list").querySelectorAll("[data-camp-edit]").forEach((b) => {
    b.onclick = () => editCampaign(camps.find((c) => c.id == b.dataset.campEdit));
  });
  $("camp-list").querySelectorAll("[data-camp-rows]").forEach((b) => {
    b.onclick = () => { campOpen = Number(b.dataset.campRows); campRowsFilter = ""; loadCampRows(); };
  });
  $("camp-list").querySelectorAll("[data-camp-toggle]").forEach((b) => {
    b.onclick = async () => {
      const c = camps.find((x) => x.id == b.dataset.campToggle);
      await post(`/api/campaigns/${c.id}/state`,
                 {status: c.status === "paused" ? "active" : "paused"});
      loadCampaigns(); loadMailState();
    };
  });
  $("camp-list").querySelectorAll("[data-camp-del]").forEach((b) => {
    b.onclick = async () => {
      const c = camps.find((x) => x.id == b.dataset.campDel);
      if (!confirm(`Удалить кампанию «${c.name}»? Неотправленные письма не уйдут. ` +
                   "Отправленные останутся в заметках компаний, ответы на них " +
                   "программа по-прежнему узнает.")) return;
      await post(`/api/campaigns/${c.id}/delete`, {});
      if (campOpen === c.id) $("camp-rows").hidden = true;
      loadCampaigns(); loadMailState();
    };
  });
}

function fillBulkCamp() {
  const sel = $("bulk-camp");
  const live = camps.filter((c) => c.status === "active");
  sel.innerHTML = `<option value="">В рассылку…</option>` + (live.length
    ? live.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")
    : `<option value="" disabled>сначала создайте кампанию в «Рассылках»</option>`);
}

$("bulk-camp").onchange = async (e) => {
  const id = e.target.value;
  e.target.value = "";
  if (!id) return;
  const d = await post("/api/bulk", {ids: [...picked], action: "campaign", campaign: Number(id)});
  if (!d || !d.ok) { toast((d && d.error) || "не вышло"); return; }
  toast("Рассылка: " + d.text);
  picked.clear();
  loadCompanies();
};

// Редактор. Поле, где стоял курсор, запоминается: нажатие на
// подстановку уводит фокус на кнопку, и без этого вставлять было бы некуда.
let lastField = null;
function stepHtml(st, i) {
  const on = i === 0 || !!st;
  st = st || (campMeta.defaults[i] || {delay_days: i === 1 ? 3 : 7, subject: "", body: "", mode: "template"});
  return `
    <fieldset class="step-box ${on ? "" : "is-off"}" data-step="${i}">
      <legend>
        ${i === 0 ? "<b>Первое письмо</b>" : `<label class="opt"><input type="checkbox"
          class="st-on" ${on ? "checked" : ""}> <b>Напоминание ${i}</b></label>`}
        ${i > 0 ? `<span class="st-delay">через
          <input type="number" class="st-days" min="1" max="60" value="${st.delay_days || 3}">
          дн. после предыдущего, если не ответили</span>` : ""}
      </legend>
      ${i === 0 ? `<label class="opt st-mode-l"><input type="checkbox" class="st-ai"
          ${st.mode === "ai" ? "checked" : ""}> Писать через ИИ — отдельное письмо
          под каждую компанию по её карточке (нужен ключ ИИ и «что продаём»
          в «ИИ-анализе»)</label>` : ""}
      <div class="st-fields">
        <label>Тема ${i > 0 ? `<em class="fld-hint">пусто — «Re:» к первому письму,
            чтобы напоминание легло в ту же переписку</em>` : ""}
          <input type="text" class="st-subj" maxlength="200" value="${esc(st.subject || "")}">
        </label>
        <label>Текст
          <textarea class="st-body" rows="${i === 0 ? 9 : 5}">${esc(st.body || "")}</textarea>
        </label>
      </div>
    </fieldset>`;
}

function syncStepBoxes() {
  document.querySelectorAll("#camp-steps .step-box").forEach((box) => {
    const on = box.dataset.step === "0" || box.querySelector(".st-on").checked;
    box.classList.toggle("is-off", !on);
    const ai = box.querySelector(".st-ai");
    box.querySelector(".st-fields").hidden = !!(ai && ai.checked);
  });
}

function editCampaign(c) {
  $("camp-edit").hidden = false;
  $("camp-edit").dataset.id = c ? c.id : "";
  $("camp-edit-title").textContent = c ? "Кампания «" + c.name + "»" : "Новая кампания";
  $("camp-name").value = c ? c.name : "";
  const steps = c ? c.steps : campMeta.defaults;
  $("camp-steps").innerHTML = [0, 1, 2].map((i) => stepHtml(steps[i], i)).join("");
  $("camp-state").textContent = "";
  $("camp-prev").innerHTML = "";
  $("camp-steps").querySelectorAll("input, textarea").forEach((el) => {
    el.addEventListener("focus", () => {
      if (el.matches(".st-subj, .st-body")) lastField = el;
    });
    el.addEventListener("change", syncStepBoxes);
  });
  syncStepBoxes();
  $("camp-edit").scrollIntoView({block: "start", behavior: "smooth"});
}

function readSteps() {
  const out = [];
  document.querySelectorAll("#camp-steps .step-box").forEach((box) => {
    const i = Number(box.dataset.step);
    if (i > 0 && !box.querySelector(".st-on").checked) return;
    const ai = box.querySelector(".st-ai");
    out.push({
      delay_days: i === 0 ? 0 : Number(box.querySelector(".st-days").value) || 3,
      mode: ai && ai.checked ? "ai" : "template",
      subject: box.querySelector(".st-subj").value,
      body: box.querySelector(".st-body").value,
    });
  });
  return out;
}

async function saveCampaign() {
  const id = $("camp-edit").dataset.id;
  const d = await post("/api/campaigns", {id: id ? Number(id) : null,
    name: $("camp-name").value, steps: readSteps()});
  if (!d || !d.ok) {
    $("camp-state").innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
    return null;
  }
  $("camp-edit").dataset.id = d.id;
  $("camp-state").innerHTML = `<span class="good">сохранено</span>`;
  loadCampaigns();
  return d.id;
}

$("camp-new").onclick = () => editCampaign(null);
$("camp-cancel").onclick = () => { $("camp-edit").hidden = true; };
$("camp-save").onclick = saveCampaign;
$("camp-preview").onclick = async () => {
  const id = await saveCampaign();
  if (!id) return;
  const d = await post(`/api/campaigns/${id}/preview`, {});
  if (!d || !d.ok) { $("camp-prev").innerHTML = `<p class="bad">${esc((d && d.error) || "не вышло")}</p>`; return; }
  if (!d.letters.length) {
    $("camp-prev").innerHTML = `<p class="hint-sm">В базе пока нет компаний с почтой — показать не на ком.</p>`;
    return;
  }
  $("camp-prev").innerHTML = `<h3 class="prev-h">Так письма увидят три компании из базы</h3>` +
    d.letters.map((x) => `
    <div class="prev-co">
      <p class="prev-to"><b>${esc(x.company)}</b> → ${x.email ? esc(x.email)
        : `<span class="bad">не отправится: ${esc(x.why)}</span>`}</p>
      ${x.letters.map((l) => `
        <div class="prev-l">
          <p class="prev-meta">Письмо ${l.step}${l.delay_days ? ` · через ${l.delay_days} дн.` : ""}</p>
          ${l.note ? `<p class="hint-sm">${esc(l.note)}</p>` : `
          <p class="letter-subj"><b>Тема:</b> ${esc(l.subject)}</p>
          <pre class="letter-body">${esc(l.body)}</pre>`}
        </div>`).join("")}
    </div>`).join("");
};

function renderPhChips() {
  $("ph-chips").innerHTML = (campMeta.placeholders || []).map(
    (p) => `<button type="button" class="chip sm" data-ph="${esc(p)}">{${esc(p)}}</button>`).join(" ");
  $("optout-line").textContent = window.MAIL_OPTOUT || "";
  $("ph-chips").querySelectorAll("[data-ph]").forEach((b) => {
    b.onclick = () => {
      const f = lastField;
      if (!f) { toast("Сначала поставьте курсор в тему или текст письма"); return; }
      const ins = "{" + b.dataset.ph + "}";
      const a = f.selectionStart ?? f.value.length, z = f.selectionEnd ?? a;
      f.value = f.value.slice(0, a) + ins + f.value.slice(z);
      f.focus();
      f.selectionStart = f.selectionEnd = a + ins.length;
    };
  });
}

async function loadCampRows() {
  if (!campOpen) return;
  const c = camps.find((x) => x.id === campOpen);
  $("camp-rows").hidden = false;
  $("camp-rows-title").textContent = c ? "Компании: " + c.name : "Компании";
  const ru = campMeta.status_ru || {};
  $("camp-rows-chips").innerHTML = [["", "все"]].concat(Object.entries(ru)).map(
    ([k, v]) => `<button class="chip ${campRowsFilter === k ? "is-active" : ""}"
       data-rf="${esc(k)}">${esc(v)}</button>`).join("");
  $("camp-rows-chips").querySelectorAll("[data-rf]").forEach((b) => {
    b.onclick = () => { campRowsFilter = b.dataset.rf; loadCampRows(); };
  });
  const d = await get(`/api/campaigns/${campOpen}/rows?status=${encodeURIComponent(campRowsFilter)}`);
  if (!d || !d.ok) return;
  const total = c ? c.steps.length : 0;
  $("camp-rows-list").innerHTML = d.rows.length ? `
    <table class="mini">
      <thead><tr><th>Компания</th><th>Адрес</th><th>Отправлено</th><th>Состояние</th>
        <th>Когда</th><th></th></tr></thead>
      <tbody>${d.rows.map((r) => `
        <tr>
          <td><a href="#" data-open-co="${r.company_id}">${esc(r.company)}</a></td>
          <td>${esc(r.email)}</td>
          <td>${Math.min(r.step, total)} из ${total}</td>
          <td class="st-${esc(r.status)}">${esc(r.status_ru)}${r.error
            ? `<em>${esc(r.error)}</em>` : ""}</td>
          <td>${r.status === "queued" ? "след. " + whenTs(r.next_at)
               : whenTs(r.sent_at)}</td>
          <td>${["queued", "waiting", "error"].includes(r.status)
            ? `<button class="btn quiet sm" data-stop-o="${r.id}">Остановить</button>` : ""}</td>
        </tr>`).join("")}</tbody>
    </table>` : `<div class="blank"><p>Здесь пусто.</p></div>`;
  $("camp-rows-list").querySelectorAll("[data-open-co]").forEach((a) => {
    a.onclick = (e) => { e.preventDefault(); openFromOtherView(Number(a.dataset.openCo)); };
  });
  $("camp-rows-list").querySelectorAll("[data-stop-o]").forEach((b) => {
    b.onclick = async () => {
      await post(`/api/outreach/${b.dataset.stopO}/stop`, {});
      loadCampRows(); loadCampaigns(); loadMailState();
    };
  });
}
$("camp-rows-close").onclick = () => { $("camp-rows").hidden = true; campOpen = null; };

async function loadMail() {
  await loadCampaigns();
  renderPhChips();
  loadMailState();
  if (campOpen) loadCampRows();
}

// Пока экран открыт, состояние отправки обновляется само: письма
// уходят раз в несколько минут, и «следующее через 3 мин» без
// обновления врёт уже через минуту.
setInterval(() => { if (view === "mail") { loadMailState(); } }, 20000);
// Список кампаний нужен и в «Базе» — для «В рассылку…», — а счётчик
// ответов в меню должен появляться, даже когда экран не открыт.
loadCampaigns();
loadMailState();
setInterval(() => { if (view !== "mail") loadMailState(); }, 60000);

// ── Проекты ──────────────────────────────────────────────
// Проект — один свой бизнес со своими лидами. Переключение меняет базу
// целиком, поэтому страница перезагружается: так ни один экран не
// останется показывать компании чужого проекта.
$("proj-cur").onclick = (e) => {
  e.stopPropagation();
  $("proj-menu").hidden = !$("proj-menu").hidden;
};
document.addEventListener("click", (e) => {
  if (!$("proj-menu").hidden && !e.target.closest("#proj")) $("proj-menu").hidden = true;
});
document.querySelectorAll("[data-proj]").forEach((b) => {
  b.onclick = async () => {
    if (Number(b.dataset.proj) === window.PROJECT_ID) { $("proj-menu").hidden = true; return; }
    const d = await post(`/api/projects/${b.dataset.proj}/activate`, {});
    if (d && d.ok) location.reload();
    else toast("Не переключилось");
  };
});

function wizOpen() {
  $("proj-menu").hidden = true;
  $("wiz").hidden = false;
  $("wiz-state").textContent = "";
  $("wiz-about").focus();
}
function wizClose() { $("wiz").hidden = true; }
$("proj-add").onclick = wizOpen;
$("wiz-close").onclick = wizClose;
$("wiz").addEventListener("click", (e) => { if (e.target === $("wiz")) wizClose(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("wiz").hidden) wizClose();
});
$("wiz-cities").querySelectorAll(".area").forEach((b) => {
  b.onclick = () => {
    const whole = b.dataset.city === window.WHOLE_RU;
    if (whole) $("wiz-cities").querySelectorAll(".area").forEach((x) => x.classList.remove("is-on"));
    else $("wiz-cities").querySelector(`[data-city="${window.WHOLE_RU}"]`)?.classList.remove("is-on");
    b.classList.toggle("is-on");
  };
});
const lines = (id) => $(id).value.split("\n").map((x) => x.trim()).filter(Boolean);

function wizShowStep2() {
  $("wiz-step2").hidden = false;
  if (!$("wiz-offer").value.trim()) $("wiz-offer").value = $("wiz-about").value.trim();
  if (!$("wiz-icp").value.trim()) $("wiz-icp").value = $("wiz-buyer").value.trim();
  $("wiz-step2").scrollIntoView({block: "start", behavior: "smooth"});
}

$("wiz-setup").onclick = async () => {
  const about = $("wiz-about").value.trim(), buyer = $("wiz-buyer").value.trim();
  if (!about || !buyer) { toast("Опишите свою компанию и покупателя"); return; }
  const btn = $("wiz-setup");
  btn.disabled = true; btn.textContent = "подбираю…";
  $("wiz-state").textContent = "ИИ думает — обычно до полуминуты";
  const d = await post("/api/projects/setup", {about, buyer});
  btn.disabled = false; btn.textContent = "Подобрать заново";
  if (!d || !d.ok) {
    $("wiz-state").innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
    wizShowStep2();
    return;
  }
  $("wiz-state").innerHTML = `<span class="good">готово — проверьте и поправьте ниже</span>`;
  $("wiz-name").value = d.name || $("wiz-name").value;
  $("wiz-find").value = (d.find || []).join("\n");
  $("wiz-vac").value = (d.vacancies || []).join("\n");
  $("wiz-icp").value = d.icp || "";
  $("wiz-offer").value = d.offer || "";
  $("wiz-mode").value = d.score_mode || "generic";
  $("wiz-note").innerHTML = [
    d.note ? `<p><b>Кого отсеивать:</b> ${esc(d.note)}</p>` : "",
    (d.okved || []).length ? `<p><b>ОКВЭД покупателей:</b> ${esc(d.okved.join("; "))}</p>` : "",
  ].join("");
  wizShowStep2();
};
$("wiz-manual").onclick = wizShowStep2;

$("wiz-create").onclick = async () => {
  const cities = [...$("wiz-cities").querySelectorAll(".area.is-on")].map((b) => b.dataset.city);
  const body = {
    name: $("wiz-name").value.trim(),
    about: $("wiz-about").value.trim(), buyer: $("wiz-buyer").value.trim(),
    terms: $("wiz-terms").value.trim(),
    find: lines("wiz-find"), vacancies: lines("wiz-vac"),
    icp: $("wiz-icp").value.trim(), offer: $("wiz-offer").value.trim(),
    score_mode: $("wiz-mode").value, cities, run: $("wiz-run").checked,
  };
  if (!body.name) { $("wiz-name").focus(); toast("Назовите проект"); return; }
  const btn = $("wiz-create");
  btn.disabled = true;
  const d = await post("/api/projects", body);
  btn.disabled = false;
  if (!d || !d.ok) {
    $("wiz-create-state").innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
    return;
  }
  location.reload();
};

// Карточка проекта в настройках.
$("pj-save").onclick = async () => {
  const d = await post(`/api/projects/${window.PROJECT_ID}`, {
    name: $("pj-name").value, about: $("pj-about").value,
    buyer: $("pj-buyer").value, score_mode: $("pj-mode").value});
  if (d && d.ok) location.reload();
  else $("pj-state").innerHTML = `<span class="bad">не сохранилось</span>`;
};
$("pj-run").onclick = async () => {
  const d = await post(`/api/projects/${window.PROJECT_ID}/run`, {});
  if (d && d.ok) { toast(`Поставлено в очередь: ${d.tasks}`); poll(); }
  else $("pj-state").innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
};
if ($("pj-del")) $("pj-del").onclick = async () => {
  if (!confirm(`Удалить проект «${$("pj-name").value}» вместе со всеми его компаниями, ` +
               "заметками и рассылками? Вернуть будет нельзя.")) return;
  const d = await post(`/api/projects/${window.PROJECT_ID}/delete`, {});
  if (d && d.ok) location.reload();
  else $("pj-state").innerHTML = `<span class="bad">${esc((d && d.error) || "не вышло")}</span>`;
};

loadStats();
// Через showView, а не напрямую: он же прячет то, что на этом экране
// лишнее. Иначе цифры в шапке дублируют плитки под ней.
showView("today");
poll();
setInterval(poll, 1500);
