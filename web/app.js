import { renderManagement } from "./management.js";
import { renderAdmin } from "./admin.js";
import { openEvidence, closeEvidence, refreshEvidence } from "./evidence.js";
import { events, renderMarkdown } from "./answer.js";

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[ch]);
const paths = {
  settings: '<path d="m10 3-.5 2-2 .9-1.8-.6-2 3.4L5 10.1v2.4l-1.3 1.4 2 3.4 1.8-.6 2 .9.5 2h4l.5-2 2-.9 1.8.6 2-3.4-1.3-1.4v-2.4l1.3-1.4-2-3.4-1.8.6-2-.9L14 3z"/><circle cx="12" cy="11.3" r="3"/>',
  compose: '<path d="M12 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-7"/><path d="m11 14 1-4L19 3l3 3-7 7z"/>',
  up: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  down: '<path d="m7 10 5 5 5-5"/>',
  chat:'<path d="M20.5 11a8.5 8.5 0 0 1-8.5 8.5H6l-4 2 1.5-5A8.5 8.5 0 1 1 20.5 11Z"/><path d="M7.5 10.5h9m-9 3h5.5"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h4M9 12h6m-6 4h6"/>',
  link: '<path d="m10 13 4-4m-6 6-1 1a3.5 3.5 0 0 1-5-5l4-4a3.5 3.5 0 0 1 5 0m2 2 1-1a3.5 3.5 0 0 1 5 5l-4 4a3.5 3.5 0 0 1-5 0" transform="translate(2 -1)"/>',
  database: '<ellipse cx="12" cy="5.5" rx="8" ry="3.5"/><path d="M4 5.5v6c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5v-6M4 12v6c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5v-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  layers: '<path d="m3 8 9-5 9 5-9 5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  sparkle: '<path d="m12 3 1.8 5.3L19 10l-5.2 1.7L12 17l-1.8-5.3L5 10l5.2-1.7zM19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z"/>',
  send: '<path d="m4 4 17 8-17 8 3-8zM7 12h10"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  external: '<path d="M6 18 18 6M8 6h10v10"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
  folder: '<path d="M3 6h7l2 3h9v11H3z"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M3 16v5h18v-5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
  edit: '<path d="m4 20 5-1L21 7l-4-4L5 15zM14 6l4 4"/>',
  play: '<path d="m7 4 13 8-13 8z"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m15.5 15.5 5 5"/>',
  copy: '<path d="M8 8h12v13H8zM4 16V3h11"/>',
  shield: '<path d="m12 2 8 3v6c0 5-4 8-8 11-4-3-8-6-8-11V5z"/><path d="m8 11 3 3 5-6"/>',
  book: '<path d="M3 4h6a4 4 0 0 1 3 2 4 4 0 0 1 3-2h6v15h-6a4 4 0 0 0-3 2 4 4 0 0 0-3-2H3zM12 6v15"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  logout: '<path d="M9 3H4v18h5m2-9h10m-4-4 4 4-4 4"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a7 7 0 0 1 7-6h2a7 7 0 0 1 7 6v2"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  quote: '<path d="M3 11h6v7H3v-6c0-4 2-6 6-7m5 6h6v7h-6v-6c0-4 2-6 6-7"/>',
  enter: '<path d="M20 5v6a3 3 0 0 1-3 3H4m4-4-4 4 4 4"/>',
  filter: '<path d="M3 6h18M6 12h12m-9 6h6"/>',
  key: '<circle cx="8" cy="10" r="5"/><path d="m12 13 8 7m-3-3 3-3"/>'
};
function icon(name, size = 18) {
  return `<svg class="ui-icon" aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${paths[name] ?? ""}</svg>`;
}

const pages = { ask:"知识问答", documents:"文档库", admin:"知识库概览", imports:"导入管理", connectors:"数据接口配置", models:"模型配置", retrieval:"检索评测" };
const managementPages = new Set(["admin", "imports", "connectors", "models", "retrieval"]);
const modes = { mixed:"综合查询", auto:"自动判断", documents:"文档检索", data:"接口查询" };
const statuses = { answered:"已回答", partial:"部分回答", needs_input:"待补充", no_evidence:"缺少证据", error:"查询失败" };
const state = {
  page:"ask", session:null, mode:"mixed", draft:"", turns:[], active:null, reading:false, sequence:0,
  replayError:"", scrollTop:0, chatKey:0,
  history:[], historyFilter:"", historyBusy:false, historyMore:false, historyOffset:0, historyError:"", historySequence:0
};
const historySize = 20;
const smallScreen = window.matchMedia("(max-width: 760px)");
const modifier = /Mac|iPad|iPhone/.test(navigator.platform) ? "⌘" : "Ctrl";
let searchTimer;
let toastTimer;
let nextChatKey = 0;
// Interrupted turns and unsent text are private to this tab and login.
const drafts = new Map();

function rememberDraft() {
  if (!state.chatKey || state.reading) return;
  if (state.draft || state.turns.some(turn => turn.phase === "failed" || turn.phase === "stopped")) {
    drafts.set(state.chatKey, {chatKey:state.chatKey, turns:state.turns, draft:state.draft, mode:state.mode,
      scrollTop:state.scrollTop, latestId:state.turns.findLast(turn => turn.result.id)?.result.id});
  } else drafts.delete(state.chatKey);
}

function notify(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, isError ? 6500 : 3200);
}

const jsonOptions = body => ({method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
const canUseWorkspace = () => Boolean(state.session?.authenticated || state.session?.auth_required === false);
const canManage = () => canUseWorkspace() && ["admin", "administrator"].includes(state.session.role);

function resetAsk() {
  stopQuery();
  state.sequence++;
  Object.assign(state, {reading:false, turns:[], replayError:"", draft:"", mode:"mixed", scrollTop:0, chatKey:++nextChatKey});
  closeEvidence(false);
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {credentials:"same-origin", ...options});
  } catch (error) {
    if (options.signal?.aborted) throw error;
    $("connection").hidden = false;
    throw new Error("无法连接服务，请检查网络后重试。");
  }
  $("connection").hidden = true;
  if (response.ok) return response;
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && canUseWorkspace() && path !== "/api/login") {
    drafts.clear();
    state.session = {authenticated:false, auth_required:true};
    clearTimeout(searchTimer);
    state.historySequence++;
    Object.assign(state, {history:[], historyFilter:"", historyBusy:false, historyMore:false, historyError:"", historyOffset:0});
    $("history-search").value = "";
    resetAsk();
    showLogin();
  }
  const detail = payload.detail ?? payload.error ?? payload.message ?? `请求失败（${response.status}）`;
  throw new Error(Array.isArray(detail) ? detail.map(item => item.msg || String(item)).join("，") : String(detail));
}

async function api(path, options = {}) {
  const response = await request(path, options);
  return response.json().catch(() => ({}));
}

function formatTime(value) {
  const date = new Date(value || "");
  return Number.isNaN(+date) ? "" : date.toLocaleString("zh-CN", {
    year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false
  });
}

function historyDay(value) {
  const date = new Date(value || "");
  if (Number.isNaN(+date)) return "";
  const today = new Date(), yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "今天";
  if (date.toDateString() === yesterday.toDateString()) return "昨天";
  return date.toLocaleDateString("zh-CN", {year:date.getFullYear() !== today.getFullYear() ? "numeric" : undefined, month:"long", day:"numeric"});
}

function drawNavigation() {
  const signedIn = canUseWorkspace();
  $("sidebar-body").hidden = !signedIn;
  $("admin-link").hidden = !canManage();
  $("open-menu").disabled = !signedIn;
  for (const [id, active] of [["library-link", state.page === "documents"], ["admin-link", managementPages.has(state.page)]]) {
    if (active) $(id).setAttribute("aria-current", "page"); else $(id).removeAttribute("aria-current");
  }
  if (!signedIn) {
    $("user-area").replaceChildren();
    $("history-list").replaceChildren();
    return;
  }
  const role = canManage() ? "管理员" : "只读成员";
  const name = state.session.username || (state.session.auth_required ? role : "本地工作区");
  const detail = (state.session.username || !state.session.auth_required) ? role : "已登录";
  $("user-area").innerHTML = `<span class="avatar">${icon("user",17)}</span>
    <div class="profile"><strong>${esc(name)}</strong><small>${esc(detail)}</small></div>
    ${state.session.auth_required ? `<button class="icon-button" id="logout" title="退出登录" aria-label="退出登录">${icon("logout",16)}</button>` : ""}`;
  $("logout")?.addEventListener("click", async () => {
    try { await api("/api/logout", {method:"POST"}); location.reload(); }
    catch (error) { notify(error.message, true); }
  });
  drawHistory();
}

function drawHistory() {
  if (!canUseWorkspace()) return;
  const list = $("history-list");
  const focusedId = list.contains(document.activeElement) ? document.activeElement.getAttribute("href") : null;
  let previousDay = null;
  const html = state.history.map(item => {
    const day = historyDay(item.created_at);
    const heading = day && day !== previousDay ? `<p class="history-day">${esc(day)}</p>` : "";
    previousDay = day;
    const hash = "#ask/" + encodeURIComponent(item.id);
    return `${heading}<a class="history-row" href="${hash}" ${location.hash === hash ? 'aria-current="page"' : ""} title="${esc(item.question)}">
      ${icon("chat",15)}<span>${esc(item.question)}</span></a>`;
  }).join("");
  let note = "";
  if (state.historyBusy) note = '<p class="history-note" role="status">正在读取…</p>';
  else if (state.historyError) note = `<p class="history-note" role="alert">${esc(state.historyError)}</p><button class="text-action" id="retry-history">重新读取</button>`;
  else if (!html) note = `<p class="history-note">${state.historyFilter.trim() ? "没有匹配的历史问题" : "暂无查询历史"}</p>`;
  else if (state.historyMore) note = '<button class="text-action" id="more-history">加载更多</button>';
  list.innerHTML = html + note;
  list.setAttribute("aria-busy", String(state.historyBusy));
  $("more-history")?.addEventListener("click", () => loadHistory(true));
  $("retry-history")?.addEventListener("click", () => loadHistory(state.historyOffset > 0));
  if (focusedId) Array.from(list.querySelectorAll("a")).find(a => a.getAttribute("href") === focusedId)?.focus({preventScroll:true});
}

async function loadHistory(append = false) {
  if (!canUseWorkspace() || (append && state.historyBusy)) return;
  clearTimeout(searchTimer);
  const ticket = ++state.historySequence;
  const offset = append ? state.historyOffset : 0;
  const moreFocused = append && document.activeElement?.id === "more-history";
  Object.assign(state, {historyBusy:true, historyError:"", historyMore:false});
  if (!append) { state.historyOffset = 0; state.history = []; }
  drawHistory();
  const params = new URLSearchParams({scope:"mine", threads:"true", offset, limit:historySize + 1});
  if (state.historyFilter.trim()) params.set("q", state.historyFilter.trim());
  try {
    const rows = await api("/api/history?" + params);
    if (ticket !== state.historySequence) return;
    const page = rows.slice(0, historySize);
    const known = new Set(state.history.map(item => item.id));
    state.history.push(...page.filter(item => !known.has(item.id)));
    state.historyOffset = offset + page.length;
    state.historyMore = rows.length > historySize;
  } catch (error) {
    if (ticket === state.historySequence) state.historyError = error.message;
  } finally {
    if (ticket === state.historySequence) {
      state.historyBusy = false;
      drawHistory();
      if (moreFocused && document.activeElement === document.body) {
        ($("history-list").querySelectorAll("a")[offset] || $("retry-history"))?.focus();
      }
    }
  }
}

function searchHistory() {
  state.historyFilter = $("history-search").value;
  state.historySequence++;
  Object.assign(state, {history:[], historyBusy:true, historyMore:false, historyOffset:0, historyError:""});
  drawHistory();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadHistory(), 180);
}

function composer(landing) {
  const disabled = state.reading ? "disabled" : "";
  const options = Object.entries(modes).map(([value, label]) =>
    `<option value="${value}" ${state.mode === value ? "selected" : ""}>${label}</option>`).join("");
  return `<form class="composer" id="ask-form">
    <label class="sr-only" for="question">输入问题</label>
    <textarea id="question" rows="${landing ? 3 : 2}" maxlength="2000" placeholder="${landing ? "输入你想了解的问题…" : "输入新的问题…"}" required ${disabled}>${esc(state.draft)}</textarea>
    <div class="composer-tools"><label class="mode-picker">${icon("layers",16)}
      <select id="mode" aria-label="查询模式" ${disabled}>${options}</select>${icon("down",12)}</label>
      <div class="send-tools"><span class="key-hint" aria-hidden="true">${modifier} + Enter</span>
      <button type="submit" class="primary-btn send-btn" id="send-query" title="发送问题（${modifier} + Enter）" ${disabled}>${icon("up",16)}发送</button>
      <button type="button" class="secondary-btn" id="stop-query" hidden><span class="stop-mark" aria-hidden="true"></span>停止</button></div></div>
    </form>`;
}

function showLogin() {
  closeMenu();
  drawNavigation();
  $("main").innerHTML = `<section class="login-card"><h1>登录工作区</h1>
    <form id="login-form"><label for="username">用户名（选填）</label>
    <input id="username" name="username" type="text" autocomplete="username" placeholder="使用角色密码时可留空">
    <label for="password">访问密码</label><input id="password" name="password" type="password" required autocomplete="current-password">
    <p class="form-error" id="login-error" role="alert"></p><button class="primary-btn">登录 ${icon("arrow",16)}</button></form></section>`;
  $("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const btn = event.currentTarget.querySelector("button");
    btn.disabled = true;
    try {
      await api("/api/login", jsonOptions({username:$("username").value.trim() || undefined, password:$("password").value}));
      await boot();
    } catch (error) {
      if ($("login-error")) $("login-error").textContent = error.message;
      btn.disabled = false;
    }
  });
}

async function addFollowUp(result, section, turnKey) {
  if (!section || result?.status !== "needs_input") return;
  try {
    const configs = await api("/api/connectors");
    if (!section.isConnected) return;
    const cfg = configs.find(item => String(item.id) === String(result.connector_id));
    const mode = result.intent === "mixed" ? "mixed" : "data";
    if (!cfg && configs.length) {
      section.innerHTML = `<form class="follow-up"><label for="connector-${turnKey}">数据接口</label><select id="connector-${turnKey}" required>
        <option value="">选择接口</option>${configs.map(item => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("")}</select>
        <button class="secondary-btn">继续查询 ${icon("arrow",15)}</button></form>`;
      section.querySelector("form").onsubmit = event => {
        event.preventDefault();
        void query(result.question, {connector_id:Number(section.querySelector("select").value), mode});
      };
      return;
    }
    const missing = result.missing || [];
    const fields = (cfg?.parameters || []).filter(p => missing.includes(p.name) || missing.includes(p.label));
    if (!fields.length) return;
    section.innerHTML = `<form class="follow-up">${fields.map((p,i) => `<label for="param-${turnKey}-${i}">${esc(p.label || p.name)}</label>
      <input id="param-${turnKey}-${i}" name="${esc(p.name)}" type="${p.type === "date" ? "date" : ["integer","number"].includes(p.type) ? "number" : "text"}"
      ${p.type === "number" ? 'step="any"' : ""} required>`).join("")}
      <button class="secondary-btn">补充并查询 ${icon("arrow",15)}</button></form>`;
    section.querySelector("form").onsubmit = event => {
      event.preventDefault();
      void query(result.question, {connector_id:result.connector_id, params:{...result.params, ...Object.fromEntries(new FormData(event.currentTarget))}, mode});
    };
  } catch (error) {
    if (!section.isConnected) return;
    section.innerHTML = `<p class="form-error" role="alert">${esc(error.message)} <button class="text-action" type="button">重新加载查询条件</button></p>`;
    section.querySelector("button").onclick = () => addFollowUp(result, section, turnKey);
  }
}

let turnIndex = 0;

function requestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0,8), hex.slice(8,12), hex.slice(12,16), hex.slice(16,20), hex.slice(20)].join("-");
}

function newTurn(requestBody, saved) {
  return { key:++turnIndex, request:requestBody, result:saved || {question:requestBody.question, answer:"", citations:[]},
    phase:saved ? "complete" : "streaming", status:"正在查询…", frame:0 };
}

function storedTurn(result) {
  return newTurn({question:result.question, mode:result.intent, connector_id:result.connector_id, params:result.params || {}}, result);
}

function resultTables(citations) {
  return (citations || []).flatMap(source => {
    if (source.kind !== "api" || !Array.isArray(source.rows)) return [];
    const rows = source.rows.filter(row => row && typeof row === "object" && !Array.isArray(row));
    if (!rows.length) return [];
    const columns = [...new Set(rows.slice(0, 5).flatMap(row => Object.keys(row)))].slice(0, 6);
    if (!columns.length) return [];
    const cell = value => esc(value == null ? "—" : typeof value === "object" ? JSON.stringify(value) : value);
    return [`<div class="returned-table" tabindex="0" role="region" aria-label="查询结果">
      <table><caption>${esc(source.title || "查询结果")} · ${rows.length > 5 ? "前 5 条记录" : rows.length + " 条记录"}</caption>
      <thead><tr>${columns.map(c => "<th scope='col'>" + esc(c) + "</th>").join("")}</tr></thead>
      <tbody>${rows.slice(0,5).map(row => "<tr>" + columns.map(c => "<td>" + cell(row[c]) + "</td>").join("") + "</tr>").join("")}</tbody></table></div>`];
  }).join("");
}

function makeTurn(turn) {
  const article = document.createElement("article");
  article.className = "result-panel";
  article.id = "turn-" + turn.key;
  article.dataset.answerTurn = turn.key;
  article.tabIndex = -1;
  const time = formatTime(turn.result.created_at);
  article.innerHTML = `<header class="asked"><div class="question-meta">提问${time ? " · " + esc(time) : ""}</div><h2>${esc(turn.request.question)}</h2></header>
    <section class="answer-section"><header class="answer-title"><span class="answer-symbol">${icon("layers",17)}</span>
    <h3>知据</h3><span class="answer-status" role="status"></span></header><div class="answer-copy"></div>
    <div class="turn-table"></div><div class="turn-follow-up"></div><div class="answer-sources"></div></section>`;
  return article;
}

function paintTurn(turn, controls = false) {
  const article = $("turn-" + turn.key);
  if (!article) return;
  const scroll = $("answer-scroll");
  const follow = !document.body.classList.contains("evidence-open") && scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop <= 2;
  article.dataset.phase = turn.phase;
  const result = turn.result;
  const status = turn.phase === "streaming" ? turn.status : turn.phase === "stopped" ? "已停止" :
    turn.phase === "failed" ? turn.status : result.status === "answered" ? "" : statuses[result.status] || result.status || "";
  const label = article.querySelector(".answer-status");
  if (label.textContent !== status) label.textContent = status;
  label.hidden = !status;
  renderMarkdown(article.querySelector(".answer-copy"), result, turn.phase !== "complete");
  if (controls) {
    const count = result.citations?.length || 0;
    const actions = article.querySelector(".answer-sources");
    actions.innerHTML = (count ? `<button class="sources-button" aria-haspopup="dialog" aria-expanded="false" aria-label="查看依据，共 ${count} 条">${icon("book",16)} 查看依据 <span class="source-count">${count}</span></button>` : "") +
      (turn.phase === "stopped" || turn.phase === "failed" || result.status === "error"
        ? '<button type="button" class="secondary-btn" data-retry-turn>重新查询</button>' : "");
    if (turn.phase === "complete") {
      article.querySelector(".turn-table").innerHTML = resultTables(result.citations);
      if (result.status === "needs_input") void addFollowUp(result, article.querySelector(".turn-follow-up"), turn.key);
    }
  }
  if (follow) scroll.scrollTop = scroll.scrollHeight;
}

function queueText(turn) {
  if (!turn.frame) turn.frame = requestAnimationFrame(() => { turn.frame = 0; paintTurn(turn); });
}

function updateComposer() {
  if (!$("ask-form")) return;
  $("send-query").hidden = Boolean(state.active);
  $("stop-query").hidden = !state.active;
  $("ask-form").querySelector(".key-hint").hidden = Boolean(state.active);
}

async function loadExamples(area) {
  try {
    const payload = await api("/api/query/examples");
    if (!area.isConnected) return;
    const examples = (payload.examples || []).slice(0, 3);
    area.innerHTML = examples.map((item, i) => `<button type="button" data-example="${i}">
      <span class="example-label">${esc(item.label || modes[item.mode] || "")}</span><span>${esc(item.question)}</span>${icon("arrow",14)}</button>`).join("");
    area.onclick = event => {
      const button = event.target.closest("[data-example]");
      if (!button) return;
      const example = examples[Number(button.dataset.example)];
      state.draft = $("question").value = example.question;
      if (Object.hasOwn(modes, example.mode)) state.mode = $("mode").value = example.mode;
      $("question").focus();
    };
  } catch { /* The composer remains available while examples are unavailable. */ }
}

function renderAsk() {
  const landing = !state.turns.length && !state.reading && !state.replayError;
  const heading = '<h1 class="work-title">知识问答</h1>';
  $("main").innerHTML = landing
    ? `<div class="ask-workspace landing"><div class="body-column landing-column"><header class="landing-heading">
        ${heading}<p>从企业文档和业务数据中查找答案，点击引用核对依据。</p></header>${composer(true)}
        <div class="question-examples" aria-label="示例问题" id="question-examples"></div></div></div>`
    : `<div class="ask-workspace active-ask"><header class="session-heading">${icon("chat",17)}${heading}</header>
        <div class="answer-scroll" id="answer-scroll" role="region" aria-label="问答内容" tabindex="0"><div class="body-column" id="conversation"></div></div>
        <div class="composer-dock"><div class="body-column">${composer(false)}</div></div></div>`;
  if (landing) void loadExamples($("question-examples"));
  else {
    const list = $("conversation");
    if (state.reading) list.innerHTML = '<p class="reading-state" role="status">正在打开历史回答…</p>';
    if (state.replayError) {
      list.innerHTML = `<div class="error-state" role="alert"><h2>无法打开这条历史记录</h2><p>${esc(state.replayError)}</p>
        <button class="secondary-btn" id="retry-answer">重新读取</button></div>`;
      $("retry-answer").onclick = route;
    }
    for (const turn of state.turns) { list.append(makeTurn(turn)); paintTurn(turn,true); }
    $("answer-scroll").scrollTop = state.scrollTop;
    list.onclick = event => {
      const article = event.target.closest("[data-answer-turn]");
      const turn = article && state.turns.find(item => String(item.key) === article.dataset.answerTurn);
      if (!turn) return;
      const opener = event.target.closest(".sources-button, [data-citation]");
      if (opener) openEvidence(turn.result, opener.dataset.citation || null, opener);
      if (event.target.closest("[data-retry-turn]") && !state.active) void query(turn.request.question, turn.request, turn);
    };
  }
  const field = $("question");
  field.oninput = () => { state.draft = field.value; };
  $("mode").onchange = e => { state.mode = e.target.value; };
  $("ask-form").onsubmit = event => { event.preventDefault(); void query(field.value); };
  $("stop-query").onclick = () => { stopQuery(); field.focus({preventScroll:true}); };
  field.onkeydown = event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault();
      if (!state.active) $("ask-form").requestSubmit();
    }
  };
  updateComposer();
}

function stopQuery() {
  if (!state.active) return;
  const {controller, turn} = state.active;
  state.active = null;
  controller.abort();
  turn.phase = "stopped";
  paintTurn(turn, true);
  updateComposer();
}

async function query(raw, extra = {}, retry = null) {
  const question = raw.trim();
  if (!question || state.active || state.reading || !canUseWorkspace()) return;
  closeEvidence(false);
  if (state.turns.at(-1)?.result.can_continue === false) { resetAsk(); renderAsk(); retry = null; }
  let turn;
  const parent = state.turns.findLast(item => item.phase === "complete" && item.result.id);
  if (retry && retry.phase !== "complete") {
    turn = retry;
    Object.assign(turn, {phase:"streaming", status:"正在查询…"});
    Object.assign(turn.result, {answer:"", citations:[]});
  } else {
    const body = {mode:state.mode, ...extra, question, request_id:requestId()};
    if (parent) body.parent_id = parent.result.id;
    turn = newTurn(body);
    state.turns.push(turn);
  }
  const controller = new AbortController();
  state.active = {controller, turn};
  state.replayError = "";
  if (!retry || state.draft.trim() === question) state.draft = "";
  const field = $("question");
  if (field) field.value = state.draft;
  if (location.hash.startsWith("#ask/")) history.pushState({chatKey:state.chatKey}, "", "#ask");
  else history.replaceState({chatKey:state.chatKey}, "", "#ask");
  if (!$("conversation") || !$("answer-scroll") || $("retry-answer")) renderAsk();
  else {
    if (!$("turn-" + turn.key)) $("conversation").append(makeTurn(turn));
    paintTurn(turn,true);
    updateComposer();
  }
  if (turn === state.turns.at(-1)) $("answer-scroll").scrollTop = $("answer-scroll").scrollHeight;
  else $("turn-" + turn.key).scrollIntoView({block:"start"});
  drawHistory();
  const current = () => state.active?.controller === controller;
  try {
    const response = await request("/api/query/stream", {...jsonOptions(turn.request),
      headers:{"Content-Type":"application/json", Accept:"text/event-stream"}, signal:controller.signal});
    for await (const {type, data} of events(response)) {
      if (!current()) return;
      if (type === "status") {
        turn.status = String(data.message || "正在查询…"); queueText(turn);
      } else if (type === "delta" && typeof data.text === "string") {
        turn.result.answer += data.text; queueText(turn);
      } else if (type === "evidence" && Array.isArray(data.citations)) {
        turn.result.citations = data.citations; paintTurn(turn,true); refreshEvidence(turn.result);
      } else if (type === "error") {
        throw new Error(data.message || "回答未完成，请重试。");
      } else if (type === "done" && data.result?.id) {
        Object.assign(turn.result, data.result);
        turn.phase = "complete";
        paintTurn(turn,true); refreshEvidence(turn.result);
        const latest = state.turns.findLast(item => item.phase === "complete");
        history.replaceState({chatKey:state.chatKey}, "", "#ask/" + encodeURIComponent(latest.result.id));
        state.active = null; updateComposer();
        void loadHistory();
        return;
      }
    }
    throw new Error("连接已中断。");
  } catch (error) {
    if (!current()) return;
    turn.phase = "failed";
    turn.status = error instanceof TypeError || error.name === "NetworkError" ? "连接已中断，可重新查询。" :
      error.message || "回答未完成，可重新查询。";
    if (!state.draft) state.draft = $("question").value = question;
    state.active = null;
    paintTurn(turn,true); updateComposer();
  }
}

async function replay(id) {
  stopQuery();
  const ticket = ++state.sequence;
  const requestedHash = location.hash;
  Object.assign(state, {turns:[], reading:true, replayError:"", draft:"", mode:"mixed", scrollTop:0, chatKey:++nextChatKey});
  renderAsk();
  try {
    const result = await api("/api/history/" + encodeURIComponent(id));
    if (ticket !== state.sequence || location.hash !== requestedHash) return;
    const {predecessors = [], ...last} = result;
    state.turns = [...predecessors, last].map(storedTurn);
    state.reading = false;
    renderAsk();
    const article = $("turn-" + state.turns.at(-1).key);
    article.scrollIntoView({block:"start"});
    article.focus({preventScroll:true});
  } catch (error) {
    if (ticket !== state.sequence || location.hash !== requestedHash || !canUseWorkspace()) return;
    Object.assign(state, {reading:false, replayError:error.message});
    renderAsk();
  }
}

function syncDrawer() {
  const open = smallScreen.matches && document.body.classList.contains("menu-open");
  $("sidebar").inert = smallScreen.matches && !open;
  $("shell").inert = open;
  $("open-menu").setAttribute("aria-expanded", String(open));
  $("nav-backdrop").hidden = !open;
  if (open) {
    $("sidebar").setAttribute("role", "dialog");
    $("sidebar").setAttribute("aria-modal", "true");
  } else {
    document.body.classList.remove("menu-open");
    $("sidebar").removeAttribute("role");
    $("sidebar").removeAttribute("aria-modal");
  }
}
function closeMenu(returnFocus = false) {
  document.body.classList.remove("menu-open");
  syncDrawer();
  if (returnFocus && smallScreen.matches) $("open-menu").focus();
}
function openMenu() {
  if (!smallScreen.matches || !canUseWorkspace()) return;
  document.body.classList.add("menu-open");
  syncDrawer();
  $("close-menu").focus();
}

function newQuestion() {
  stopQuery();
  if ($("answer-scroll")) state.scrollTop = $("answer-scroll").scrollTop;
  rememberDraft();
  const replace = location.hash === "#ask" && !state.turns.length && !state.draft;
  resetAsk();
  if (replace) history.replaceState({chatKey:state.chatKey}, "", "#ask");
  else history.pushState({chatKey:state.chatKey}, "", "#ask");
  void route();
  $("question")?.focus({preventScroll:true});
}

async function route() {
  if (!state.session) return;
  if ($("answer-scroll")) state.scrollTop = $("answer-scroll").scrollTop;
  stopQuery();
  if (state.page === "ask") rememberDraft();
  closeEvidence(false);
  closeMenu();
  const hash = location.hash.slice(1), match = /^ask\/(\d+)$/.exec(hash);
  const page = Object.hasOwn(pages, hash) ? hash : "ask";
  state.page = managementPages.has(page) && !canManage() ? "ask" : page;
  if (hash && !match && hash !== "history" && hash !== state.page) history.replaceState(null, "", "#" + state.page);
  document.title = pages[state.page] + " · 知据";
  if (!canUseWorkspace()) { showLogin(); return; }
  drawNavigation();
  if (state.page === "ask") {
    const cached = match ? [...drafts.values()].find(item => String(item.latestId) === match[1]) : drafts.get(history.state?.chatKey);
    if (cached) {
      state.sequence++;
      Object.assign(state, cached, {reading:false, replayError:""});
      renderAsk();
      return;
    }
    if (match) {
      if (String(state.turns.at(-1)?.result.id) === match[1]) renderAsk();
      else await replay(match[1]);
    } else {
      state.sequence++;
      state.reading = false;
      state.replayError = "";
      if (history.state?.chatKey !== state.chatKey) resetAsk();
      if (!state.chatKey) state.chatKey = ++nextChatKey;
      history.replaceState({chatKey:state.chatKey}, "", location.href);
      renderAsk();
      if (hash === "history") {
        if (smallScreen.matches) openMenu();
        $("history-search").focus();
      }
    }
    return;
  }
  state.sequence++;
  state.reading = false;
  const container = document.createElement("div");
  container.className = "workspace";
  $("main").replaceChildren(container);
  const ctx = {api, notify, canManage:canManage(), icon, esc, refresh:() => Promise.resolve()};
  try {
    if (state.page === "admin") await renderAdmin(container, ctx);
    else if (state.page === "retrieval") {
      const {renderRetrievalCheck} = await import("./retrieval-check.js");
      if (container.isConnected) await renderRetrievalCheck(container, ctx);
    }
    else if (state.page === "models") {
      const {renderModels} = await import("./model-settings.js");
      if (container.isConnected) await renderModels(container, ctx);
    }
    else await renderManagement(state.page, container, ctx);
  } catch (error) {
    if (!container.isConnected) return;
    container.innerHTML = `<div class="error-state" role="alert"><h2>加载失败</h2><p>${esc(error.message)}</p><button class="secondary-btn" id="retry-view">重新加载</button></div>`;
    $("retry-view").onclick = route;
  }
}

async function boot() {
  try {
    state.session = await api("/api/session");
    $("connection").hidden = true;
    void route();
    if (!smallScreen.matches && state.page === "ask" && !state.reading && !state.turns.length && location.hash !== "#history") {
      $("question")?.focus({preventScroll:true});
    }
    if (canUseWorkspace()) void loadHistory();
  } catch (error) {
    $("connection").hidden = false;
    $("main").innerHTML = `<section class="workspace error-state"><h1>工作区加载失败</h1><p>${esc(error.message)}</p><button class="secondary-btn" id="retry">重新连接</button></section>`;
    $("retry").onclick = boot;
  }
}

$("new-question").innerHTML = icon("compose",17) + "<span>新建问答</span>";
$("library-link").innerHTML = icon("book",18) + "<span>文档库</span>";
$("admin-link").innerHTML = icon("settings",18) + "<span>管理后台</span>" + icon("chevron",14);
$("search-icon").innerHTML = icon("search",15);
$("open-menu").innerHTML = icon("menu",20);
$("close-menu").innerHTML = icon("close",18);
$("history-search").oninput = searchHistory;
$("new-question").onclick = newQuestion;
$("open-menu").onclick = openMenu;
$("close-menu").onclick = () => closeMenu(true);
$("nav-backdrop").onclick = () => closeMenu(true);
$("skip-link").onclick = event => { event.preventDefault(); closeMenu(); $("main").focus(); };
$("sidebar").addEventListener("click", event => {
  const link = event.target.closest("a[href^='#']");
  if (link && link.hash === location.hash && smallScreen.matches) closeMenu(true);
});
document.addEventListener("keydown", event => {
  if (!document.body.classList.contains("menu-open")) return;
  if (event.key === "Escape") { event.preventDefault(); closeMenu(true); }
  if (event.key !== "Tab") return;
  const controls = [...$("sidebar").querySelectorAll("button:not(:disabled), input, a[href]")].filter(el => el.getClientRects().length);
  const first = controls[0], last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
smallScreen.addEventListener("change", syncDrawer);
syncDrawer();
window.addEventListener("hashchange", route);
void boot();
