import { managementNav } from "./admin.js";

const pageSize = 25;

function jsonRequest(method, data) {
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

function makeDialog(shell, ctx, title, body, className = "") {
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

function bindCancel(dialog) {
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
    payload.parameters = [...list.children].map(row => {
      const field = key => row.querySelector('[name="param_' + key + '"]');
      return { name: field("name").value.trim(), label: field("label").value.trim(), type: field("type").value,
        required: field("required").checked, aliases: tokens(field("aliases").value) };
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
      if (raw) params[p.name] = ["integer", "number"].includes(p.type) ? Number(raw) : raw;
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
      const total = data.total_count ?? rows.length;
      result.innerHTML = `<div class="mg-subheading"><h3>返回结果</h3><span>${esc(Number(total).toLocaleString("zh-CN"))} 条记录</span></div>
        <small>${esc(formatDate(data.fetched_at))} · ${esc(data.request || item.url)}</small>
        ${total > preview.length ? "<p>预览前 " + preview.length + " 条记录</p>" : ""}
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
        <small>导入同名文件将更新检索版本，历史引用保留原版本。</small></span></button>
      <input type="file" class="mg-picker" multiple hidden>
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
      <small>${esc(ext)} · ${esc(formatSize(d.size))}</small></div></div></td>
      <td class="mg-number">${esc(d.chunk_count ?? "—")}</td><td class="mg-time">${esc(formatDate(d.created_at))}</td>
      <td class="mg-controls"><div class="mg-doc-actions">
        <a class="mg-read-link" href="${viewer}" target="_blank" rel="noopener" aria-label="在线查看 ${esc(d.filename)}">在线查看</a>
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
      if (button.dataset.action === "delete") confirmRemoval(shell, ctx, item, isDoc, reload);
      else if (button.dataset.action === "edit") openEditor(shell, ctx, item, reload);
      else openTest(shell, ctx, item);
    }));
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
    async function upload(files) {
      if (!files?.length || uploading) return;
      const selected = Array.from(files);
      const errors = [];
      const imported = [];
      const retryable = [];
      feedback.hidden = false;
      feedback.classList.remove("mg-warning");
      uploading = true;
      drop.disabled = true;
      drop.setAttribute("aria-busy", "true");
      try {
        for (const [i, file] of selected.entries()) {
          feedback.textContent = "正在导入 " + (i + 1) + " / " + selected.length + " · " + file.name;
          if (maxFileBytes && file.size > maxFileBytes) {
            errors.push({ filename: file.name, error: "单文件超过 " + (maxFileBytes / 1024 / 1024) + " MiB" });
            continue;
          }
          const body = new FormData();
          body.append("files", file, file.name);
          try {
            const result = await ctx.api("/api/documents", { method: "POST", body });
            imported.push(...(result.imported || []));
            errors.push(...(result.errors || []));
          } catch (err) {
            errors.push({ filename: file.name, error: messageOf(err) });
            retryable.push(file);
          }
        }
        const duplicates = imported.filter(item => item.duplicate).length;
        feedback.classList.toggle("mg-warning", !!errors.length);
        feedback.innerHTML = `<strong>成功导入 ${imported.length - duplicates} 个文件${duplicates ? "，" + duplicates + " 个已存在" : ""}${errors.length ? "，" + errors.length + " 个失败" : ""}</strong>` +
          (errors.length ? "<ul>" + errors.map(error => `<li>${esc(typeof error === "string" ? error :
            [error.filename || error.name, error.error || error.message || error.detail || "导入失败"].filter(Boolean).join(" · "))}</li>`).join("") + "</ul>" : "");
        if (retryable.length) {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.className = "mg-btn mg-small";
          retry.textContent = `重试请求失败的 ${retryable.length} 个文件`;
          retry.addEventListener("click", () => upload(retryable));
          feedback.append(retry);
        }
        await reload();
      } catch (error) {
        feedback.classList.add("mg-warning");
        feedback.textContent = messageOf(error);
      } finally {
        picker.value = "";
        uploading = false;
        drop.disabled = false;
        drop.removeAttribute("aria-busy");
      }
    }
  }
  await Promise.all([load(), loadFormats()]);
}
