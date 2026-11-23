// Evidence always belongs to the saved answer. Opening a source never runs retrieval.
const escapeText = value => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[character]);
const validId = value => /^\d+$/.test(String(value)) && Number.isSafeInteger(Number(value)) && Number(value) > 0;
let panel;
let preview;
let activeResult;
let returnFocus;
let activeTurn;

function glyph(name, size = 18) {
  const paths = {
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    document: '<path d="M5 3h9l5 5v13H5zM14 3v5h5M8 12h8m-8 4h6"/>',
    api: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0v-7"/>',
    book: '<path d="M3 5h5a5 5 0 0 1 4 2 5 5 0 0 1 4-2h5v14h-5a5 5 0 0 0-4 2 5 5 0 0 0-4-2H3zM12 7v14"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>'
  };
  return `<svg aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${paths[name]}</svg>`;
}

function timestamp(value) {
  const time = value && new Date(value);
  if (!time || !Number.isFinite(time.getTime())) return "未记录查询时间";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23"
  }).format(time);
}

function documentBody(source) {
  const excerpt = source.excerpt || source.text;
  const location = source.locator || "未记录页码、章节或行号";
  let link = '<p class="ev-missing">缺少文档标识，无法打开原文。</p>';
  if (validId(source.document_id)) {
    const query = new URLSearchParams({id: source.document_id});
    if (validId(source.chunk_id)) query.set("chunk", source.chunk_id);
    link = `<a class="ev-read" href="/reader.html?${escapeText(query)}" target="_blank" rel="noopener">${glyph("book", 16)} 打开文档 ${glyph("arrow", 14)}</a>`;
  }
  return `<p class="ev-locator">${escapeText(location)}</p>
    ${excerpt ? `<blockquote><mark>${escapeText(excerpt)}</mark></blockquote>` : '<p class="ev-missing">未保存原文片段。</p>'}${link}`;
}

function apiBody(source, result) {
  const filters = source.filters;
  const label = filters == null ? "未记录可展示的筛选条件" : !filters.length ? "无可展示的筛选条件" : "";
  const visibleFilters = label ? escapeText(label) : filters.map(item =>
    `<span class="ev-filter">${escapeText(item.label)} · ${escapeText(item.value)}</span>`).join("");
  const path = `/api/history/${encodeURIComponent(result.id)}/sources/${encodeURIComponent(source.id)}`;
  const rows = Array.isArray(source.rows) ? source.rows : null;
  const number = source.total_count ?? rows?.length;
  const details = rows?.length ? `<details class="ev-records"><summary>查看记录快照</summary>
    ${rows.slice(0, 3).filter(row => row && typeof row === "object").map(row => `<dl>${Object.entries(row).map(([k,v]) =>
      `<div><dt>${escapeText(k)}</dt><dd>${escapeText(typeof v === "object" ? JSON.stringify(v) : v)}</dd></div>`).join("")}</dl>`).join("")}
    ${rows.length > 3 ? '<p>此处展示前 3 条记录。</p>' : ""}</details>` : "";
  const sql = typeof source.executed_sql === "string" && source.executed_sql.trim();
  return `<dl class="ev-api-meta"><dt>查询时间</dt><dd>${escapeText(timestamp(source.fetched_at))}</dd>
    ${source.metric_definition ? `<dt>指标口径</dt><dd>${escapeText(source.metric_definition)}</dd>` : ""}
    <dt>筛选条件</dt><dd>${visibleFilters}</dd>${Number.isInteger(number) ? `<dt>返回记录</dt><dd>${number} 条</dd>` : ""}</dl>
    ${details}${sql ? `<details class="ev-records"><summary>查看执行 SQL</summary><pre><code>${escapeText(sql)}</code></pre></details>` : ""}
    ${source.redacted ? '<p class="ev-missing">部分受限字段已隐藏。</p>' : ""}
    ${validId(result.id) && source.id && rows ? `<div class="ev-downloads">${[["rows","下载记录"],["raw","下载原始响应"]].map(([format, label]) =>
      `<button type="button" data-download="${path}/${format}" data-filename="query-${result.id}-${escapeText(source.id)}-${format}.json">${label}</button>`).join("")}</div>
      <p class="ev-download-status" role="status"></p>` : ""}`;
}

async function downloadSnapshot(button) {
  const status = button.closest(".ev-card-body").querySelector(".ev-download-status");
  button.disabled = true;
  status.textContent = "正在读取快照…";
  try {
    const response = await fetch(button.dataset.download, {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "快照读取失败，请重试。");
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    if (button.isConnected) {
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = button.dataset.filename;
      link.click();
      status.textContent = "";
    }
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  } catch (error) {
    status.textContent = error.message || "快照读取失败，请重试。";
  } finally {
    button.disabled = false;
  }
}

function sourceCard(source, index, current) {
  const api = source.kind === "api";
  const type = api ? (source.demo ? "演示接口" : "数据接口") : source.kind === "document" ? "文档" : "来源";
  const title = source.title || (api ? "未记录接口名称" : "未记录文件名");
  return `<details class="ev-source" data-source-id="${escapeText(source.id)}" ${current ? "open" : ""}>
    <summary><span class="ev-kind">${glyph(api ? "api" : "document", 17)} ${type}</span>
    <span class="ev-number">[${index + 1}]</span>
    <strong>${escapeText(title)}</strong><span class="ev-chevron">${glyph("down", 15)}</span></summary>
    <div class="ev-card-body">${api ? apiBody(source, activeResult) : source.kind === "document" ? documentBody(source) : '<p class="ev-missing">缺少来源详情。</p>'}</div></details>`;
}

function updateButtons() {
  const expanded = new Set(Array.from(panel?.querySelectorAll(".ev-source[open]") || [], item => item.dataset.sourceId));
  document.querySelectorAll("[data-citation]").forEach(button => {
    const selected = panel?.open && button.closest("[data-answer-turn]")?.dataset.answerTurn === activeTurn
      && expanded.has(button.dataset.citation);
    button.setAttribute("aria-expanded", String(Boolean(selected)));
  });
  if (returnFocus && !returnFocus.hasAttribute("data-citation")) {
    returnFocus.setAttribute("aria-expanded", String(Boolean(panel?.open)));
  }
}

function clickedBackdrop(event, dialog) {
  if (event.target !== dialog) return false;
  const {left, right, top, bottom} = dialog.getBoundingClientRect();
  return event.clientX < left || event.clientX > right || event.clientY < top || event.clientY > bottom;
}

function mountPanel() {
  if (panel) return;
  panel = document.createElement("dialog");
  panel.className = "ev-panel";
  panel.id = "evidence-panel";
  panel.setAttribute("aria-labelledby", "ev-title");
  panel.addEventListener("cancel", event => { event.preventDefault(); closeEvidence(); });
  panel.addEventListener("click", event => {
    if (clickedBackdrop(event, panel)) { closeEvidence(); return; }
    const download = event.target.closest("[data-download]");
    if (download) void downloadSnapshot(download);
  });
  panel.addEventListener("toggle", event => {
    if (event.target.matches(".ev-source")) updateButtons();
  }, true);
  document.body.append(panel);
}

export function openEvidence(result, sourceId, opener) {
  mountPanel();
  if (returnFocus && returnFocus !== opener) returnFocus.setAttribute("aria-expanded", "false");
  returnFocus = opener || document.activeElement;
  activeTurn = returnFocus.closest("[data-answer-turn]")?.dataset.answerTurn;
  activeResult = result;
  const sources = result.citations || [];
  const selected = sourceId == null ? 0 : sources.findIndex(source => String(source.id) === String(sourceId));
  panel.innerHTML = `<header class="ev-head"><h2 id="ev-title">答案依据</h2><span class="ev-count">${sources.length} 条</span>
    <button type="button" class="ev-close" aria-label="关闭答案依据">${glyph("close")}</button></header>
    <div class="ev-content">${sources.map((source, i) => sourceCard(source, i, i === selected)).join("") ||
      '<p class="ev-missing">本次回答未记录可查证的来源。</p>'}</div>`;
  const closer = panel.querySelector(".ev-close");
  closer.onclick = () => closeEvidence();
  document.body.classList.add("evidence-open");
  if (!panel.open) panel.showModal();
  const entry = panel.querySelector(".ev-source[open] summary");
  (sourceId != null && entry ? entry : closer).focus({preventScroll:true});
  if (entry && sourceId != null) {
    const list = panel.querySelector(".ev-content");
    const item = entry.getBoundingClientRect();
    const viewport = list.getBoundingClientRect();
    if (item.top < viewport.top || item.bottom > viewport.bottom) list.scrollTop += item.top - viewport.top;
  }
  updateButtons();
}

export function refreshEvidence(result) {
  if (!panel?.open || result !== activeResult) return;
  const list = panel.querySelector(".ev-content");
  const scrollTop = list.scrollTop;
  const expanded = new Set(Array.from(list.querySelectorAll(".ev-source[open]"), el => el.dataset.sourceId));
  const focused = list.contains(document.activeElement) ? document.activeElement.closest(".ev-source")?.dataset.sourceId : null;
  const sources = result.citations || [];
  list.innerHTML = sources.map((source, i) => sourceCard(source, i, expanded.has(String(source.id)))).join("");
  panel.querySelector(".ev-count").textContent = sources.length + " 条";
  if (focused) Array.from(list.querySelectorAll(".ev-source")).find(el => el.dataset.sourceId === focused)
    ?.querySelector("summary")?.focus({preventScroll:true});
  list.scrollTop = scrollTop;
  updateButtons();
}

function closePreview(focus = true) {
  if (!preview) return;
  const {dialog, opener} = preview;
  preview = null;
  dialog.close();
  dialog.remove();
  if (focus && opener?.isConnected) opener.focus({preventScroll:true});
}

export function closeEvidence(focus = true) {
  closePreview(false);
  if (!panel?.open) return;
  panel.close();
  panel.replaceChildren();
  document.body.classList.remove("evidence-open");
  updateButtons();
  if (focus) {
    const turn = activeTurn && document.querySelector(`[data-answer-turn="${CSS.escape(activeTurn)}"]`);
    const marker = returnFocus?.dataset.citation;
    const target = returnFocus?.isConnected ? returnFocus : turn?.querySelector(marker ?
      `[data-citation="${CSS.escape(marker)}"]` : ".sources-button");
    target?.focus({preventScroll:true});
  }
  activeResult = returnFocus = activeTurn = null;
}

document.addEventListener("click", event => {
  if (event.defaultPrevented || event.button || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
  const opener = event.target.closest?.("a[href]");
  if (!opener || opener.hasAttribute("download") || opener.closest(".ev-preview")) return;
  const url = new URL(opener.href);
  if (url.origin !== location.origin || url.pathname !== "/reader.html" || !validId(url.searchParams.get("id"))) return;
  event.preventDefault();
  closePreview(false);
  const dialog = document.createElement("dialog");
  dialog.className = "ev-preview";
  dialog.setAttribute("aria-labelledby","ev-preview-title");
  dialog.innerHTML = `<header class="ev-preview-head"><h2 id="ev-preview-title">${glyph("book", 17)} 文档阅读</h2>
    <a class="ev-read" target="_blank" rel="noopener">独立打开 ↗</a>
    <button type="button" class="ev-close" aria-label="关闭文档预览">${glyph("close")}</button></header>`;
  dialog.querySelector("a").href = url.href;
  const frame = document.createElement("iframe");
  frame.title = "文档预览";
  // A browser may suppress the iframe load event when framing is denied.
  // Keep the standalone link available until the same-origin reader is ready.
  frame.hidden = true;
  const notice = document.createElement("p");
  notice.className = "ev-preview-notice";
  notice.setAttribute("role", "status");
  notice.textContent = "若弹窗未显示正文，请点击“独立打开”查看文档。";
  url.searchParams.set("embedded","1");
  frame.src = url;
  frame.addEventListener("load", () => {
    if (frame.contentDocument?.getElementById("reader")) {
      frame.hidden = false;
      notice.remove();
    }
  });
  dialog.querySelector("button").onclick = () => closePreview();
  dialog.addEventListener("cancel", e => { e.preventDefault(); closePreview(); });
  dialog.addEventListener("click", e => {
    if (clickedBackdrop(e, dialog)) closePreview();
  });
  dialog.append(notice, frame);
  document.body.append(dialog);
  preview = { dialog, opener, frame };
  dialog.showModal();
  dialog.querySelector("button").focus();
});

window.addEventListener("message", event => {
  if (preview && event.origin === location.origin && event.source === preview.frame.contentWindow && event.data === "reader:close") closePreview();
});
