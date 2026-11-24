import { managementNav } from "./admin.js";
import { compareChunks } from "./chunk-compare.js";
import { batchDescription, optionsForm, readOptions, warningsOf } from "./import-options.js";

const pageSize = 25;

export function jsonRequest(method, data) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) };
}

function messageOf(error) {
  return error?.message || String(error || "请求失败");
}

function formatSize(bytes) {
  if (!Number.isFinite(Number(bytes))) return "—";
  if (bytes < 1024) return bytes + " B";
  return bytes < 1024 * 1024 ? (bytes / 1024).toFixed(1) + " KB" : (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function formatDate(raw) {
  if (!raw) return "—";
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? String(raw) : date.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false
  });
}

function tokens(input) {
  return [...new Set(String(input || "").split(/[,，、\n]/u).map(s => s.trim()).filter(Boolean))];
}

function typeName(type) {
  return ({ string: "文本", integer: "整数", number: "数值", date: "日期" })[type] || "文本";
}

export function makeDialog(shell, ctx, title, body, className = "") {
  const el = document.createElement("dialog");
  el.className = "mg-dialog " + className;
  el.setAttribute("aria-label", title);
  el.innerHTML = `<div class="mg-modal-heading"><h2>${ctx.esc(title)}</h2>
    <button type="button" class="mg-icon-btn" data-close aria-label="关闭">${ctx.icon("close")}</button></div>
    <div class="mg-modal-content">${body}</div>`;
  shell.append(el);
  el.querySelector("[data-close]").addEventListener("click", () => el.close());
  el.addEventListener("close", () => el.remove(), { once: true });
  el.addEventListener("click", e => {
    if (e.target !== el) return;
    const rect = el.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) el.close();
  });
  el.showModal();
  return el;
}

function showError(element, value) {
  element.textContent = messageOf(value);
  element.hidden = false;
}

export function bindCancel(dialog) {
  const btn = dialog.querySelector("[data-cancel]");
  if (btn) btn.addEventListener("click", () => dialog.close());
}

function safeRowId(item) {
  return encodeURIComponent(String(item.id));
}

function confirmRemoval(shell, ctx, item, isDoc, reload) {
  const title = isDoc ? "删除文档" : "删除数据接口";
  const name = isDoc ? item.filename : item.name;
  const impact = isDoc ? "删除后，该文档将退出后续检索。历史问答中引用的原件仍可核验。" : "删除后，后续查询将无法再调用该接口。";
  const dialog = makeDialog(shell, ctx, title, `
    <p class="mg-confirm-text">确认删除 <strong>${ctx.esc(name)}</strong>？${impact}</p>
    <div class="mg-error" role="alert" hidden></div>
    <div class="mg-actions"><button type="button" class="mg-btn" data-cancel>取消</button>
      <button type="button" class="mg-btn mg-danger" data-submit>确认删除</button></div>`, "mg-dialog-small");
  bindCancel(dialog);
  const submit = dialog.querySelector("[data-submit]");
  submit.addEventListener("click", async () => {
    submit.disabled = true;
    try {
      await ctx.api("/api/" + (isDoc ? "documents/" : "connectors/") + safeRowId(item), { method: "DELETE" });
      dialog.close();
      ctx.notify("已删除“" + name + "”");
      await reload();
    } catch (error) {
      showError(dialog.querySelector(".mg-error"), error);
      submit.disabled = false;
    }
  });
}

function openEditor(shell, ctx, item, reload) {
  const c = item || {};
  const { esc } = ctx;
  const dialog = makeDialog(shell, ctx, item ? "编辑数据接口" : "配置数据接口", `
    <form class="mg-editor">
      <div class="mg-fields">
        <label>接口名称<input required name="name" value="${esc(c.name || "")}" placeholder="如：科研项目预算"></label>
        <label>用途描述<input name="description" value="${esc(c.description || "")}" placeholder="如：查询项目年度预算和执行率"></label>
      </div>
      <label>查询地址<div class="mg-url-input"><span>GET</span><input required name="url"
        value="${esc(c.url || "")}" spellcheck="false" placeholder="https://api.example.com/budget"></div>
        <small>外部接口的主机和端口需在服务端 RAG_ALLOWED_HOSTS 中启用。</small></label>
      <label>意图关键词<input name="keywords" value="${esc((c.keywords || []).join("，"))}" placeholder="预算，执行率，经费">
        <small>使用逗号分隔。问答匹配到这些词时可选择此接口。</small></label>
      <div class="mg-fields">
        <label>返回记录路径<input name="records_path" value="${esc(c.records_path || "")}" placeholder="如：data.records" spellcheck="false">
          <small>填写 JSON 中记录数组的位置。根数组可留空。</small></label>
        <label>令牌环境变量<input name="token_env" value="${esc(c.token_env || "")}" placeholder="如：RAG_CONNECTOR_FINANCE_TOKEN" spellcheck="false">
          <small>按需填写服务端保存令牌的变量名。</small></label>
      </div>
      <div class="mg-subheading"><h3>查询参数</h3><button class="mg-btn mg-small" type="button" data-add-param>${ctx.icon("plus", 14)}添加参数</button></div>
      <div class="mg-param-list"></div>
      <details><summary>业务口径与响应规则</summary>
        <label>指标定义（JSON 数组）<textarea name="metrics" rows="3" spellcheck="false" placeholder='[{"field":"库存量","label":"库存","unit":"件","additive":true,"definition":"在库实物数量"}]'>${esc(c.metrics ? JSON.stringify(c.metrics, null, 2) : "")}</textarea></label>
        <label>响应规则（JSON 对象）<textarea name="response_contract" rows="3" spellcheck="false" placeholder='{"success_path":"code","success_value":0,"total_path":"data.total","has_more_path":"data.has_more"}'>${esc(c.response_contract ? JSON.stringify(c.response_contract, null, 2) : "")}</textarea></label>
        <small>当接口承诺按条件一次返回全部数据时，可填写 {"complete":true}。</small>
      </details>
      <div class="mg-error" role="alert" hidden></div>
      <div class="mg-actions"><button type="button" class="mg-btn" data-cancel>取消</button>
        <button type="submit" class="mg-btn mg-primary">保存接口</button></div>
    </form>`);
  bindCancel(dialog);
  const form = dialog.querySelector("form");
  const list = dialog.querySelector(".mg-param-list");

  function addParam(p = {}) {
    const row = document.createElement("fieldset");
    row.className = "mg-param";
    row.innerHTML = `<legend class="mg-sr-only">查询参数</legend>
      <label>请求字段<input required name="param_name" aria-label="请求字段" value="${esc(p.name || "")}" placeholder="project"></label>
      <label>中文名称<input required name="param_label" aria-label="中文名称" value="${esc(p.label || "")}" placeholder="项目名称"></label>
      <label>类型<select name="param_type" aria-label="参数类型">${["string", "integer", "number", "date"].map(t =>
        `<option value="${t}"${(p.type || "string") === t ? " selected" : ""}>${typeName(t)}</option>`).join("")}</select></label>
      <label class="mg-aliases">别名<input name="param_aliases" value="${esc((p.aliases || []).join("，"))}" placeholder="用逗号分隔"></label>
      <label>可选文本值<input name="param_choices" value="${esc((p.choices || []).join("，"))}" placeholder="如：研发一部，研发二部"></label>
      <label class="mg-check"><input type="checkbox" name="param_required"${p.required ? " checked" : ""}>必填</label>
      <button type="button" class="mg-icon-btn" aria-label="移除参数" title="移除参数">${ctx.icon("trash", 16)}</button>`;
    row.querySelector("button").addEventListener("click", () => row.remove());
    list.append(row);
    return row;
  }
  for (const param of c.parameters || []) addParam(param);
  form.querySelector("[data-add-param]").addEventListener("click", () => addParam().querySelector("input").focus());
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    if (submit.disabled) return;
    const error = form.querySelector(".mg-error");
    error.hidden = true;
    const payload = {};
    for (const key of ["name", "description", "url", "records_path", "token_env"]) payload[key] = form.elements.namedItem(key).value.trim();
    payload.keywords = tokens(form.elements.keywords.value);
    try {
      for (const key of ["metrics", "response_contract"]) {
        const value = form.elements.namedItem(key).value.trim();
        if (value) payload[key] = JSON.parse(value);
      }
    } catch {
      showError(error, new Error("指标定义或响应规则的 JSON 格式有误。"));
      return;
    }
    payload.parameters = [...list.children].map(row => {
      const field = key => row.querySelector('[name="param_' + key + '"]');
      return { name: field("name").value.trim(), label: field("label").value.trim(), type: field("type").value,
        required: field("required").checked, aliases: tokens(field("aliases").value), choices: tokens(field("choices").value) };
    });
    if (new Set(payload.parameters.map(p => p.name)).size !== payload.parameters.length) {
      showError(error, "请求字段名重复，请分别命名。");
      return;
    }
    submit.disabled = true;
    try {
      await ctx.api(item ? "/api/connectors/" + safeRowId(item) : "/api/connectors", jsonRequest(item ? "PUT" : "POST", payload));
      dialog.close();
      ctx.notify(item ? "接口配置已更新" : "接口已添加");
      await reload();
    } catch (err) {
      showError(error, err);
      submit.disabled = false;
    }
  });
}

function openTest(shell, ctx, item) {
  const { esc } = ctx;
  const parameters = item.parameters || [];
  const dialog = makeDialog(shell, ctx, "测试接口 · " + item.name, `
    <p class="mg-test-url"><span class="mg-method">GET</span><code>${esc(item.url)}</code></p>
    <form class="mg-editor"><div class="mg-fields">${parameters.map((p, i) => `<label>
      ${esc(p.label || p.name)}${p.required ? " *" : ""}<input name="q${i}" aria-label="${esc(p.label || p.name)}"
        type="${p.type === "date" ? "date" : ["integer", "number"].includes(p.type) ? "number" : "text"}"
        ${p.type === "number" ? 'step="any"' : ""} ${p.required ? "required" : ""} placeholder="${esc(p.name)}"></label>`).join("")}</div>
      <div class="mg-error" role="alert" hidden></div>
      <div class="mg-actions"><button type="button" class="mg-btn" data-cancel>关闭</button>
        <button type="submit" class="mg-btn mg-primary">${ctx.icon("play", 14)}执行测试</button></div>
    </form><section class="mg-test-result" aria-live="polite" hidden></section>`);
  bindCancel(dialog);
  const form = dialog.querySelector("form");
  const result = dialog.querySelector(".mg-test-result");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    if (submit.disabled) return;
    const params = {};
    parameters.forEach((p, i) => {
      const raw = form.elements["q" + i].value.trim();
      if (raw) params[p.name] = raw;
    });
    submit.disabled = true;
    const error = form.querySelector(".mg-error");
    error.hidden = true;
    result.textContent = "正在执行接口测试…";
    result.hidden = false;
    try {
      const data = await ctx.api("/api/connectors/" + safeRowId(item) + "/test", jsonRequest("POST", { params }));
      const rows = data.rows || [];
      const preview = rows.slice(0, 20);
      const countLabel = data.total_count == null ? `本次 ${rows.length} 条` : `本次 ${rows.length} 条，共 ${data.total_count} 条`;
      result.innerHTML = `<div class="mg-subheading"><h3>返回结果</h3><span>${esc(countLabel)}</span></div>
        <small>${esc(formatDate(data.fetched_at))} · ${esc(data.request || item.url)}</small>
        ${rows.length > preview.length ? "<p>预览前 " + preview.length + " 条记录</p>" : ""}
        ${data.has_more === true ? "<p>仍有后续记录</p>" : ""}
        <pre>${esc(JSON.stringify(preview, null, 2))}</pre>`;
      result.hidden = false;
    } catch (err) {
      result.hidden = true;
      showError(error, err);
    }
    finally { submit.disabled = false; }
  });
}

export async function renderManagement(kind, container, ctx) {
  const { esc, icon, canManage } = ctx;
  const isLibrary = kind === "documents";
  const isImport = kind === "imports";
  const isDoc = isLibrary || isImport;
  if (!isLibrary && !canManage) {
    container.textContent = "访问此页面需要管理员权限。";
    return;
  }
  const shell = document.createElement("section");
  shell.className = "mg-page";
  shell.innerHTML = `<header class="mg-intro"><h1>${isLibrary ? "文档库" : "知识库管理"}</h1></header>
    ${isLibrary ? "" : managementNav(kind)}
    ${isImport ? `<button class="mg-drop" type="button">
      <span class="mg-drop-icon">${icon("upload", 22)}</span><span><strong>选择文件或拖放到此处</strong>
        <small data-format-hint>正在读取可导入格式…</small>
        <small>同一来源下的同名文件更新为新版本，历史引用可继续查看。</small></span></button>
      <input type="file" class="mg-picker" multiple hidden>
      <details class="mg-import-options"><summary>来源与解析设置</summary>
        <label>资料来源（可填部门或资料集名称）<input data-source-key maxlength="128"></label>
        ${optionsForm()}
        <label><input type="checkbox" data-inspect> 先预览分块，再确认导入</label>
      </details>
      <div class="mg-import-result" aria-live="polite" hidden></div>` : ""}
    <div class="mg-panel"><div class="mg-list-toolbar"><div class="mg-list-title"><h2>${isDoc ? "已导入文档" : "已配置接口"}</h2>
      <span class="mg-count"></span></div><label class="mg-search">${icon("search", 16)}<input type="search"
      aria-label="${isDoc ? "搜索文档" : "搜索接口"}" placeholder="${isDoc ? "搜索文件名" : "搜索接口名称或关键词"}"></label>
      ${!isDoc ? `<button type="button" data-create class="mg-btn mg-primary">${icon("plus", 15)}配置接口</button>` : ""}</div>
      <div class="mg-list-area" aria-live="polite"><div class="mg-empty">正在加载…</div></div></div>`;
  container.replaceChildren(shell);
  let items = [];
  let page = 0;
  let ready = false;
  const area = shell.querySelector(".mg-list-area");
  const search = shell.querySelector('[type="search"]');
  const count = shell.querySelector(".mg-count");
  let maxFileBytes = null;

  async function loadFormats() {
    if (!isImport) return;
    const hint = shell.querySelector("[data-format-hint]");
    try {
      const formats = await ctx.api("/api/formats");
      const extensions = formats.extensions.filter(ext => /^\.[a-z0-9]+$/i.test(ext));
      shell.querySelector(".mg-picker").accept = extensions.join(",");
      maxFileBytes = Number(formats.max_file_bytes) || null;
      const details = [extensions.map(ext => ext.slice(1).toUpperCase()).join("、")];
      if (maxFileBytes) details.push("单文件 " + (maxFileBytes / 1024 / 1024) + " MiB");
      if (formats.ocr_enabled) details.push("可识别图片中的文字");
      hint.textContent = details.join(" · ");
    } catch (error) {
      hint.textContent = "未能读取格式列表，导入时将由服务端校验。";
    }
  }

  async function load() {
    ready = false;
    count.textContent = "";
    area.innerHTML = '<div class="mg-empty" role="status">正在加载…</div>';
    try {
      items = await ctx.api(isDoc ? "/api/documents" : "/api/connectors");
      ready = true;
      draw();
    } catch (error) {
      ready = false;
      count.textContent = "";
      area.innerHTML = `<div class="mg-empty"><strong>加载失败</strong><p>${esc(messageOf(error))}</p>
        <button type="button" class="mg-btn" data-retry>重新加载</button></div>`;
      area.querySelector("[data-retry]").addEventListener("click", load);
    }
  }

  async function reload() {
    await Promise.allSettled([load(), Promise.resolve().then(() => ctx.refresh())]);
  }

  function docRow(d, i) {
    const ext = d.filename.includes(".") ? d.filename.split(".").pop().toUpperCase() : "FILE";
    const viewer = "/reader.html?id=" + safeRowId(d);
    return `<tr><td><div class="mg-doc-name"><span class="mg-format-icon">${icon("file", 18)}</span><div>
      <a href="${viewer}" target="_blank" rel="noopener" title="在线阅读">${esc(d.filename)}</a>
      <small>${esc([d.source_key,ext,formatSize(d.size),d.warning_count ? d.warning_count + " 项导入提示" : ""].filter(Boolean).join(" · "))}</small></div></div></td>
      <td class="mg-number">${esc(d.chunk_count ?? "—")}</td><td class="mg-time">${esc(formatDate(d.created_at))}</td>
      <td class="mg-controls"><div class="mg-doc-actions">
        ${isImport ? `<button type="button" class="mg-compare-link" data-action="reindex" data-item="${i}">重新解析</button>
          <button type="button" class="mg-compare-link" data-action="compare" data-item="${i}"
          aria-label="分块对照 ${esc(d.filename)}">分块对照</button>` :
          `<a class="mg-read-link" href="${viewer}" target="_blank" rel="noopener" aria-label="在线查看 ${esc(d.filename)}">在线查看</a>`}
        <a href="/api/documents/${safeRowId(d)}/download" download aria-label="下载原件 ${esc(d.filename)}">下载原件</a>
        ${isImport ? `<button type="button" class="mg-icon-btn" data-action="delete" data-item="${i}"
          title="删除文档" aria-label="删除 ${esc(d.filename)}">${icon("trash", 16)}</button>` : ""}
      </div></td></tr>`;
  }

  function connectorCard(c, i) {
    const kws = c.keywords || [];
    return `<article class="mg-connector"><div class="mg-connection-head"><span class="mg-format-icon">${icon("link", 18)}</span>
      <div><h3>${esc(c.name)}</h3>${c.description ? `<p>${esc(c.description)}</p>` : ""}</div>
      ${canManage ? `<div class="mg-inline-actions">
        <button type="button" class="mg-icon-btn" data-action="edit" data-item="${i}" aria-label="编辑 ${esc(c.name)}" title="编辑接口">${icon("edit", 16)}</button>
        <button type="button" class="mg-icon-btn" data-action="delete" data-item="${i}" aria-label="删除 ${esc(c.name)}" title="删除接口">${icon("trash", 16)}</button></div>` : ""}</div>
      <div class="mg-endpoint"><span class="mg-method">GET</span><code>${esc(c.url)}</code></div>
      <div class="mg-connector-bottom"><div class="mg-tags">${kws.map(kw => `<span>${esc(kw)}</span>`).join("")}</div>
      ${canManage ? `<button class="mg-btn mg-small" type="button" data-action="test" data-item="${i}">${icon("play", 13)}测试接口</button>` : ""}</div></article>`;
  }

  function draw() {
    if (!ready) return;
    const key = search.value.trim().toLocaleLowerCase();
    const filtered = items.filter(item => {
      const source = isDoc ? item.filename : [item.name, item.description, ...(item.keywords || [])].join(" ");
      return String(source).toLocaleLowerCase().includes(key);
    });
    const pages = Math.ceil(filtered.length / pageSize);
    page = Math.max(0, Math.min(page, pages - 1));
    count.textContent = key ? `匹配 ${filtered.length} / ${items.length}` : String(items.length);
    if (!filtered.length) {
      area.innerHTML = `<div class="mg-empty">${icon(isDoc ? "folder" : "link", 26)}
        <strong>${key ? "未找到匹配结果" : isDoc ? "暂无文档" : "暂无数据接口"}</strong>
        ${key ? '<button type="button" class="mg-btn" data-clear>清除搜索</button>' : ""}</div>`;
      area.querySelector("[data-clear]")?.addEventListener("click", () => { search.value = ""; draw(); search.focus(); });
      return;
    }
    const visible = filtered.slice(page * pageSize, (page + 1) * pageSize);
    area.innerHTML = (isDoc ? `<div class="mg-table-wrap"><table class="mg-table"><thead><tr><th>文件名称</th>
      <th class="mg-number">片段数</th><th class="mg-time">导入时间</th><th class="mg-controls">操作</th></tr></thead>
      <tbody>${visible.map(docRow).join("")}</tbody></table></div>` : `<div class="mg-connector-grid">${visible.map(connectorCard).join("")}</div>`) +
      (pages > 1 ? `<div class="mg-pager"><button type="button" class="mg-btn mg-small" data-prev ${page === 0 ? "disabled" : ""}>上一页</button>
        <span>${page + 1} / ${pages}</span><button type="button" class="mg-btn mg-small" data-next ${page + 1 >= pages ? "disabled" : ""}>下一页</button></div>` : "");
    area.querySelector("[data-prev]")?.addEventListener("click", () => { page--; draw(); });
    area.querySelector("[data-next]")?.addEventListener("click", () => { page++; draw(); });
    area.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => {
      const item = visible[Number(button.dataset.item)];
      if (button.dataset.action === "compare") compareChunks(shell, ctx, item, makeDialog);
      else if (button.dataset.action === "reindex") reindex(item, button);
      else if (button.dataset.action === "delete") confirmRemoval(shell, ctx, item, isDoc, reload);
      else if (button.dataset.action === "edit") openEditor(shell, ctx, item, reload);
      else openTest(shell, ctx, item);
    }));
  }

  async function reindex(item, button) {
    button.disabled = true;
    try {
      const doc = (await ctx.api(`/api/documents/${item.id}/preview?limit=1`)).document;
      let activeId = doc.id;
      const dialog = makeDialog(shell, ctx, "重新解析 · " + item.filename,
        `<div data-settings>${optionsForm(doc.options)}</div>
        <p data-msg role="status">${esc(warningsOf(doc))}</p>
        <button class="mg-btn mg-primary" type="button" data-rebuild>按以上设置重新解析</button>
        <section class="mg-versions"><h3>索引版本</h3>
          <p data-current></p>
          <p data-version-status role="status"></p>
          <label hidden>已有版本<select data-versions></select></label>
          <button class="mg-btn mg-small" type="button" data-refresh-versions hidden>重新读取版本</button>
          <button class="mg-btn mg-small" type="button" data-restore hidden disabled>确认启用所选版本</button>
        </section>`);
      const rebuild = dialog.querySelector("[data-rebuild]");
      const restore = dialog.querySelector("[data-restore]");
      const list = dialog.querySelector("[data-versions]");
      const retry = dialog.querySelector("[data-refresh-versions]");
      const note = dialog.querySelector("[data-version-status]");
      const message = dialog.querySelector("[data-msg]");
      let working = false;
      const describe = version => `#${version.id} · 解析版本 ${version.parser_version} · ${formatDate(version.created_at)} · ${version.chunk_count} 个分块`;

      async function loadVersions() {
        const requested = activeId;
        list.parentElement.hidden = restore.hidden = retry.hidden = true;
        restore.disabled = true;
        note.textContent = "正在读取版本…";
        try {
          const versions = await ctx.api(`/api/documents/${requested}/versions`);
          if (requested !== activeId || !dialog.open) return;
          const current = versions.find(v => v.active);
          const archived = versions.filter(v => !v.active);
          dialog.querySelector("[data-current]").textContent = current ? "当前版本 · " + describe(current) : "";
          list.replaceChildren(new Option("选择要启用的版本", ""));
          for (const version of archived) list.add(new Option(describe(version), version.id));
          list.parentElement.hidden = restore.hidden = !archived.length;
          note.textContent = archived.length ? "" : "暂无历史版本";
        } catch (error) {
          if (requested !== activeId || !dialog.open) return;
          note.textContent = messageOf(error);
          retry.hidden = false;
        }
      }

      async function changeVersion(restoreId) {
        if (working) return;
        working = true;
        rebuild.disabled = list.disabled = restore.disabled = retry.disabled = true;
        try {
          const base = `/api/documents/${activeId}`;
          const result = restoreId
            ? await ctx.api(base + "/restore/" + restoreId, {method:"POST"})
            : await ctx.api(base + "/reindex", jsonRequest("POST", readOptions(dialog)));
          activeId = result.id;
          const state = restoreId ? "已启用历史索引版本" : result.duplicate ? "已是当前解析版本" : "已启用新解析版本";
          message.textContent = state + " #" + activeId;
          await reload();
          if (!dialog.open) return;
          const updated = (await ctx.api(`/api/documents/${activeId}/preview?limit=1`)).document;
          dialog.querySelector("[data-settings]").innerHTML = optionsForm(updated.options);
          message.textContent += "\n" + warningsOf(updated);
          await loadVersions();
        } catch (error) {
          message.textContent = messageOf(error);
        } finally {
          working = false;
          rebuild.disabled = list.disabled = retry.disabled = false;
          restore.disabled = !list.value;
        }
      }
      list.onchange = () => { restore.disabled = !list.value || working; };
      retry.onclick = loadVersions;
      restore.onclick = () => { if (list.value) changeVersion(list.value); };
      rebuild.onclick = () => changeVersion(null);
      await loadVersions();
    } catch (error) { shell.querySelector(".mg-import-result").hidden = false; shell.querySelector(".mg-import-result").textContent = messageOf(error); }
    finally { button.disabled = false; }
  }

  search.addEventListener("input", () => { page = 0; draw(); });
  if (canManage && !isDoc) shell.querySelector("[data-create]").addEventListener("click", () => openEditor(shell, ctx, null, reload));
  if (isImport) {
    const picker = shell.querySelector(".mg-picker");
    const drop = shell.querySelector(".mg-drop");
    const feedback = shell.querySelector(".mg-import-result");
    let uploading = false;
    drop.addEventListener("click", () => { if (!uploading) picker.click(); });
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("mg-dragging"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("mg-dragging"));
    drop.addEventListener("drop", e => {
      e.preventDefault();
      drop.classList.remove("mg-dragging");
      upload(e.dataTransfer.files);
    });
    picker.addEventListener("change", () => upload(picker.files));
    function upload(files) {
      if (!files?.length || uploading) return;
      feedback.hidden = false;
      try {
        const settings = {options:readOptions(shell), source:shell.querySelector("[data-source-key]").value.trim()};
        const stage = shell.querySelector("[data-inspect]").checked ? "inspect" : "commit";
        const entries = Array.from(files, file => ({file}));
        runBatch(entries, entries, stage, settings);
      } catch (error) {
        feedback.classList.add("mg-warning");
        feedback.textContent = messageOf(error);
        picker.value = "";
      }
    }

    async function runBatch(entries, targets, stage, settings) {
      if (uploading) return;
      uploading = true;
      drop.disabled = true;
      drop.setAttribute("aria-busy", "true");
      try {
        for (const [index, entry] of targets.entries()) {
          const {file} = entry;
          feedback.textContent = (stage === "inspect" ? "正在预检 " : "正在导入 ") +
            (index + 1) + " / " + targets.length + " · " + file.name;
          entry.stage = stage;
          entry.result = null;
          entry.error = null;
          if (maxFileBytes && file.size > maxFileBytes) {
            entry.error = {error:"单文件超过 " + (maxFileBytes / 1024 / 1024) + " MiB"};
            continue;
          }
          const body = new FormData();
          body.append("files", file, file.name);
          body.append("source_key", settings.source);
          body.append("options", JSON.stringify(settings.options));
          try {
            const result = await ctx.api(stage === "inspect" ? "/api/documents/inspect" : "/api/documents", {method:"POST", body});
            entry.error = result.errors?.[0];
            entry.result = result.imported?.[0];
            if (!entry.result && !entry.error) throw new Error("没有收到文件处理结果");
          } catch (error) {
            entry.error = {error:messageOf(error), retryable:true};
          }
        }
        if (stage === "commit") await reload();
        drawBatch(entries, settings);
      } finally {
        picker.value = "";
        uploading = false;
        drop.disabled = false;
        drop.removeAttribute("aria-busy");
      }
    }

    function drawBatch(entries, settings) {
      const previewed = entries.filter(e => e.stage === "inspect" && e.result);
      const committed = entries.filter(e => e.stage === "commit" && e.result);
      const failed = entries.filter(e => e.error);
      const duplicates = committed.filter(e => e.result.duplicate).length;
      const summary = [
        previewed.length && previewed.length + " 个待确认",
        committed.length && "成功导入 " + (committed.length - duplicates) + " 个文件",
        duplicates && duplicates + " 个已存在",
        failed.length && failed.length + " 个失败"
      ].filter(Boolean).join("，");
      feedback.classList.toggle("mg-warning", !!failed.length);
      feedback.innerHTML = "<strong>" + esc(summary) + "</strong><p>" + esc(batchDescription(settings)) + "</p><ul>" + entries.map((entry, index) => {
        const {error, result, file} = entry;
        if (error) {
          const time = Number.isFinite(error.parse_ms) ? " · 解析耗时 " + error.parse_ms + " ms" : "";
          return `<li>${esc(file.name)} · ${esc(error.error || error.message || "文件处理失败")}${esc(time)}</li>`;
        }
        const warning = warningsOf(result);
        const preview = entry.stage === "inspect";
        const timing = Number.isFinite(result.parse_ms) ? " · 解析耗时 " + result.parse_ms + " ms" : "";
        const detail = preview
          ? "<ul>" + result.sample.map(s => `<li>${esc(s.locator)} · ${esc([s.context,s.text].filter(Boolean).join(" ").slice(0,240))}</li>`).join("") + "</ul>"
          : ` <button type="button" class="mg-compare-link" data-import-compare="${index}">分块对照</button>`;
        return `<li><strong>${esc(file.name)}</strong> · ${esc(result.chunk_count)} 个分块${esc(timing)}
          ${warning ? "<p>" + esc(warning) + "</p>" : ""}${detail}</li>`;
      }).join("") + "</ul>";
      feedback.querySelectorAll("[data-import-compare]").forEach(button => button.addEventListener("click", () =>
        compareChunks(shell, ctx, entries[Number(button.dataset.importCompare)].result, makeDialog)));
      function action(label, targets, stage, primary) {
        if (!targets.length) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mg-btn " + (primary ? "mg-primary" : "mg-small");
        button.textContent = label;
        button.onclick = () => runBatch(entries, targets, stage, settings);
        feedback.append(button);
      }
      action("确认导入已预览文件", previewed, "commit", true);
      for (const stage of ["inspect", "commit"]) {
        const retryable = failed.filter(e => e.stage === stage && e.error.retryable);
        action(`重试${stage === "inspect" ? "预检" : "导入"}失败的 ${retryable.length} 个文件`, retryable, stage, false);
      }
    }
  }
  await Promise.all([load(), loadFormats()]);
}
