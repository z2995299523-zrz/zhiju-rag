const historyPageSize = 5;
const metricDefinitions = [
  { key: "documents", label: "已导入文档", unit: "份", icon: "file" },
  { key: "chunks", label: "可检索片段", unit: "段", icon: "layers" },
  { key: "connectors", label: "数据接口", unit: "个", icon: "database" },
  { key: "queries", label: "累计查询", unit: "次", icon: "chat" },
];
const intentLabels = { documents: "文档检索", data: "接口查询", mixed: "综合查询" };
const statusLabels = { answered: "已回答", partial: "部分回答", no_evidence: "缺少证据", needs_input: "待补充", error: "查询失败" };
const displayNumber = new Intl.NumberFormat("zh-CN");
const displayTime = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

export function managementNav(active) {
  const sections = [["admin", "知识库概览"], ["imports", "导入管理"], ["connectors", "数据接口配置"], ["models", "模型配置"]];
  return `<nav class="adm-nav" aria-label="管理功能">${sections.map(([route, label]) =>
    `<a href="#${route}"${active === route ? ' aria-current="page"' : ""}>${label}</a>`).join("")}</nav>`;
}

function timeLabel(value) {
  const date = new Date(value);
  return Number.isNaN(+date) ? "—" : displayTime.format(date);
}

export async function renderAdmin(container, ctx) {
  if (!ctx.canManage) {
    container.textContent = "知识库概览需要管理员权限。";
    return;
  }
  const { api, esc, icon } = ctx;
  const shell = document.createElement("section");
  shell.className = "adm-page";
  shell.innerHTML = `<header class="adm-title"><h1>知识库管理</h1></header>
  ${managementNav("admin")}
  <section aria-label="知识库统计" class="adm-stats" aria-live="polite"><p class="adm-note">正在读取统计…</p></section>
  <div class="adm-panels">
    <section class="adm-panel"><header><h2>最近导入</h2><a href="#imports">全部文档 ${icon("chevron", 13)}</a></header>
      <div class="adm-docs" aria-live="polite"><p class="adm-note">正在读取文档…</p></div></section>
    <section class="adm-panel"><header><h2>工作区查询记录</h2>${icon("clock", 17)}</header>
      <div class="adm-history" aria-live="polite"><p class="adm-note">正在读取记录…</p></div></section>
  </div>`;
  container.replaceChildren(shell);
  const stats = shell.querySelector(".adm-stats");
  const documents = shell.querySelector(".adm-docs");
  const history = shell.querySelector(".adm-history");
  let queryTotal = null;
  let historyOffset = 0;
  let historyItems = [];

  function failure(target, error, retry) {
    target.innerHTML = `<p class="adm-note" role="alert">${esc(error.message || "读取失败")} <button type="button">重试</button></p>`;
    target.querySelector("button").onclick = retry;
  }

  async function loadOverview() {
    try {
      const overview = await api("/api/overview");
      queryTotal = overview.queries;
      stats.innerHTML = metricDefinitions.map(({ key, label, unit, icon: symbol }) =>
        `<div class="adm-metric"><span>${icon(symbol, 16)} ${label}</span><strong>${esc(displayNumber.format(overview[key]))}<small>${unit}</small></strong></div>`).join("");
      if (historyItems.length) drawHistory();
    } catch (error) {
      failure(stats, error, loadOverview);
    }
  }

  async function loadDocuments() {
    try {
      const rows = (await api("/api/documents?limit=5")).slice(0, 5);
      documents.innerHTML = rows.length ? rows.map(doc => `<a class="adm-row" href="/reader.html?id=${encodeURIComponent(doc.id)}" target="_blank" rel="noopener">
        <span class="adm-symbol">${icon("file", 17)}</span><span class="adm-row-text">
          <strong>${esc(doc.filename)}</strong><small>${esc(timeLabel(doc.created_at))} · ${esc(doc.chunk_count)} 个片段</small></span>
        ${icon("chevron", 14)}</a>`).join("") : '<p class="adm-note">还没有导入文档。</p>';
    } catch (error) {
      failure(documents, error, loadDocuments);
    }
  }

  function drawHistory() {
    const rows = historyItems.map(query => `<a class="adm-row" href="#ask/${encodeURIComponent(query.id)}">
      <span class="adm-row-text"><strong>${esc(query.question)}</strong><small>${esc(timeLabel(query.created_at))} · ${esc(intentLabels[query.intent] || query.intent)}</small></span>
      <span class="adm-query-status">${esc(statusLabels[query.status] || query.status)}</span></a>`).join("");
    history.innerHTML = rows || '<p class="adm-note">暂无查询记录。</p>';
    const hasNext = queryTotal === null ? historyItems.length === historyPageSize : historyOffset + historyItems.length < queryTotal;
    if (historyOffset === 0 && !hasNext) return;
    history.insertAdjacentHTML("beforeend", `<div class="adm-pagination"><span>第 ${Math.floor(historyOffset / historyPageSize) + 1} 页</span>
      <button type="button" data-prev ${historyOffset === 0 ? "disabled" : ""}>上一页</button>
      <button type="button" data-next ${hasNext ? "" : "disabled"}>下一页</button></div>`);
    history.querySelector("[data-prev]").onclick = () => loadHistory(historyOffset - historyPageSize);
    history.querySelector("[data-next]").onclick = () => loadHistory(historyOffset + historyPageSize);
  }

  async function loadHistory(offset) {
    history.querySelectorAll("button").forEach(button => { button.disabled = true; });
    try {
      historyItems = await api("/api/history?scope=workspace&limit=" + historyPageSize + "&offset=" + offset);
      historyOffset = offset;
      drawHistory();
    } catch (error) {
      historyItems = [];
      failure(history, error, () => loadHistory(offset));
    }
  }

  await Promise.all([loadOverview(), loadDocuments(), loadHistory(0)]);
}
