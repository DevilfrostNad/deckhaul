"use strict";

// ------------------------------------------------------------------ state
let S = null;              // snapshot from the server
let order = [];            // working copy of the active list (top first)
let saved = [];            // what is in the profile right now
let preview = [];          // issues for the working copy
let selected = null;       // key shown in the details panel
let picked = null;         // key being moved with keys / gamepad
let byKey = {};
let groupIndex = {};
let busy = false;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const SEV_MARK = { error: "✖", warning: "▲", info: "•" };
const SEV_TEXT = { error: "Ошибка", warning: "Внимание", info: "Заметка" };
const CATEGORY = {
  truck: "Грузовик", trailer: "Прицеп", interior: "Салон", tuning_parts: "Тюнинг",
  ai_traffic: "Трафик", sound: "Звук", paint_job: "Покраска", cargo_pack: "Грузы", map: "Карта",
  ui: "Интерфейс", weather_setup: "Погода", physics: "Физика", graphics: "Графика",
  models: "Модели", movers: "Движущиеся объекты", walkers: "Пешеходы", prefabs: "Префабы", other: "Другое",
};

function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  if (a === 1 && b !== 11) return `${n} ${one}`;
  if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return `${n} ${few}`;
  return `${n} ${many}`;
}

function fmtDate(t) {
  const d = new Date(t * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// -------------------------------------------------------------------- api
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-DeckHaul": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`);
  return data;
}

async function run(label, fn) {
  if (busy) return;
  busy = true;
  document.body.setAttribute("aria-busy", "true");
  const t = setTimeout(() => toast(label + "…", 0), 400);
  try {
    await fn();
  } catch (e) {
    toast(e.message, 6000);
  } finally {
    clearTimeout(t);
    busy = false;
    document.body.removeAttribute("aria-busy");
    if ($("toast").dataset.sticky === "1") hideToast();
  }
}

let toastTimer;
function toast(text, ms = 3500) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  el.dataset.sticky = ms === 0 ? "1" : "0";
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(hideToast, ms);
}
function hideToast() { $("toast").hidden = true; }

// ------------------------------------------------------------------ load
function setState(snap, keepOrder = false) {
  S = snap;
  byKey = {};
  for (const m of S.mods) byKey[m.key] = m;
  groupIndex = {};
  S.groups.forEach((g, i) => (groupIndex[g.id] = i));
  saved = S.active.slice();
  if (!keepOrder) order = saved.slice();
  else order = order.filter((k) => byKey[k]);
  preview = S.issues;
  renderAll();
  if (keepOrder && isDirty()) schedulePreview();
}

async function load() {
  setState(await api("/api/state"));
  if (S.new_events && S.new_events.length) {
    toast(`С прошлой проверки: ${plural(S.new_events.length, "изменение", "изменения", "изменений")}. Смотрите вкладку «Изменения».`, 6000);
  }
}

const isDirty = () => order.join("\n") !== saved.join("\n");

let previewTimer;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    try {
      const r = await api("/api/preview", { order });
      preview = r.issues;
      renderOrder();
      renderDetails();
    } catch (e) { toast(e.message); }
  }, 250);
}

function changed() {
  renderOrder();
  renderOff();
  renderDirty();
  schedulePreview();
}

// ---------------------------------------------------------------- render
function renderAll() {
  renderTop();
  renderIssues();
  renderOrder();
  renderOff();
  renderDetails();
  renderDirty();
  renderMods();
  renderHistory();
}

function renderTop() {
  const sel = $("profile");
  sel.innerHTML = S.profiles.map((p) =>
    `<option value="${esc(p.id)}" ${S.profile && p.id === S.profile.id ? "selected" : ""}>${esc(p.name)}${p.cloud ? " (Steam Cloud)" : ""}</option>`
  ).join("") || `<option>Профилей нет</option>`;

  const lay = S.layout || {};
  const ver = S.game_version_full || S.game_version;
  const flavor = lay.active_flavor === "proton" ? "Proton" : lay.active_flavor === "native" ? "Linux-версия" : "";
  $("game").textContent = [lay.game, ver ? `версия ${ver}` : "версия неизвестна", flavor].filter(Boolean).join(" · ");

  const b = $("banner");
  const notes = [];
  if (S.game_running) notes.push("Игра запущена. Сохранять порядок можно только после выхода из неё: при закрытии игра перезапишет профиль.");
  if (S.profile && S.profile.note) notes.push(S.profile.note);
  if (S.profile && S.profile.cloud) notes.push("Профиль хранится в Steam Cloud. После сохранения дождитесь синхронизации, прежде чем играть на другом устройстве.");
  b.innerHTML = notes.map(esc).join("<br>");
  b.hidden = notes.length === 0;

  const s = S.summary;
  $("c-issues").textContent = s.error + s.warning ? String(s.error + s.warning) : "";
  $("c-order").textContent = String(order.length);
  $("c-mods").textContent = String(S.mods.filter((m) => !m.missing).length);
  $("c-history").textContent = S.new_events.length ? `+${S.new_events.length}` : "";
}

function issueActions(i) {
  const out = [];
  if (i.action === "remove_missing") out.push(`<button class="btn small" data-act="remove" data-key="${esc(i.mod)}">Убрать из списка</button>`);
  if (i.action === "dedupe") out.push(`<button class="btn small" data-act="dedupe">Убрать повтор</button>`);
  if (i.action === "rollback") {
    out.push(`<button class="btn small" data-act="rollback" data-point="${esc(i.extra.point)}">Вернуть как было</button>`);
    out.push(`<button class="btn small" data-act="ack" data-point="${esc(i.extra.point)}">Всё в порядке</button>`);
  }
  if (i.action === "compat_ok") out.push(`<button class="btn small" data-act="compat-ok" data-key="${esc(i.mod)}">Работает, не предупреждать</button>`);
  if (i.action === "autosort") out.push(`<button class="btn small" data-act="autosort">Упорядочить</button>`);
  if (i.code === "conflict") out.push(`<button class="btn small" data-act="conflicts" data-key="${esc(i.mod)}">Какие файлы</button>`);
  if (i.mod && byKey[i.mod] && !byKey[i.mod].missing) out.push(`<button class="btn small" data-act="show" data-key="${esc(i.mod)}">Показать мод</button>`);
  if (i.extra && i.extra.workshop_id) out.push(`<a class="btn small" style="display:inline-flex;align-items:center;text-decoration:none" target="_blank" rel="noopener" href="https://steamcommunity.com/sharedfiles/filedetails/?id=${Number(i.extra.workshop_id)}">Страница в Workshop</a>`);
  return out.join("");
}

function issueCard(i) {
  return `<article class="issue ${i.severity}">
    <span class="sev ${i.severity}" title="${SEV_TEXT[i.severity]}">${SEV_MARK[i.severity]}<span class="sr">${SEV_TEXT[i.severity]}</span></span>
    <div class="issue-title">${esc(i.title)}</div>
    <div class="issue-actions">${issueActions(i)}</div>
    ${i.detail ? `<div class="issue-detail">${esc(i.detail)}</div>` : ""}
    ${i.fix ? `<div class="issue-fix">${esc(i.fix)}</div>` : ""}
  </article>`;
}

function renderIssues() {
  const el = $("tab-issues");
  const s = S.summary;
  const head = `<div class="summary">
    <div><b>${s.error}</b>${plural(s.error, "ошибка", "ошибки", "ошибок").replace(/^\d+ /, "")}</div>
    <div><b>${s.warning}</b>${plural(s.warning, "предупреждение", "предупреждения", "предупреждений").replace(/^\d+ /, "")}</div>
    <div><b>${s.info}</b>${plural(s.info, "заметка", "заметки", "заметок").replace(/^\d+ /, "")}</div>
  </div>`;
  if (!S.issues.length) {
    el.innerHTML = head + `<div class="empty"><b>Всё в порядке</b>Моды установлены правильно, порядок соответствует правилам.</div>`;
    return;
  }
  const blocks = [["error", "Нужно исправить"], ["warning", "Стоит проверить"], ["info", "Для сведения"]]
    .map(([sev, title]) => {
      const list = S.issues.filter((i) => i.severity === sev);
      return list.length ? `<h2>${title}</h2>` + list.map(issueCard).join("") : "";
    }).join("");
  el.innerHTML = head + blocks;
}

function flagsFor(key) {
  const list = preview.filter((i) => i.mod === key && i.severity !== "info");
  if (!list.length) return "";
  const err = list.some((i) => i.severity === "error");
  return `<span class="flags ${err ? "flag-error" : "flag-warning"}">${err ? "✖ Ошибка" : "▲ Внимание"}</span>`;
}

function rowHtml(key, idx) {
  const m = byKey[key] || { key, name: key, missing: true, group: "top" };
  const g = S.groups[groupIndex[m.group]];
  const meta = m.missing ? "нет на диске" :
    [m.version && `v${m.version}`, g && g.title, m.source === "workshop" ? "Workshop" : "папка mod"].filter(Boolean).join(" · ");
  const thumb = m.icon && !m.missing
    ? `<img class="thumb" alt="" loading="lazy" src="/api/icon?key=${encodeURIComponent(key)}" onerror="this.style.visibility='hidden'">`
    : `<span class="thumb"></span>`;
  const cls = ["row", selected === key && "selected", picked === key && "picked", m.missing && "missing"].filter(Boolean).join(" ");
  return `<li class="${cls}" data-key="${esc(key)}" tabindex="0" draggable="true" aria-label="${esc(m.name)}, место ${idx + 1}">
    <span class="pos">${idx + 1}</span>${thumb}
    <span style="min-width:0"><div class="name">${esc(m.name)}</div><div class="meta">${esc(meta)} ${flagsFor(key)}</div></span>
    <span class="row-btns">
      <button class="icon-btn" data-move="-1" aria-label="Выше" title="Выше">▲</button>
      <button class="icon-btn" data-move="1" aria-label="Ниже" title="Ниже">▼</button>
      <button class="icon-btn" data-off="1" aria-label="Выключить" title="Выключить">✕</button>
    </span>
  </li>`;
}

function renderOrder() {
  const parts = [];
  let lastGroup = null;
  order.forEach((k, i) => {
    const mod = byKey[k];
    const g = !mod || mod.missing ? "missing" : mod.group;
    if (g !== lastGroup) {
      const title = g === "missing" ? "Нет на диске" : (S.groups[groupIndex[g]] || {}).title || "";
      parts.push(`<li class="group-head" aria-hidden="true">${esc(title)}</li>`);
      lastGroup = g;
    }
    parts.push(rowHtml(k, i));
  });
  $("order-list").innerHTML = parts.join("") ||
    `<li class="empty">В профиле не включено ни одного мода. Включите моды из списка справа.</li>`;
  $("c-order").textContent = String(order.length);
  if (picked) {
    const el = document.querySelector(`.row[data-key="${CSS.escape(picked)}"]`);
    if (el) el.focus();
  }
}

const BROKEN = ["broken_archive", "nested_archive", "nested_folder", "workshop_empty"];
const isBroken = (m) => (m.scan_problems || []).some((p) => BROKEN.includes(p[0]));

function renderOff() {
  const q = $("off-search").value.trim().toLowerCase();
  const on = new Set(order);
  const list = S.mods.filter((m) => !m.missing && !on.has(m.key))
    .filter((m) => !q || (m.name + " " + m.author + " " + m.key).toLowerCase().includes(q))
    .sort((a, b) => a.name.localeCompare(b.name, "ru"));
  $("off-list").innerHTML = list.map((m) => `<li>
      <span style="min-width:0"><div class="name">${esc(m.name)}</div><div class="meta">${esc([m.version && "v" + m.version, CATEGORY[m.category] || m.category].filter(Boolean).join(" · "))}</div></span>
      ${isBroken(m) ? `<span class="meta flag-error">✖ не загрузится</span>` : `<button class="btn small" data-on="${esc(m.key)}">Включить</button>`}
    </li>`).join("") || `<li class="meta">${q ? "Ничего не нашлось" : "Все установленные моды включены"}</li>`;
}

function renderDetails() {
  const el = $("details");
  const m = selected && byKey[selected];
  if (!m) {
    el.innerHTML = `<p class="hint" style="margin:0">Выберите мод в списке, чтобы увидеть подробности, сменить его группу или задать правило порядка.</p>`;
    return;
  }
  const comp = m.compat === false
    ? (m.compat_ok ? `✓ проверен вами на ${esc(S.game_version)} (автор указал ${esc(m.compatible.join(", "))})`
                   : `автор указал ${esc(m.compatible.join(", "))}, у вас ${esc(S.game_version)}`)
    : m.compat ? "совместим" : "автор не указал";
  const issues = preview.filter((i) => i.mod === m.key);
  const groupOpts = [`<option value="">Автоматически</option>`].concat(
    S.groups.map((g) => `<option value="${g.id}" ${m.override === g.id ? "selected" : ""}>${esc(g.title)}</option>`)).join("");
  const others = order.filter((k) => k !== m.key && byKey[k] && !byKey[k].missing);
  el.innerHTML = `<h3>${esc(m.name)}</h3>
    ${m.missing ? `<p>Мода нет на диске. Уберите его из списка или установите заново.</p>` : `
    <dl>
      <dt>Версия</dt><dd>${esc(m.version || "не указана")}</dd>
      <dt>Автор</dt><dd>${esc(m.author || "не указан")}</dd>
      <dt>Категория</dt><dd>${esc(CATEGORY[m.category] || m.category)}</dd>
      <dt>Игра</dt><dd>${comp}</dd>
      <dt>Файлов</dt><dd>${m.file_count}${m.paths_known ? "" : " (имена скрыты автором)"}</dd>
      <dt>Файл</dt><dd>${esc(m.path)}</dd>
    </dl>
    ${m.description ? `<div class="desc">${esc(m.description)}</div>` : ""}`}
    ${issues.map((i) => `<p class="${i.severity === "error" ? "flag-error" : i.severity === "warning" ? "flag-warning" : "meta"}">${SEV_MARK[i.severity]} ${esc(i.title)}</p>`).join("")}
    ${m.missing ? "" : `
    <label>Группа в порядке<select id="d-group">${groupOpts}</select></label>
    ${others.length ? `<label>Всегда ставить выше мода<select id="d-rule"><option value="">Выберите мод</option>${others.map((k) => `<option value="${esc(k)}">${esc(byKey[k].name)}</option>`).join("")}</select></label>` : ""}`}
    <div class="btns">
      ${m.compat === false && !m.missing ? (m.compat_ok
        ? `<button class="btn small" data-act="compat-undo" data-key="${esc(m.key)}">Снять отметку «работает»</button>`
        : `<button class="btn small" data-act="compat-ok" data-key="${esc(m.key)}">Работает, не предупреждать</button>`) : ""}
      ${order.includes(m.key) && !m.missing ? `<button class="btn small" data-act="conflicts" data-key="${esc(m.key)}">Пересечения файлов</button>` : ""}
      ${m.workshop_id ? `<a class="btn small" style="display:inline-flex;align-items:center;text-decoration:none" target="_blank" rel="noopener" href="https://steamcommunity.com/sharedfiles/filedetails/?id=${Number(m.workshop_id)}">Страница в Workshop</a>` : ""}
    </div>`;
  const gs = $("d-group");
  if (gs) gs.onchange = () => run("Сохраняю группу", async () => {
    setState(await api("/api/override", { key: m.key, group: gs.value || null }), true);
  });
  const rs = $("d-rule");
  if (rs) rs.onchange = () => rs.value && run("Сохраняю правило", async () => {
    setState(await api("/api/rule", { above: m.key, below: rs.value }), true);
    toast("Правило сохранено. Нажмите «Упорядочить», чтобы применить его.");
  });
}

function renderDirty() {
  const dirty = isDirty();
  $("dirty").textContent = dirty ? "Порядок изменён и ещё не сохранён" : "";
  const ap = $("apply");
  const blocked = !dirty || S.game_running || !S.profile || !S.profile.writable;
  ap.setAttribute("aria-disabled", String(blocked));
  ap.title = S.game_running ? "Закройте игру, чтобы сохранить" : !dirty ? "Изменений нет" : "";
  $("revert").hidden = !dirty;
}

function renderMods() {
  const q = $("mods-search").value.trim().toLowerCase();
  const f = $("mods-filter").value;
  const on = new Set(saved);
  const problem = new Set(S.issues.filter((i) => i.severity !== "info").map((i) => i.mod));
  const rows = S.mods.filter((m) => !m.missing)
    .filter((m) => !q || [m.name, m.author, m.key, m.path].join(" ").toLowerCase().includes(q))
    .filter((m) => f === "all" || (f === "on" && on.has(m.key)) || (f === "off" && !on.has(m.key)) ||
      (f === "problems" && problem.has(m.key)) || (f === "workshop" && m.source === "workshop") ||
      (f === "local" && m.source === "local"))
    .sort((a, b) => a.name.localeCompare(b.name, "ru"));
  $("mods-body").innerHTML = rows.map((m) => `<tr>
      <td><div>${esc(m.name)} ${problem.has(m.key) ? `<span class="flag-warning">▲</span>` : ""}</div><div class="meta">${esc(m.author || "")}</div></td>
      <td>${esc(m.version || "—")}</td>
      <td>${esc(CATEGORY[m.category] || m.category)}</td>
      <td>${m.compat === false ? (m.compat_ok ? "✓ проверен вами" : `<span class="flag-warning">▲ для ${esc(m.compatible.join(", "))}</span>`) : m.compat ? "✓ да" : "не указана"}</td>
      <td>${m.source === "workshop" ? "Workshop" : "папка mod"}${on.has(m.key) ? ` · <span class="state-on">включён</span>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="empty">Ничего не нашлось</td></tr>`;
}

function renderHistory() {
  $("events").innerHTML = S.events.map((e) =>
    `<li><span class="when">${fmtDate(e.t)}</span><span>${esc(e.text)}</span></li>`
  ).join("") || `<li class="meta">Изменений пока нет. DeckHaul запоминает состояние модов при каждой проверке и покажет здесь, что обновилось.</li>`;
}

const REASON = {
  apply: "Сохранение порядка", install: "Установка", rollback: "Возврат", restore: "Возврат старой копии",
  manual: "Сохранено вами",
};

async function renderBackups() {
  try {
    const r = await api("/api/points");
    $("points").innerHTML = r.points.map((p) => {
      const title = p.kind === "manual" ? `<b>${esc(p.label)}</b>` : esc(p.summary || REASON[p.reason] || "");
      const meta = [p.kind === "manual" ? "сохранено вами" : "создано автоматически",
        p.profile_name && `профиль «${esc(p.profile_name)}»`,
        p.files_changed ? `файлов изменено: ${p.files_changed}` : ""].filter(Boolean).join(" · ");
      const own = p.kind === "manual" || p.rolled_back ? 0 : 1;
      const extra = p.undo_count - own;
      const later = extra > 0 ? `<div class="meta">Отменит ${own ? "и " : ""}${plural(extra, "более позднее изменение", "более поздних изменения", "более поздних изменений")}</div>`
        : p.kind === "manual" && !p.undo_count ? `<div class="meta">Сейчас всё так же, как в этой точке</div>` : "";
      const status = p.rolled_back ? `<div class="meta">Это изменение сейчас отменено</div>` : "";
      const btn = `<button class="btn small" data-act="rollback" data-point="${esc(p.id)}">${p.kind === "manual" ? "Вернуться к этому состоянию" : "Вернуть как было до этого"}</button>`;
      return `<li class="${p.rolled_back ? "muted" : ""}">
        <span class="when">${fmtDate(p.t)}</span>
        <span class="pt-body"><div>${title}</div><div class="meta">${meta}</div>${status}${later}</span>
        <span class="pt-btns">${btn}${p.kind === "manual" ? `<button class="btn small" data-act="point-delete" data-point="${esc(p.id)}">Удалить</button>` : ""}</span>
      </li>`;
    }).join("") || `<li class="meta">Точек пока нет. Первая появится перед первым изменением или когда вы сохраните состояние.</li>`;
  } catch (e) { toast(e.message); }
  if (!S.profile) return;
  try {
    const r = await api("/api/backups");
    $("legacy").hidden = !r.backups.length;
    $("backups").innerHTML = r.backups.map((b) =>
      `<li><span>${esc(b.name)}</span><span class="when">${fmtDate(b.mtime)}</span>
       <button class="btn small" data-restore="${esc(b.name)}">Вернуть профиль</button></li>`).join("");
  } catch (e) { /* no legacy copies */ }
}

// ---------------------------------------------------------------- actions
function move(key, delta) {
  const i = order.indexOf(key);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  changed();
}

function enable(key) {
  const g = groupIndex[byKey[key].group] ?? 0;
  let at = 0;
  order.forEach((k, i) => { if ((groupIndex[(byKey[k] || {}).group] ?? 0) <= g) at = i + 1; });
  order.splice(at, 0, key);
  selected = key;
  changed();
  renderDetails();
  toast(`«${byKey[key].name}» включён на место ${at + 1}`);
}

function disable(key) {
  order = order.filter((k) => k !== key);
  if (picked === key) picked = null;
  changed();
}

async function autosort() {
  await run("Упорядочиваю", async () => {
    const r = await api("/api/autosort", { order });
    const moved = r.order.filter((k, i) => order[i] !== k).length;
    order = r.order;
    preview = r.issues;
    renderOrder(); renderOff(); renderDirty(); renderDetails();
    let msg = moved ? `Перемещено модов: ${moved}. Проверьте и сохраните.` : "Порядок уже правильный.";
    if (r.cycles.length) msg += " Правила противоречат друг другу для: " + r.cycles.join(", ");
    toast(msg, 6000);
  });
}

async function apply() {
  if ($("apply").getAttribute("aria-disabled") === "true") {
    if (S.game_running) toast("Закройте игру, затем сохраните порядок.");
    return;
  }
  await run("Сохраняю", async () => {
    const r = await api("/api/apply", { order });
    setState(r.state);
    toast("Порядок сохранён. Вернуть как было можно на вкладке «Точки восстановления».", 6000);
  });
}

async function showConflicts(key) {
  await run("Ищу пересечения", async () => {
    const r = await api("/api/conflicts?key=" + encodeURIComponent(key));
    $("dialog-title").textContent = `${r.mod}: заменено ${r.lost} из ${r.total} файлов`;
    $("dialog-body").innerHTML = r.rows.length
      ? `<p class="hint">Файл берётся из мода, который стоит выше. Его версия и работает в игре.</p>
         <table><tr><td class="meta">Файл</td><td class="meta">Чей работает</td></tr>${r.rows.map((x) => `<tr><td>${esc(x.file)}</td><td>${esc(x.winner)}</td></tr>`).join("")}</table>`
      : `<p>Файлы этого мода никто не заменяет.</p>`;
    $("dialog").showModal();
  });
}

function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
  if (name === "backups") renderBackups();
  if (name === "install") loadDownloads(true);
}

function currentTab() {
  return document.querySelector('.tabs button[aria-selected="true"]').dataset.tab;
}

function showMod(key) {
  showTab("order");
  selected = key;
  renderOrder();
  renderDetails();
  const el = document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
  if (el) { el.scrollIntoView({ block: "center" }); el.focus(); }
}

// ----------------------------------------------------------------- events
document.addEventListener("click", (e) => {
  const t = e.target.closest("button, .row, a");
  if (!t) return;
  if (t.matches(".tabs button")) return showTab(t.dataset.tab);
  if (t.dataset.act === "remove") { disable(t.dataset.key); showTab("order"); return toast("Мод убран из списка. Сохраните порядок, чтобы записать изменения."); }
  if (t.dataset.act === "dedupe") { order = [...new Set(order)]; changed(); showTab("order"); return; }
  if (t.dataset.act === "autosort") { showTab("order"); return autosort(); }
  if (t.dataset.act === "conflicts") return showConflicts(t.dataset.key);
  if (t.dataset.act === "compat-ok" || t.dataset.act === "compat-undo") {
    const ok = t.dataset.act === "compat-ok";
    return run("Сохраняю", async () => {
      setState(await api("/api/compat-ok", { key: t.dataset.key, ok }), true);
      toast(ok ? "Запомнил. Предупреждение вернётся, если обновится игра или сам мод."
               : "Отметка снята, предупреждение снова показывается.");
    });
  }
  if (t.dataset.act === "show") return showMod(t.dataset.key);
  if (t.dataset.on) return enable(t.dataset.on);
  if (t.dataset.restore) return run("Восстанавливаю", async () => {
    setState(await api("/api/restore", { name: t.dataset.restore }));
    renderBackups();
    toast("Профиль возвращён. Состояние до этого сохранено в точке восстановления.", 6000);
  });
  const row = t.closest(".row");
  if (row) {
    const key = row.dataset.key;
    if (t.dataset.move) return move(key, Number(t.dataset.move));
    if (t.dataset.off) return disable(key);
    if (selected === key) picked = picked === key ? null : key;
    else { selected = key; picked = null; }
    renderOrder();
    renderDetails();
    const el = document.querySelector(`.row[data-key="${CSS.escape(key)}"]`);
    if (el) el.focus();
  }
});

$("order-list").addEventListener("keydown", (e) => {
  const row = e.target.closest(".row");
  if (!row || e.target.closest("button") && e.key === "Enter") return;
  const key = row.dataset.key;
  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    e.preventDefault();
    const d = e.key === "ArrowUp" ? -1 : 1;
    if (picked === key) return move(key, d);
    const rows = [...document.querySelectorAll(".row")];
    const next = rows[rows.indexOf(row) + d];
    if (next) next.focus();
  } else if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    selected = key;
    picked = picked === key ? null : key;
    renderOrder(); renderDetails();
  } else if (e.key === "Escape" && picked) {
    picked = null; renderOrder();
  } else if (e.key === "Delete") {
    disable(key);
  }
});

// mouse drag and drop
let dragKey = null;
$("order-list").addEventListener("dragstart", (e) => {
  const row = e.target.closest(".row");
  if (row) { dragKey = row.dataset.key; e.dataTransfer.effectAllowed = "move"; }
});
$("order-list").addEventListener("dragover", (e) => { if (dragKey) e.preventDefault(); });
$("order-list").addEventListener("drop", (e) => {
  e.preventDefault();
  const row = e.target.closest(".row");
  if (!dragKey || !row || row.dataset.key === dragKey) return;
  const from = order.indexOf(dragKey);
  order.splice(from, 1);
  let to = order.indexOf(row.dataset.key);
  const r = row.getBoundingClientRect();
  if (e.clientY > r.top + r.height / 2) to += 1;
  order.splice(to, 0, dragKey);
  dragKey = null;
  changed();
});

$("off-search").addEventListener("input", renderOff);
$("mods-search").addEventListener("input", renderMods);
$("mods-filter").addEventListener("change", renderMods);
$("autosort").addEventListener("click", autosort);
$("apply").addEventListener("click", apply);
$("revert").addEventListener("click", () => { order = saved.slice(); picked = null; preview = S.issues; changed(); });
$("refresh").addEventListener("click", () => run("Проверяю моды", async () => {
  setState(await api("/api/refresh", {}), true);
  toast("Проверка закончена");
}));
$("profile").addEventListener("change", (e) => {
  if (isDirty() && !confirm("Несохранённый порядок будет потерян. Переключить профиль?")) {
    e.target.value = S.profile.id;
    return;
  }
  run("Открываю профиль", async () => { selected = picked = null; setState(await api("/api/profile", { id: e.target.value })); });
});
$("online").addEventListener("click", () => run("Спрашиваю Steam", async () => {
  const r = await api("/api/online", {});
  setState(r.state, true);
  toast(`Проверено модов из Workshop: ${r.checked}. Результат — на вкладке «Проблемы».`, 6000);
}));
$("point-form").addEventListener("submit", (e) => {
  e.preventDefault();
  run("Сохраняю состояние", async () => {
    await api("/api/points/save", { label: $("point-label").value });
    $("point-label").value = "";
    renderBackups();
    toast("Состояние сохранено. К нему можно вернуться в любой момент.");
  });
});
document.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.act === "rollback") {
    if (isDirty() && !confirm("Несохранённый порядок модов будет потерян. Продолжить?")) return;
    return run("Возвращаю", async () => {
      const r = await api("/api/points/rollback", { id: t.dataset.point });
      setState(r.state);
      renderBackups();
      const x = r.result;
      toast(`Готово: возвращено файлов ${x.returned}, убрано ${x.removed}${x.profile ? ", профиль восстановлен" : ""}. ` +
        "Если стало хуже, отмените возврат: он тоже есть в списке точек.", 8000);
    });
  }
  if (t.dataset.act === "point-delete") return run("Удаляю", async () => {
    await api("/api/points/delete", { id: t.dataset.point });
    renderBackups();
  });
  if (t.dataset.act === "ack") return run("Скрываю", async () => {
    setState(await api("/api/points/ack", { id: t.dataset.point }), true);
  });
});

window.addEventListener("beforeunload", (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });

// keyboard tab switching (Steam Deck desktop mode maps L1/R1 to keys only in some layouts)
const TABS = ["issues", "install", "order", "mods", "history", "backups"];
function cycleTab(d) {
  const i = TABS.indexOf(currentTab());
  showTab(TABS[(i + d + TABS.length) % TABS.length]);
}
document.addEventListener("keydown", (e) => {
  if (e.key === "PageDown" && e.ctrlKey) { e.preventDefault(); cycleTab(1); }
  if (e.key === "PageUp" && e.ctrlKey) { e.preventDefault(); cycleTab(-1); }
});

// ---------------------------------------------------------------- gamepad
// D-pad moves focus, A presses, B cancels, X picks a mod up, Y sorts,
// L1/R1 switch tabs, Start saves.
const padPrev = {};
function sendKey(key) {
  const el = document.activeElement || document.body;
  el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
}
function focusStep(d) {
  const items = [...document.querySelectorAll("main :is(button, select, input, a, .row):not([hidden])")]
    .filter((el) => el.offsetParent !== null && !el.closest("[hidden]"));
  const i = items.indexOf(document.activeElement);
  const next = items[Math.max(0, Math.min(items.length - 1, i + d))];
  if (next) { next.focus(); next.scrollIntoView({ block: "nearest" }); }
}
function padLoop() {
  for (const pad of navigator.getGamepads ? navigator.getGamepads() : []) {
    if (!pad) continue;
    const prev = padPrev[pad.index] || [];
    const down = pad.buttons.map((b) => b.pressed);
    const hit = (n) => down[n] && !prev[n];
    const onRow = document.activeElement && document.activeElement.classList.contains("row");
    if (hit(12)) onRow ? sendKey("ArrowUp") : focusStep(-1);
    if (hit(13)) onRow ? sendKey("ArrowDown") : focusStep(1);
    if (hit(14)) focusStep(-1);
    if (hit(15)) focusStep(1);
    if (hit(0) && document.activeElement) document.activeElement.click();
    if (hit(1)) { if ($("picker").open) $("picker").close(); else if ($("dialog").open) $("dialog").close(); else if (picked) { picked = null; renderOrder(); } }
    if (hit(2) && onRow) sendKey("Enter");
    if (hit(3) && currentTab() === "order") autosort();
    if (hit(4)) cycleTab(-1);
    if (hit(5)) cycleTab(1);
    if (hit(9) && currentTab() === "order") apply();
    padPrev[pad.index] = down;
  }
  requestAnimationFrame(padLoop);
}
window.addEventListener("gamepadconnected", () => requestAnimationFrame(padLoop), { once: true });

// ------------------------------------------------------------- downloads
let DL = null;
const seenDownloads = new Set();
let firstDownloadsLoad = true;

function fmtSize(b) {
  if (b >= 1 << 30) return (b / (1 << 30)).toFixed(1).replace(".", ",") + " ГБ";
  if (b >= 1 << 20) return (b / (1 << 20)).toFixed(1).replace(".", ",") + " МБ";
  return Math.max(1, Math.round(b / 1024)) + " КБ";
}

async function loadDownloads(render) {
  try {
    DL = await api("/api/downloads");
  } catch (e) {
    if (render) toast(e.message);
    return;
  }
  const ready = DL.items.filter((c) => c.status === "ready" && !c.already);
  $("c-install").textContent = ready.length ? String(ready.length) : "";
  // Files still being analysed count as seen too, so they do not pop up as "new" later.
  const fresh = ready.filter((c) => !seenDownloads.has(c.id));
  DL.items.forEach((c) => seenDownloads.add(c.id));
  if (!firstDownloadsLoad && fresh.length && currentTab() !== "install") {
    toast(`В загрузках новый мод: ${fresh[0].name}. Откройте вкладку «Установка».`, 6000);
  }
  firstDownloadsLoad = false;
  if (render || currentTab() === "install") renderDownloads();
  if (DL.pending) setTimeout(() => loadDownloads(false), 300);
}

function payloadHtml(c, p) {
  const m = p.mod || {};
  const compat = p.compat === false
    ? (p.compat_ok ? `✓ вы отметили, что он работает на ${esc(S.game_version)}`
                   : `<span class="flag-warning">▲ автор указал ${esc((m.compatible || []).join(", "))}, у вас ${esc(S.game_version)}</span>`)
    : p.compat ? "✓ подходит к вашей версии игры" : "совместимость не указана";
  const lines = [];
  for (const r of p.replaces) lines.push(`Обновит «${esc(r.name)}» ${esc(r.version || "")} (${esc(r.file)})`);
  if (p.overwrite && !p.replaces.length) lines.push(`Файл ${esc(p.target)} уже есть в папке и будет заменён`);
  if (p.workshop_twin) lines.push(`<span class="flag-warning">▲ Этот мод уже подписан в Workshop: «${esc(p.workshop_twin)}». Две копии будут мешать друг другу.</span>`);
  for (const pr of p.problems) lines.push(`<span class="flag-error">✖ ${esc(pr)}</span>`);
  const id = `pl-${c.id}-${p.id}`;
  return `<label class="payload" for="${id}">
    <input type="checkbox" id="${id}" data-payload="${p.id}" ${!p.installable ? "disabled" : p.compat === false && !p.compat_ok ? "" : "checked"}>
    <span style="min-width:0">
      <span class="name">${esc(m.name || p.target)}</span> <span class="meta">${esc(m.version ? "v" + m.version : "")} · ${esc(p.target)}</span>
      <div class="meta">${compat}${p.notes.length ? " · " + esc(p.notes.join(", ")) : ""}</div>
      ${lines.map((l) => `<div class="meta">${l}</div>`).join("")}
    </span>
  </label>`;
}

function candidateHtml(c) {
  const head = `<div class="cand-head"><span class="name">${esc(c.name)}</span>
    <span class="meta">${fmtSize(c.size)} · ${fmtDate(c.mtime)}${DL.dirs.length > 1 ? " · " + esc(c.folder.split("/").pop()) : ""}</span></div>`;
  if (c.status === "pending") {
    return `<article class="cand">${head}<p class="meta">Разбираю архив…</p></article>`;
  }
  if (c.status !== "ready") {
    return `<article class="cand" data-cand="${c.id}">${head}
      <p class="${c.status === "needs_tool" ? "flag-warning" : "flag-error"}">${esc(c.error)}</p>
      <div class="btns"><button class="btn small" data-dismiss="${c.id}">Скрыть</button></div></article>`;
  }
  const anyOld = c.payloads.some((p) => p.replaces.length);
  const prof = S.profile ? `«${esc(S.profile.name)}»` : "";
  return `<article class="cand" data-cand="${c.id}">${head}
    ${c.payloads.map((p) => payloadHtml(c, p)).join("")}
    <div class="opts">
      ${S.profile && S.profile.writable ? `<label><input type="checkbox" data-opt="enable" checked> Включить в профиле ${prof}</label>` : ""}
      ${anyOld ? `<label><input type="checkbox" data-opt="remove_old" checked> Убрать старую версию в корзину DeckHaul</label>` : ""}
      <label><input type="checkbox" data-opt="delete_download" ${c.is_default_dir ? "checked" : ""}> Убрать исходный файл в корзину DeckHaul</label>
    </div>
    <div class="btns">
      <button class="btn primary" data-install="${c.id}">${c.already ? "Установить заново" : "Установить"}</button>
      <button class="btn small" data-dismiss="${c.id}">Скрыть</button>
    </div></article>`;
}

function renderDownloads() {
  if (!DL) return;
  const short = (p) => (p && DL.home && (p === DL.home || p.startsWith(DL.home + "/")) ? "~" + p.slice(DL.home.length) : p);
  $("dl-target").textContent = short(DL.target) || "папку игры, когда она найдётся";
  $("dl-dirs").innerHTML = DL.dirs.map((d) => `<li>
      <span style="min-width:0"><span class="name">${esc(short(d.path))}</span>
      ${d.default ? `<span class="meta">загрузки браузера</span>` : ""}
      ${d.exists ? "" : `<span class="meta flag-error">✖ папки нет</span>`}</span>
      <button class="btn small" data-dir-remove="${esc(d.path)}">Не искать здесь</button>
    </li>`).join("") || `<li class="meta">Ни одной папки. Добавьте папку, куда вы скачиваете моды.</li>`;
  $("dl-busy").hidden = !DL.busy.length;
  $("dl-busy").textContent = DL.busy.length ? "Ещё скачиваются: " + DL.busy.join(", ") : "";
  const list = $("dl-list");
  const fresh = DL.items.filter((c) => !c.already);
  const done = DL.items.filter((c) => c.already);
  list.innerHTML = (fresh.map(candidateHtml).join("") ||
    `<div class="empty"><b>Новых модов нет</b>Скачайте мод, он появится здесь через несколько секунд после окончания загрузки.</div>`) +
    (done.length ? `<details class="done"><summary>Уже установлены: ${done.length}</summary>${done.map(candidateHtml).join("")}</details>` : "");
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.dismiss) {
    const id = t.dataset.dismiss;
    return run("Скрываю", async () => {
      await api("/api/downloads/dismiss", { id });
      await loadDownloads(true);
    });
  }
  if (t.dataset.install) {
    const card = t.closest(".cand");
    const payloads = [...card.querySelectorAll("input[data-payload]:checked")].map((i) => Number(i.dataset.payload));
    if (!payloads.length) return toast("Отметьте хотя бы один мод");
    const opt = (n) => { const i = card.querySelector(`input[data-opt="${n}"]`); return i ? i.checked : false; };
    return run("Устанавливаю", async () => {
      const r = await api("/api/downloads/install", {
        id: t.dataset.install, payloads,
        enable: opt("enable"), remove_old: opt("remove_old"), delete_download: opt("delete_download"),
      });
      setState(await api("/api/state"));
      await loadDownloads(true);
      toast(`Установлено: ${r.installed.join(", ")}. ${r.note}`.trim(), 7000);
    });
  }
});

// folder picker
let pickerPath = null;
async function browse(path) {
  const r = await api("/api/browse" + (path ? "?path=" + encodeURIComponent(path) : ""));
  pickerPath = r.path;
  $("picker-path").value = r.path;
  $("picker-places").innerHTML = r.places.map((p) =>
    `<button class="btn small" data-browse="${esc(p.path)}">${esc(p.title)}</button>`).join("");
  $("picker-info").textContent = r.archives
    ? `В этой папке архивов и модов: ${r.archives}`
    : "В этой папке нет архивов, но они могут быть во вложенных папках.";
  const rows = [];
  if (r.parent) rows.push(`<li><button class="pick-row" data-browse="${esc(r.parent)}">↑ На уровень выше</button></li>`);
  for (const n of r.dirs) {
    const full = r.path.replace(/\/$/, "") + "/" + n;
    rows.push(`<li><button class="pick-row" data-browse="${esc(full)}">${esc(n)}</button></li>`);
  }
  $("picker-dirs").innerHTML = rows.join("") || `<li class="meta">Вложенных папок нет</li>`;
  const first = $("picker-dirs").querySelector("button");
  if (first) first.focus();
}

$("dl-dir-add").addEventListener("click", () => run("Открываю папки", async () => {
  await browse(null);
  $("picker").showModal();
}));
$("picker-form").addEventListener("submit", (e) => {
  e.preventDefault();
  run("Открываю", () => browse($("picker-path").value.trim()));
});
$("picker-cancel").addEventListener("click", () => $("picker").close());
$("picker-choose").addEventListener("click", () => run("Добавляю папку", async () => {
  DL = await api("/api/downloads/dirs/add", { path: pickerPath });
  $("picker").close();
  renderDownloads();
  toast("Папка добавлена. Моды из неё появятся в списке.");
}));
document.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.browse) return run("Открываю", () => browse(t.dataset.browse));
  if (t.dataset.dirRemove) return run("Убираю папку", async () => {
    DL = await api("/api/downloads/dirs/remove", { path: t.dataset.dirRemove });
    renderDownloads();
  });
});

setInterval(() => {
  if (!document.hidden && !busy) loadDownloads(false);
}, 5000);

// ---------------------------------------------------------------- update
let UPD = null;

function renderUpdate() {
  const el = $("update");
  if (!UPD) { el.innerHTML = ""; return; }
  const checked = UPD.checked ? `Проверено ${fmtDate(UPD.checked)}.` : "Ещё не проверялось.";
  let body;
  if (UPD.available) {
    body = `<p><b>Вышла версия ${esc(UPD.latest.replace(/^v/, ""))}</b>, у вас ${esc(UPD.current)}.</p>
      ${UPD.notes ? `<div class="notes">${esc(UPD.notes)}</div>` : ""}
      ${UPD.can_apply
        ? `<button class="btn primary" data-act="update-apply">Обновить DeckHaul</button>
           <span class="meta">Займёт несколько секунд, потом страница перезагрузится сама.</span>`
        : `<p class="meta">Эта копия запущена из папки с исходниками. Обновите её командой <code>git pull</code>.</p>`}`;
  } else {
    body = `<p>У вас DeckHaul ${esc(UPD.current)}${UPD.latest ? ", это последняя версия" : ""}.</p>`;
  }
  el.innerHTML = `<h2>Версия DeckHaul</h2>${body}
    ${UPD.error ? `<p class="flag-warning">▲ ${esc(UPD.error)}</p>` : ""}
    <div class="btns">
      <button class="btn small" data-act="update-check">Проверить сейчас</button>
      <label class="check"><input type="checkbox" id="upd-auto" ${UPD.auto ? "checked" : ""}> Проверять раз в день</label>
    </div>
    <p class="meta">${checked} DeckHaul обращается только к GitHub и ничего о вас не передаёт.</p>
    <h2>Изменения модов</h2>`;
  $("upd-auto").onchange = (e) => run("Сохраняю", async () => {
    UPD = await api("/api/update/auto", { on: e.target.checked });
    renderUpdate();
  });
}

async function loadUpdate() {
  try { UPD = await api("/api/update"); } catch (e) { return; }
  renderUpdate();
  if (UPD.available) {
    $("c-history").textContent = "новая версия";
    toast(`Вышел DeckHaul ${UPD.latest.replace(/^v/, "")}. Обновить можно на вкладке «Изменения».`, 7000);
  }
}

async function waitForRestart() {
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const r = await fetch("/api/ping", { cache: "no-store" });
      if (r.ok && i > 1) return location.reload();
    } catch (e) { /* server is restarting */ }
  }
  toast("DeckHaul не перезапустился сам. Закройте окно и откройте программу снова.", 0);
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.dataset.act === "update-check") return run("Проверяю обновления", async () => {
    UPD = await api("/api/update/check", {});
    renderUpdate();
    toast(UPD.error ? UPD.error : UPD.available ? `Доступна версия ${UPD.latest.replace(/^v/, "")}` : "У вас последняя версия");
  });
  if (t.dataset.act === "update-apply") return run("Обновляю DeckHaul", async () => {
    if (isDirty() && !confirm("Несохранённый порядок модов будет потерян. Обновить?")) return;
    const r = await api("/api/update/apply", {});
    toast(`Установлена версия ${r.installed.replace(/^v/, "")}. Перезапускаю…`, 0);
    await waitForRestart();
  });
});

setInterval(() => fetch("/api/ping").catch(() => {}), 30000);
load().then(() => { loadDownloads(false); loadUpdate(); }).catch((e) => toast(e.message, 0));
