import { managementNav } from "./admin.js";

const types = {
  llm: { title:"对话模型", purpose:"辅助理解提问，并依据检索到的文档原文生成回答。", env:"RAG_LLM" },
  embedding: { title:"向量模型", purpose:"比较问题与候选文段的语义相似度，辅助文档检索排序。", env:"RAG_EMBED" }
};
const modes = { environment:"使用环境配置", workspace:"使用工作区配置", disabled:"关闭模型" };
const sources = { environment:"环境配置", workspace:"工作区配置", disabled:"手动关闭", none:"未配置" };

function modelForm(kind, initial, ctx) {
  const form = document.createElement("form");
  form.dataset.modelKind = kind;
  form.className = "model-form";
  const { title, purpose, env } = types[kind];
  form.setAttribute("aria-label", title + "配置");
  form.innerHTML = `<fieldset><legend>${title}</legend><p class="model-purpose">${purpose}</p>
    <div class="model-current"><strong></strong><span></span></div>
    <label class="model-field">配置方式<select name="mode">${Object.entries(modes).map(([value,label]) =>
      `<option value="${value}">${label}</option>`).join("")}</select></label>
    <p class="model-help" data-mode-help></p>
    <div data-workspace-fields>
      <label class="model-field">服务地址<input name="base_url" type="url" inputmode="url" maxlength="2048" spellcheck="false" autocomplete="off" placeholder="https://…/v1" required></label>
      <label class="model-field">模型名称<input name="model" type="text" maxlength="512" autocomplete="off" spellcheck="false" required></label>
      <label class="model-field">API 密钥 <input name="api_key" type="password" maxlength="4096" autocomplete="new-password" spellcheck="false"></label>
      <label class="model-remove"><input name="clear_api_key" type="checkbox">移除已配置的密钥</label>
    </div>
    <div class="model-actions"><button type="button" class="secondary-btn" data-test>测试连接</button>
      <button type="submit" class="primary-btn" data-save>保存配置</button></div>
    <p class="model-feedback" role="status" aria-live="polite"></p>
  </fieldset>`;
  const {mode, base_url:address, model, api_key:key, clear_api_key:clear} = form.elements;
  const fields = form.querySelector("[data-workspace-fields]");
  const test = form.querySelector("[data-test]");
  const save = form.querySelector("[data-save]");
  const feedback = form.querySelector(".model-feedback");
  let current = initial;

  function fill() {
    mode.value = current.mode;
    address.value = current.base_url || "";
    model.value = current.model || "";
    key.value = "";
    clear.checked = false;
    const summary = form.querySelector(".model-current");
    summary.querySelector("strong").textContent = current.enabled ? "当前已启用" : "当前未启用";
    const details = [sources[current.source] || modes[current.mode]];
    if (current.enabled) details.push(current.model, current.base_url);
    if (current.has_api_key) details.push("密钥 ••••••••");
    summary.querySelector("span").textContent = details.filter(Boolean).join(" · ");
    update();
  }

  function update() {
    const manual = mode.value === "workspace";
    fields.hidden = !manual;
    address.disabled = model.disabled = !manual;
    clear.disabled = !manual || !current.has_api_key;
    clear.parentElement.hidden = !current.has_api_key;
    key.disabled = !manual || clear.checked;
    const sameAddress = address.value.trim().replace(/\/+$/, "") === (current.base_url || "").replace(/\/+$/, "");
    key.placeholder = clear.checked ? "保存时移除" :
      current.has_api_key && sameAddress ? "•••••••• · 留空保留原密钥" : "可选，有鉴权要求时填写";
    const help = form.querySelector("[data-mode-help]");
    if (mode.value === "environment") {
      help.textContent = (current.mode === "workspace" || current.mode === "disabled" ? "保存后移除工作区覆盖。" : "") +
        `读取服务器环境变量 ${env}_BASE_URL、${env}_MODEL 和可选的 ${env}_API_KEY。`;
    } else if (mode.value === "disabled") {
      help.textContent = "关闭后停用此模型，已保存的工作区配置可再次启用。";
    } else {
      help.textContent = current.has_api_key && !sameAddress ?
        "服务地址已更改。如新服务需要鉴权，请填写对应的密钥。" : "填写兼容 OpenAI API 的服务地址和模型名称。";
    }
    test.disabled = mode.value === "disabled";
    save.disabled = mode.value === current.mode && (!manual ||
      (address.value.trim() === (current.base_url || "") && model.value.trim() === (current.model || "") && !key.value && !clear.checked));
  }

  form.addEventListener("input", () => { feedback.textContent = ""; update(); });
  form.addEventListener("change", () => { feedback.textContent = ""; update(); });

  async function submit(testOnly) {
    if (!form.reportValidity()) return;
    const payload = {mode:mode.value};
    if (mode.value === "workspace") {
      payload.base_url = address.value.trim();
      payload.model = model.value.trim();
      if (clear.checked) payload.clear_api_key = true;
      else if (key.value.trim()) payload.api_key = key.value.trim();
    }
    form.querySelector("fieldset").disabled = true;
    form.setAttribute("aria-busy", "true");
    feedback.textContent = testOnly ? "正在测试连接…" : "正在保存…";
    feedback.classList.remove("error");
    try {
      const data = await ctx.api("/api/admin/models/" + kind + (testOnly ? "/test" : ""), {
        method:testOnly ? "POST" : "PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
      });
      if (!form.isConnected) return;
      if (testOnly) {
        feedback.textContent = (data.message || "连接成功") + (Number.isFinite(data.latency_ms) ? `（${data.latency_ms} ms）` : "");
      } else {
        current = data;
        fill();
        feedback.textContent = "配置已保存。";
      }
    } catch (error) {
      if (!form.isConnected) return;
      feedback.classList.add("error");
      feedback.textContent = (testOnly ? "测试失败。" : "保存失败。") + error.message;
    } finally {
      form.querySelector("fieldset").disabled = false;
      form.setAttribute("aria-busy", "false");
      update();
    }
  }

  form.onsubmit = event => { event.preventDefault(); void submit(false); };
  test.onclick = () => submit(true);
  fill();
  return form;
}

export async function renderModels(container, ctx) {
  if (!ctx.canManage) {
    container.textContent = "模型配置需要管理员权限。";
    return;
  }
  const page = document.createElement("section");
  page.className = "adm-page";
  page.innerHTML = `<header class="adm-title"><h1>知识库管理</h1></header>${managementNav("models")}
    <div class="model-configurations" aria-label="模型配置"><p class="adm-note" role="status">正在读取配置…</p></div>`;
  container.replaceChildren(page);
  const data = await ctx.api("/api/admin/models");
  if (!page.isConnected) return;
  const area = page.querySelector(".model-configurations");
  area.replaceChildren(...Object.keys(types).map(kind => modelForm(kind, data[kind], ctx)));
}
