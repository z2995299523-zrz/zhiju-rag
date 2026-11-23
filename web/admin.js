const historyPageSize = 5;
const metricDefinitions = [
  { key: "documents", label: "已导入文档", unit: "份", icon: "file" },
  { key: "chunks", label: "可检索片段", unit: "段", icon: "layers" },
  { key: "connectors", label: "数据接口", unit: "个", icon: "database" },
  { key: "queries", label: "累计查询", unit: "次", icon: "chat" },
];
const intentLabels = { documents: "文档检索", data: "接口查询", mixed: "综合查询" };
const statusLabels = { answered: "已回答", partial: "部分回答", no_evidence: "缺少证据", needs_input: "待补充", error: "查询失败" };
const stageLabels = { parse: "问题解析", parameters: "核对条件", data: "业务查数", retrieval: "文档检索", generation: "生成回答", archive: "保存历史" };
const stepResults = { ok: "完成", failed: "失败", stopped: "中断", empty: "无匹配结果", needs_input: "待补充条件", no_connector: "缺少匹配接口" };
const stepNotes = {
  verified_record_lookup: "模型段落未通过校验，已定位唯一记录并引用原件取值",
  local_route_fallback: "模型解析未完成，已使用本地规则", lexical_fallback: "向量排序未完成，已使用关键词排序",
  embedding_busy: "向量服务繁忙，已使用关键词排序", extractive_fallback: "模型未选出可用引文，已使用原文摘录",
};
const displayNumber = new Intl.NumberFormat("zh-CN");
const displayTime = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

export function managementNav(active) {
  const sections = [["admin", "知识库概览"], ["imports", "导入管理"], ["connectors", "数据接口配置"], ["models", "模型配置"], ["retrieval", "检索评测"]];
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

  const traceSection = document.createElement("section");
  traceSection.className = "adm-panel adm-traces";
  traceSection.innerHTML = `<header><h2>查询诊断</h2><button class="secondary-btn" type="button" data-refresh>刷新</button></header>
    <div class="adm-trace-list" aria-live="polite"><p class="adm-note">正在读取查询诊断…</p></div>`;
  shell.append(traceSection);
  const traceList = traceSection.querySelector(".adm-trace-list");
  const refreshTraces = traceSection.querySelector("[data-refresh]");
  const cursors = [null];

  function duration(ms) {
    return ms < 1 ? "小于 1 毫秒" : ms < 1000 ? Math.round(ms) + " 毫秒" : (ms / 1000).toFixed(2) + " 秒";
  }

  function traceRow(run) {
    const outcome = run.outcome === "stopped" ? "已中断" : statusLabels[run.outcome];
    const problem = run.stages.find(step => step.status === "failed" || step.status === "stopped");
    const labels = {candidate_count:"候选块数", lexical_ids:"词法前列", ranked_ids:"重排前列", excerpt_ids:"选取原文",
      embedding_batch_size:"本批条数", embedding_batches:"向量请求数", embedding_dimension:"向量维数",
      embedding_outcome:"向量结果", embedding_cache_hits:"向量缓存命中", first_upstream_ms:"首个上游增量（毫秒）",
      first_verified_ms:"首个核验段落（毫秒）", upstream_outcome:"生成结果", ranking_method:"排序方法"};
    const outcomes = {ranked:"语义命中", lexical:"关键词", lexical_fallback:"关键词回退", hybrid:"混合",
      below_threshold:"低于相似度门槛", insufficient:"依据不足", validation:"校验失败",
      invalid_response:"响应格式异常", timeout:"超时", transport:"连接异常", busy:"繁忙", complete:"完成"};
    const metrics = step => Object.entries(step.metrics || {}).filter(([key]) => labels[key]).map(([key,val]) => {
      const value = Array.isArray(val) ? val.join("、") || "无" : outcomes[val] || String(val);
      return `<small>${esc(labels[key])} · ${esc(value)}</small>`;
    }).join("");
    const steps = run.stages.map(step => `<li>
      <span>${esc(stageLabels[step.name])}</span><span>${esc(stepResults[step.status])}</span><span>${esc(duration(step.duration_ms))}</span>
      ${step.notes.map(note => `<small>${esc(stepNotes[note] || note)}</small>`).join("")}${metrics(step)}</li>`).join("");
    return `<details class="adm-trace">
      <summary><span class="adm-trace-name">执行 #${esc(run.id)} · ${esc(intentLabels[run.mode] || "自动判断")}</span>
        <span>${esc(timeLabel(run.started_at))}</span><span class="adm-trace-summary-status">${esc(outcome)}${problem ? " · " + esc(stageLabels[problem.name]) : ""}</span>
        <span>${esc(duration(run.duration_ms))}</span></summary>
      <ol>${steps}</ol><div class="adm-trace-links">
        ${run.request_id ? `<small>请求号 ${esc(run.request_id)}</small>` : ""}
        ${run.history_id ? `<a href="#ask/${encodeURIComponent(run.history_id)}">查看回答 #${esc(run.history_id)}</a>` : ""}</div></details>`;
  }

  async function loadTraces(pageIndex = 0) {
    refreshTraces.disabled = true;
    traceList.querySelectorAll("button").forEach(button => { button.disabled = true; });
    try {
      const before = cursors[pageIndex];
      const page = await api("/api/admin/query-traces?limit=5" + (before ? "&before=" + before : ""));
      traceList.innerHTML = page.items.map(traceRow).join("") || '<p class="adm-note">暂无查询诊断。新的问答执行结束后可在这里查看各阶段耗时。</p>';
      if (pageIndex || page.next_before) {
        traceList.insertAdjacentHTML("beforeend", `<div class="adm-pagination"><span>第 ${pageIndex + 1} 页</span>
          <button type="button" data-prev ${pageIndex ? "" : "disabled"}>上一页</button>
          <button type="button" data-next ${page.next_before ? "" : "disabled"}>下一页</button></div>`);
        traceList.querySelector("[data-prev]").onclick = () => loadTraces(pageIndex - 1);
        traceList.querySelector("[data-next]").onclick = () => {
          cursors[pageIndex + 1] = page.next_before;
          loadTraces(pageIndex + 1);
        };
      }
    } catch (error) {
      failure(traceList, error, () => loadTraces(pageIndex));
    } finally {
      refreshTraces.disabled = false;
    }
  }
  refreshTraces.onclick = () => loadTraces();

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

  await Promise.all([loadOverview(), loadDocuments(), loadHistory(0), loadTraces()]);
}
