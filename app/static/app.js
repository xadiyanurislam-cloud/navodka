// Голый JS намеренно: программа собирается в exe без сборщика фронтенда,
// а вся логика всё равно живёт на стороне Python.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s)
  .replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

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
                ["написали", "написали"], ["созвон", "созвон"],
                ["отказ", "отказ"]];

// ── Экраны ───────────────────────────────────────────────
const VIEWS = {
  today: ["Сегодня", "Кому звонить и что нового"],
  base: ["База", "Найденное и обогащённое"],
  board: ["Воронка", "Компании по стадиям работы"],
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
}

// ── Сегодня ──────────────────────────────────────────────
// Первый экран отвечает на единственный вопрос, с которым сюда
// приходят утром: кому звонить. Не «сколько всего компаний», а «кому
// звонить сегодня» — цифры без этого списка не помогают начать работу.
const STAGE_RU = {"new": "новые", "в работе": "в работе",
                  "написали": "написали", "созвон": "созвон", "отказ": "отказ"};

function scoreBadge(n) {
  const v = Number(n) || 0;
  const cls = v >= 60 ? "hot" : v >= 35 ? "warm" : "cold";
  return `<span class="badge ${cls}">${v}</span>`;
}

async function loadToday() {
  const d = await get("/api/today");
  if (!d || !d.ok) return;

  const st = d.stages || {};
  const total = Object.values(st).reduce((a, b) => a + b, 0);
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
    : `<div class="blank">
         <p><b>На сегодня ничего не назначено.</b></p>
         <p>Откройте компанию в базе и впишите в «Что дальше», когда к ней
            вернуться — она появится здесь в нужный день.</p>
       </div>`;

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

const today = () => new Date().toISOString().slice(0, 10);

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

function boardCard(r) {
  return `<article class="bcard" draggable="true" data-id="${r.id}">
    <div class="bcard-top">
      ${scoreBadge(r.score)}
      <b>${esc(r.name)}</b>
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
    cities: [...$("q-cities").selectedOptions].map((o) => o.value),
    pages: $("q-pages").value,
    limit: $("q-limit").value,
    osm: $("q-osm").checked,
    gis: $("q-gis").checked,
    yandex: $("q-yandex").checked,
    dadata: $("q-dadata").checked,
    hh: $("q-hh").checked,
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
    [...$("q-cities").options].forEach((o) => { o.selected = want.includes(o.value); });
  }
  if (p.pages) $("q-pages").value = String(p.pages);
  if (p.limit) $("q-limit").value = String(p.limit);
  const src = p.sources || {};
  $("q-osm").checked = src.osm !== false;
  $("q-gis").checked = src.gis !== false;
  $("q-yandex").checked = src.yandex !== false;
  $("q-dadata").checked = src.dadata !== false;
  $("q-hh").checked = src.hh !== false;
  $("q-skip-empty").checked = p.skip_empty !== false;
  $("q-then").checked = !!p.then_enrich;
  $("q-then-zak").checked = !!p.then_zakupki;
  $("q-then-ai").checked = !!p.then_ai;
}

// Сколько источников реально готово — видно до нажатия, а не после.
function findReady() {
  const f = findForm();
  const on = [f.osm && "OSM", f.gis && "2ГИС", f.yandex && "Яндекс",
              f.dadata && "ЕГРЮЛ", f.hh && "hh.ru"].filter(Boolean);
  const box = $("find-ready");
  box.textContent = on.length
    ? `ищем в: ${on.join(", ")}`
    : "ни один источник не выбран";
  box.classList.toggle("bad", !on.length);
}
["q-osm", "q-gis", "q-yandex", "q-dadata", "q-hh"].forEach((id) => { $(id).onchange = findReady; });
findReady();

$("btn-find").onclick = async () => {
  const f = findForm();
  if (!f.query) { $("q-text").focus(); toast("Впишите, кого ищем"); return; }
  if (!f.osm && !f.gis && !f.yandex && !f.dadata && !f.hh) {
    toast("Выберите хотя бы один источник"); return; }
  const d = await post("/api/find", f);
  if (!d.ok) { toast(d.error || "не вышло"); return; }
  toast(`Ищу «${f.query}» — ${f.cities.length || 1} город(ов)`);
  showView("base");
  poll();
};

$("q-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("btn-find").click();
});

fillFindForm(window.LAST_FIND);

// Условия поиска собираются в одном месте — их и запускают, и сохраняют,
// и восстанавливают при следующем открытии программы.
function searchForm() {
  return {
    queries: $("f-text").value.split("\n").map((x) => x.trim()).filter(Boolean),
    areas: [...$("f-area").selectedOptions].map((o) => o.value),
    period: $("f-period").value,
    pages: $("f-pages").value,
    in_title: $("f-title").checked,
    skip_agencies: $("f-noagency").checked,
    max_open: $("f-maxopen").value,
    then_enrich: $("f-then").checked,
    then_zakupki: $("f-then-zak").checked,
    then_ai: $("f-then-ai").checked,
  };
}

function fillSearchForm(p) {
  if (!p || !p.queries) return;
  $("f-text").value = (p.queries || []).join("\n");
  const areas = (p.areas || []).map(String);
  [...$("f-area").options].forEach((o) => { o.selected = areas.includes(o.value); });
  if (p.period) $("f-period").value = String(p.period);
  if (p.pages) $("f-pages").value = String(p.pages);
  $("f-title").checked = p.in_title !== false;
  $("f-noagency").checked = p.skip_agencies !== false;
  $("f-maxopen").value = String(p.max_open || 0);
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

$("btn-import").onclick = () => {
  const text = $("i-text").value.trim();
  if (!text) { toast("Список пуст"); return; }
  run("/api/import", {text}, "Импорт");
};

// Каталог тематик. Поле «Вид деятельности» — пустая строка, и человек,
// открывший программу впервые, не знает, что в неё писать: разница
// между «грузоперевозки» и «транспортная компания» решает, найдётся
// сотня компаний или три.
// Тема только переключает список, а ищется слово. Два ряда кнопок
// подряд читаются как один выбор, поэтому подпись к теме говорит «1.»,
// подпись к словам «2.», а выбранное слово подсвечено: иначе человек
// жмёт тему, видит в поле прежний запрос и считает это поломкой.
function markTrade() {
  const cur = $("q-text").value.trim().toLowerCase();
  let known = false;
  document.querySelectorAll(".trade-items .pick").forEach((el) => {
    const on = el.dataset.q.toLowerCase() === cur;
    el.classList.toggle("is-on", on);
    if (on) known = true;
  });
  const note = $("trade-note");
  if (cur && !known) {
    note.innerHTML = `Сейчас ищется <b>${esc($("q-text").value.trim())}</b> —
      своё слово, не из списка. Нажмите любое ниже, чтобы заменить.`;
    note.classList.add("warn-note");
  } else {
    note.textContent = "Бледные слова ищутся только по названию — у них нет "
      + "тега на карте, и компания, не назвавшая себя так, не найдётся";
    note.classList.remove("warn-note");
  }
}

$("trade-tabs").onclick = (e) => {
  const tab = e.target.closest(".trade-tab");
  if (!tab) return;
  document.querySelectorAll(".trade-tab").forEach(
    (t) => t.classList.toggle("is-on", t === tab));
  document.querySelectorAll(".trade-items").forEach(
    (box) => { box.hidden = box.dataset.group !== tab.dataset.group; });
  markTrade();
};
document.querySelectorAll(".trade-items").forEach((box) => {
  box.onclick = (e) => {
    const pick = e.target.closest(".pick");
    if (!pick) return;
    $("q-text").value = pick.dataset.q;
    markTrade();
    // Подставили — и сразу показали, что дальше: иначе человек жмёт
    // слово и ждёт, что поиск пойдёт сам.
    $("q-text").focus();
    toast("Вписано: " + pick.dataset.q + ". Проверьте города и жмите «Найти компании»");
  };
});
$("q-text").oninput = markTrade;
markTrade();

$("btn-socials").onclick = () => run("/api/socials",
  {limit: $("e-limit").value, only_empty: true}, "Поиск соцсетей");
$("btn-enrich").onclick = () => run("/api/enrich", {
  limit: $("e-limit").value, verify: $("e-verify").checked,
  fns: $("e-fns").checked, zakupki: $("e-zakupki").checked,
  vk: $("e-vk").checked,
  only_lpr: $("e-only-lpr").checked}, "Обогащение");

$("btn-ai").onclick = () => run("/api/ai", {
  icp: $("a-icp").value, offer: $("a-offer").value,
  terms: $("a-terms").value,
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
$("btn-log").onclick = () => {
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
$("s-save").onclick = async () => {
  await post("/api/settings", {
    dadata_token: $("s-dadata").value, gis_key: $("s-gis").value,
    ai_key: $("s-ai-key").value, ai_url: $("s-ai-url").value,
    ai_model: $("s-ai-model").value, ai_kind: $("s-ai-kind").value,
    proxy_url: $("s-proxy").value,
    hh_token: $("s-hh-token").value,
    yandex_key: $("s-yandex").value, vk_token: $("s-vk").value,
    update_repo: $("s-upd-repo").value, update_token: $("s-upd-token").value});
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
  for (const [input, tag] of [["s-gis", "tag-gis"], ["s-ai-key", "tag-ai"]]) {
    const ready = !!$(input).value.trim();
    const el = $(tag);
    el.classList.toggle("ready", ready);
    el.textContent = ready ? "ключ есть" : "нужен ключ";
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
      (direct ? ` <a class="hint" href="${esc(direct)}" target="_blank">скачать вручную</a>` : "");
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
               enrich: "Обогащение", ai: "ИИ-анализ"};
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
  if (!t) { box.hidden = true; return; }

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
    (t.message ? ` · ${t.message}` : "");
  $("btn-stop").hidden = !live;

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
  if (live && (t.done !== liveSeen.done || Date.now() - liveSeen.at > 5000)) {
    liveSeen = {done: t.done, at: Date.now()};
    loadCompanies(); loadStats();
  }
}
let liveSeen = {done: -1, at: 0};

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
  ].map(([key, n, t]) => {
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
  if (c.kind === "social") {
    const host = (c.value.split("/")[2] || "").replace("www.", "");
    const net = HOST_NET[host] || "";
    const lpr = c.owner === "director";
    const tail = c.value.split("/").slice(3).join("/") || host;
    return `<div class="ct ${lpr ? "lpr" : ""}">
      <span class="who">${lpr ? "ГД" : ""}</span>
      <a class="val net" href="${esc(c.value)}" target="_blank"
         title="${esc(NET_NAME[net] || host)}">${netIcon(net)}${esc(tail)}</a>
      ${lpr ? `<span class="mk ok">найден</span>` : ""}</div>`;
  }
  if (c.kind !== "email") {
    const lpr = c.owner === "director";
    return `<div class="ct ${lpr ? "lpr" : ""}">
      <span class="who">${lpr ? "ГД тел" : (c.kind === "phone" ? "тел" : "tg")}</span>
      <button class="val copyable" data-copy="${v}">${v}</button>
      ${lpr ? `<span class="mk ok">найден</span>` : ""}</div>`;
  }
  // Найденный адрес и выведенный по схеме — вещи разной надёжности, и это
  // должно читаться с первого взгляда.
  const guess = (c.source || "").indexOf("схеме") >= 0;
  let mk = "";
  if (c.verified === "ok") mk = `<span class="mk ok">живой</span>`;
  else if (c.verified === "catch_all") mk = `<span class="mk ca">домен ловит всё</span>`;
  else if (guess) mk = `<span class="mk guess">догадка ${c.confidence}%</span>`;
  else if (c.owner === "director") mk = `<span class="mk ok">найден</span>`;
  const lpr = c.owner === "director";
  return `<div class="ct ${lpr ? "lpr" : ""} ${guess ? "is-guess" : ""}">
    <span class="who">${lpr ? "ГД" : esc(c.owner === "unknown" ? "" : c.owner)}</span>
    <button class="val copyable" data-copy="${v}">${v}</button>${mk}</div>`;
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
  if (sig.revenue) out.push([(sig.revenue / 1e6).toFixed(1) + " млн ₽", false]);
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

const money = (v) => (v / 1e6).toFixed(v >= 1e8 ? 0 : 1) + " млн ₽";
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
  return `<div class="score-block">
    <h4>Из чего сложился балл <b class="score-total">${total}</b></h4>
    ${stale ? `<p class="score-stale">В таблице ${c.score} — балл с
      прошлого обогащения. Обновится, когда компанию обогатят снова.</p>` : ""}
    <ul class="score-list">${got.map((p) => row(p, true)).join("")}</ul>
    ${miss.length ? `<p class="score-could">Не засчитано — это ещё
      ${could} баллов, если появится:</p>
      <ul class="score-list">${miss.map((p) => row(p, false)).join("")}</ul>` : ""}
    <p class="score-note">Балл нужен не для точности, а для порядка
      обзвона: список проходят сверху вниз и до середины обычно не
      доходят. Наведите на строку — объяснение, почему она считается.</p>
  </div>`;
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
function risks(c, sig) {
  const out = [];
  if (c.status && c.status !== "ACTIVE")
    out.push(["Статус в ЕГРЮЛ: " + c.status, "bad"]);
  if ((c.growth || "").indexOf("спад") >= 0)
    out.push(["Выручка падает: " + c.growth, "warn"]);
  if (sig.size === "микро") out.push(["Микробизнес — бюджета может не быть", "warn"]);
  if (!sig.revenue) out.push(["Отчётности в ФНС нет", "warn"]);
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
  const rev = sig.revenue ? (sig.revenue / 1e6).toFixed(1) + " млн ₽" : "";
  const cts = d.contacts || [];
  const socials = cts.filter((x) => x.kind === "social");
  const rest = cts.filter((x) => x.kind !== "social");
  const links = [
    c.site ? `<a href="${esc(c.site)}" target="_blank">сайт</a>` : "",
    sig.hh_url ? `<a href="${esc(sig.hh_url)}" target="_blank">на hh.ru</a>` : "",
    sig.zakupki_url ? `<a href="${esc(sig.zakupki_url)}" target="_blank">в закупках</a>` : "",
    c.inn ? `<a href="https://bo.nalog.ru/search?query=${esc(c.inn)}" target="_blank">отчётность</a>` : "",
  ].filter(Boolean).join("");

  holder.querySelector("td").innerHTML = `<div class="detail">
    <section>
      <h4>Контакты</h4>
      ${rest.map(contactRow).join("") || "<span class='nobody'>—</span>"}
      <h4 class="mt">Соцсети</h4>
      ${socials.length ? socials.map(contactRow).join("")
        : `<p class="nobody">Не нашлось ни на сайте, ни в карточках справочников.
           ${sig.vk_group ? "" : "Сообщество ВК ищется по ссылке, которую компания опубликовала сама, — если её нигде нет, программа не угадывает."}</p>`}
      ${(d.search || []).length ? `
        <h4 class="mt">Найти руководителя вручную</h4>
        <p class="hint-sm">Программа сюда не ходит и ничего не сохраняет:
           по имени надёжно не найти, однофамильцев в любом городе сотни.</p>
        <div class="detail-links">${d.search.map(
          (x) => `<a href="${esc(x.url)}" target="_blank">${esc(x.title)}</a>`).join("")}</div>` : ""}
    </section>

    <section>
      <h4>Чем занимается</h4>
      <p>${c.activity ? esc(c.activity)
          : "<span class='nobody'>Описание не найдено — сайт не открылся или описания на нём нет.</span>"}</p>
      <h4 class="mt">Телефонные продажи — ${esc(c.callcenter || "нет данных")}</h4>
      ${why.length
        ? `<ul class="why-list">${why.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`
        : "<p class='nobody'>Признаков не нашлось. Скорее всего, звонки для компании не основной канал.</p>"}

      <h4 class="mt">Реквизиты и структура</h4>
      <div class="facts">
        ${fact(c.founded, "в ЕГРЮЛ с")}
        ${fact(sig.self_year, "по сайту работает с")}
        ${fact(c.branches, "филиалов по ЕГРЮЛ")}
        ${fact(sig.self_branches, "точек по сайту")}
        ${fact(c.founders_count, "учредителей")}
        ${fact(sig.hh_open_all, "вакансий всего")}
        ${fact(c.cms, "движок сайта")}
        ${fact(ruDate(sig.last_post), "последняя публикация")}
      </div>
      ${c.founders ? `<p class="small"><b>Учредители:</b> ${esc(c.founders)}</p>` : ""}
      <h4 class="mt">Что дальше</h4>
      <div class="next">
        <input type="text" class="next-step" data-id="${c.id}"
               placeholder="позвонить, отправить письмо…"
               value="${esc(c.next_step || "")}">
        <input type="date" class="next-date" data-id="${c.id}"
               value="${esc(c.next_date || "")}">
      </div>
      <div class="detail-links">
        <button class="btn sm" data-analyze="${c.id}">Разобрать компанию</button>
        <button class="btn sm" data-letter="${c.id}">Письмо через ИИ</button>
        <button class="btn sm" data-kp="${c.id}">Коммерческое предложение</button>
      </div>
      <div class="letter" data-letter-box="${c.id}" hidden></div>
      <div class="letter" data-kp-box="${c.id}"${c.ai_kp ? "" : " hidden"}>${
        c.ai_kp ? `<div class="kp"><p class="kp-title"><b>Составленное КП</b>
          <span class="ai-mark">ИИ</span></p><pre class="letter-body">${
          esc(c.ai_kp)}</pre></div>
        <div class="detail-links">
          <button class="btn sm" data-copy-saved-kp>Скопировать целиком</button>
        </div>` : ""}</div>

      <h4 class="mt">Заметки</h4>
      <div class="notes" data-notes="${c.id}">${notesHtml(d.notes || [])}</div>
      <textarea class="note-new" data-note="${c.id}" rows="2"
                placeholder="что сказали, о чём договорились — Ctrl+Enter"></textarea>

      ${sig.sales_model ? `<p class="small"><b>Модель продаж:</b> ${esc(sig.sales_model)}</p>` : ""}
      ${c.okved_name ? `<p class="small"><b>ОКВЭД:</b> ${esc(c.okved)} ${esc(c.okved_name)}</p>` : ""}
      ${c.okveds_extra ? `<p class="small">Также: ${esc(c.okveds_extra)}</p>` : ""}
      ${c.address ? `<p class="small">${esc(c.address)}</p>` : ""}
    </section>

    <section>
      <h4>Финансы${c.growth ? ` — <span class="${GROWTH_CLASS(c.growth)}">${esc(c.growth)}</span>` : ""}</h4>
      ${revenueBars(parseSeries(sig.revenue_series), c.growth) ||
        `<p class="nobody">${rev ? "Данные за один год: " + rev : "Отчётности в ФНС не нашлось."}</p>`}
      <div class="facts mt">
        ${fact(sig.profit ? money(+sig.profit) : "", "прибыль",
               +sig.profit < 0 ? "bad" : "")}
        ${fact(sig.size, "размер")}
        ${fact(c.employees, "сотрудников по ФНС")}
        ${fact(sig.self_staff, "сотрудников по сайту")}
        ${fact(c.capital ? c.capital.toLocaleString("ru") + " ₽" : "", "уставный капитал")}
        ${fact(sig.hh_salary, "зарплаты в вакансиях")}
      </div>

      ${scoreBlock(c, d.score_parts || [], d.score_now)}
      ${aiBlock(c, sig)}
      ${(() => { const r = risks(c, sig); return r.length ? `
        <h4 class="mt">На что обратить внимание</h4>
        <ul class="risk-list">${r.map(
          ([t, k]) => `<li class="${k}">${esc(t)}</li>`).join("")}</ul>` : ""; })()}

      <div class="detail-links mt">${links}</div>
      <div class="detail-links mt">
        <a href="#" class="danger-link" data-del="${c.id}">Удалить и больше не показывать</a>
      </div>
    </section>
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
    const meta = [r.inn ? `<span>${esc(r.inn)}</span>` : "",
                  host ? `<a href="${esc(r.site)}" target="_blank">${esc(host)}</a>` : "",
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
    const all = (r.contacts || []).slice().sort((a, b) => rank(a) - rank(b));
    const cts = all.slice(0, 3).map(contactRow).join("")
      + (all.length > 3
         ? `<div class="ct c-more">и ещё ${all.length - 3}</div>` : "");
    // Соцсети — отдельной строкой под названием, а не в общей очереди
    // из трёх контактов: там их всегда вытесняют телефоны, и найденная
    // группа компании остаётся невидимой до открытия карточки.
    const soc = (r.contacts || []).filter((x) => x.kind === "social");
    const socLine = soc.length
      ? `<div class="soc-line">${soc.slice(0, 4).map((x) =>
          `<a class="soc" href="${esc(x.value)}" target="_blank"
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

loadStats();
// Через showView, а не напрямую: он же прячет то, что на этом экране
// лишнее. Иначе цифры в шапке дублируют плитки под ней.
showView("today");
poll();
setInterval(poll, 1500);
