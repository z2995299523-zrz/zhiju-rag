import { managementNav } from "./admin.js";

const candidateMethods = { all:"小库全量候选", fts5:"FTS5 全文召回" };
const rankingMethods = { lexical:"词法排序", hybrid:"词法与向量融合", lexical_fallback:"本次无向量命中，采用词法排序" };

function showResult(target, {metrics, top_chunks}, esc) {
  const item = metrics.evidence[0];
  const stage = item.stage === "hit" ? "期望证据进入前 " + metrics.top_k + " 名"
    : item.in_candidates ? "期望证据已召回，重排后未进入前 " + metrics.top_k + " 名" : "候选中缺少期望证据";
  const rank = item.rank ? "最佳名次为第 " + item.rank + " 名" : "排序结果中未命中";
  const position = item.expected.locator ? " · " + item.expected.locator : " · 整份文档";
  target.innerHTML = `<h2>${esc(stage)}</h2><p class="rc-target">${esc(item.expected.filename + position)}</p>
    <dl class="rc-stage-list"><div><dt>候选召回</dt><dd>${item.in_candidates ? "命中" : "未命中"} · ${metrics.candidate_count} 个候选分块 · ${esc(candidateMethods[metrics.candidate_method] || metrics.candidate_method)}</dd></div>
    <div><dt>重排</dt><dd>${esc(rank)} · ${metrics.ranked_count} 个分块保留 · ${esc(rankingMethods[metrics.ranking_method] || metrics.ranking_method)}</dd></div></dl>
    ${top_chunks.length ? `<h3>前 ${metrics.top_k} 名中的实际分块</h3><ol class="rc-ranking">${top_chunks.map(row => {
      const link = "/reader.html?" + new URLSearchParams({id:row.document_id, chunk:row.chunk_id});
      return `<li><a href="${esc(link)}" target="_blank" rel="noopener">${esc(row.filename)} · ${esc(row.locator)}</a><p>${esc(row.text)}</p></li>`;
    }).join("")}</ol>` : "<p>本次没有可展示的排序结果。</p>"}`;
}

export async function renderRetrievalCheck(container, {api, esc, canManage}) {
  if (!canManage) return;
  const page = document.createElement("section");
  page.className = "adm-page rc-page";
  page.innerHTML = `<header class="adm-title"><h1>检索评测</h1></header>${managementNav("retrieval")}
    <p class="rc-help">选择期望命中的文档或分块，检查它是否进入候选，以及重排后的名次。</p>
    <div data-content aria-live="polite">正在读取工作区文档…</div>`;
  container.replaceChildren(page);
  const docs = await api("/api/documents");
  if (!page.isConnected) return;
  const content = page.querySelector("[data-content]");
  content.removeAttribute("aria-live");
  if (!docs.length) {
    content.innerHTML = '<p>还没有可评测的文档。<a href="#imports">去导入文档</a></p>';
    return;
  }
  content.innerHTML = `<form class="rc-form">
    <label class="model-field">评测问题<textarea name="question" rows="2" maxlength="2000" required></textarea></label>
    <label class="model-field">期望命中的文档<select name="doc" required></select></label>
    <div data-search hidden><label class="model-field">按原文或位置查找分块<input name="search" type="search" maxlength="256"></label></div>
    <label class="model-field">期望命中的分块<select name="chunk"></select></label>
    <div class="rc-pager" data-pager hidden><button type="button" data-prev>上一页</button><span data-page></span><button type="button" data-next>下一页</button></div>
    <div class="rc-actions"><label class="model-field">检查名次<select name="k"><option value="3">前 3 名</option><option value="5">前 5 名</option><option value="8">前 8 名</option></select></label>
      <button type="submit" class="primary-btn">运行评测</button></div>
    <p data-error role="alert" tabindex="-1"></p><button data-retry type="button" class="secondary-btn" hidden>重新加载分块</button>
    </form><div data-result role="region" aria-live="polite" aria-label="评测结果" class="rc-result"></div>`;
  const form = content.querySelector("form");
  const {question, doc, chunk, search, k} = form.elements;
  const submit = form.querySelector("[type=submit]");
  const errorBox = form.querySelector("[data-error]");
  const retry = form.querySelector("[data-retry]");
  const result = content.querySelector("[data-result]");
  const pageSize = 30;
  let offset = 0, searchTimer, previewRequest, evaluationRequest;
  for (const file of docs) doc.add(new Option(file.filename, file.id));
  function resetResult() {
    evaluationRequest?.abort();
    evaluationRequest = null;
    result.replaceChildren();
    errorBox.textContent = "";
    submit.disabled = chunk.disabled;
  }
  async function loadChunks(start = 0) {
    clearTimeout(searchTimer);
    previewRequest?.abort();
    const req = previewRequest = new AbortController();
    resetResult();
    const current = docs.find(file => String(file.id) === doc.value);
    form.querySelector("[data-search]").hidden = current.chunk_count <= pageSize;
    chunk.replaceChildren(new Option("整份文档（任一分块）", ""));
    submit.disabled = chunk.disabled = true;
    retry.hidden = true;
    const pager = form.querySelector("[data-pager]");
    pager.hidden = true;
    const params = new URLSearchParams({limit:pageSize, offset:start, q:search.value.trim()});
    try {
      const data = await api("/api/documents/" + doc.value + "/preview?" + params, {signal:req.signal});
      if (req.signal.aborted || !page.isConnected) return;
      offset = data.offset;
      for (const row of data.chunks) chunk.add(new Option(row.locator + " · " + row.text.slice(0, 48), row.id));
      pager.hidden = data.total <= pageSize;
      form.querySelector("[data-page]").textContent = "第 " + (1 + Math.floor(offset / pageSize)) + " / " + Math.ceil(data.total / pageSize) + " 页";
      form.querySelector("[data-prev]").disabled = offset === 0;
      form.querySelector("[data-next]").disabled = offset + pageSize >= data.total;
      if (!data.total && search.value.trim()) errorBox.textContent = "未找到包含该文字的分块，请调整查找条件。";
      submit.disabled = chunk.disabled = false;
    } catch (error) {
      if (!req.signal.aborted && page.isConnected) { errorBox.textContent = error.message; retry.hidden = false; }
    }
  }
  doc.onchange = () => { search.value = ""; void loadChunks(); };
  chunk.onchange = k.onchange = question.oninput = resetResult;
  search.oninput = () => {
    previewRequest?.abort();
    resetResult();
    submit.disabled = chunk.disabled = true;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void loadChunks(), 200);
  };
  form.querySelector("[data-prev]").onclick = () => void loadChunks(Math.max(0, offset - pageSize));
  form.querySelector("[data-next]").onclick = () => void loadChunks(offset + pageSize);
  retry.onclick = () => void loadChunks();
  form.onsubmit = async event => {
    event.preventDefault();
    if (!question.value.trim()) { question.setCustomValidity("请填写评测问题"); question.reportValidity(); question.setCustomValidity(""); return; }
    if (submit.disabled) return;
    resetResult();
    const req = evaluationRequest = new AbortController();
    submit.disabled = true;
    result.textContent = "正在检索并核对期望证据…";
    try {
      const body = {question:question.value.trim(), document_id:Number(doc.value), top_k:Number(k.value)};
      if (chunk.value) body.chunk_id = Number(chunk.value);
      const response = await api("/api/admin/retrieval/evaluate", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), signal:req.signal});
      if (!req.signal.aborted && page.isConnected) showResult(result, response, esc);
    } catch (error) {
      if (!req.signal.aborted && page.isConnected) { result.replaceChildren(); errorBox.textContent = error.message; errorBox.focus(); }
    } finally {
      if (!req.signal.aborted) submit.disabled = false;
    }
  };
  await loadChunks();
}
