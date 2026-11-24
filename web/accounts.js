import { managementNav } from "./admin.js";
import { bindCancel, jsonRequest, makeDialog } from "./management.js";

const roles = { admin: "管理员", reader: "只读成员" };
const newPasswordFields = `<label>新密码<input name="password" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required aria-describedby="password-rules"></label>
  <label>确认新密码<input name="confirm" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>
  <p class="mg-help" id="password-rules">使用至少 12 个字符。</p>`;

function actions(label, cancel = false) {
  return `<p class="mg-error" role="alert" hidden></p><div class="mg-actions">
    ${cancel ? '<button class="mg-btn" type="button" data-cancel>取消</button>' : ""}
    <button class="mg-btn mg-primary" type="submit">${label}</button></div>`;
}

function handleSubmit(form, submit) {
  const errorBox = form.querySelector(".mg-error");
  const button = form.querySelector("[type=submit]");
  const label = button.textContent;
  form.oninput = () => { errorBox.hidden = true; };
  form.onsubmit = async event => {
    event.preventDefault();
    if (button.disabled) return;
    const fields = new FormData(form);
    if (fields.has("confirm") && fields.get("confirm") !== fields.get("password")) {
      errorBox.textContent = "两次输入的新密码不一致。";
      errorBox.hidden = false;
      form.elements.confirm.focus();
      return;
    }
    errorBox.hidden = true;
    form.setAttribute("aria-busy", "true");
    const controls = [...form.elements].filter(control => !control.disabled);
    for (const control of controls) control.disabled = true;
    button.textContent = "正在保存…";
    try {
      await submit(fields);
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      form.removeAttribute("aria-busy");
      for (const control of controls) control.disabled = false;
      button.textContent = label;
    }
  };
}

function roleSelect(role) {
  return `<label>角色<select name="role">${["reader", "admin"].map(value =>
    `<option value="${value}"${role === value ? " selected" : ""}>${roles[value]}</option>`).join("")}</select></label>`;
}

function editMember(shell, ctx, action, member, refresh) {
  const { api, esc, notify, requireLogin, session } = ctx;
  const name = member?.username;
  const first = !session.auth_required;
  const base = "/api/admin/accounts";
  const target = name ? base + "/" + encodeURIComponent(name) : base;
  const create = action === "create", password = action === "password";
  const identity = name ? `<p class="mg-help">账号 <strong>${esc(name)}</strong></p>` : "";
  let fields;
  if (create) {
    fields = `<label>用户名<input name="username" maxlength="64" required autocomplete="off" autocapitalize="none" spellcheck="false"></label>
      ${first ? '<p class="mg-help">首个个人账号为管理员。创建后请使用该账号登录。</p>' : roleSelect("reader")}${newPasswordFields}`;
  } else if (password) {
    fields = `${identity}${newPasswordFields}<p class="mg-help">保存后，该账号需要使用新密码重新登录。</p>`;
  } else {
    fields = `${identity}${roleSelect(member.role)}<label>状态<select name="enabled">
      <option value="true"${member.enabled ? " selected" : ""}>启用</option><option value="false"${member.enabled ? "" : " selected"}>停用</option>
      </select></label><p class="mg-help">角色或状态变更后，该账号的现有会话会失效。</p>`;
  }
  const title = { create: "新建用户", password: "重置密码", edit: "编辑用户" }[action];
  const dialog = makeDialog(shell, ctx, title, `<form class="mg-editor">${fields}${actions(create ? "创建用户" : "保存", true)}</form>`, "mg-dialog-small account-dialog");
  const form = dialog.querySelector("form");
  dialog.addEventListener("cancel", event => { if (form.getAttribute("aria-busy") === "true") event.preventDefault(); });
  bindCancel(dialog);
  handleSubmit(form, async values => {
    const role = first ? "admin" : values.get("role");
    const payload = { password: values.get("password") };
    if (create) {
      const result = await api(base, jsonRequest("POST", { ...payload, username: values.get("username").trim(), role }));
      if (result.login_required) {
        dialog.close();
        requireLogin("管理员账号已创建，请使用新账号登录。", result.username);
        return;
      }
    } else if (password) {
      await api(target + "/password", jsonRequest("PUT", payload));
    } else {
      await api(target, jsonRequest("PUT", { role, enabled: values.get("enabled") === "true" }));
    }
    dialog.close();
    notify(create ? "用户已创建" : password ? "密码已重置" : "用户设置已保存");
    await refresh();
  });
}

export async function renderAccounts(container, ctx) {
  const { api, esc, icon, session } = ctx;
  container.innerHTML = `<section class="adm-page mg-page accounts-page">
    <header class="adm-title"><h1>用户管理</h1></header>${managementNav("accounts")}
    <div class="accounts-toolbar">
      <label class="account-search">${icon("search", 17)}<input type="search" placeholder="搜索用户名" aria-label="搜索用户名" maxlength="64" disabled></label>
      <button type="button" class="mg-btn mg-primary" data-create disabled>${icon("plus", 16)} 新建用户</button>
    </div>
    <p class="accounts-status" role="status">正在读取用户…</p>
    <div class="accounts-list"></div>
  </section>`;
  const shell = container.firstElementChild;
  const status = shell.querySelector(".accounts-status");
  const list = shell.querySelector(".accounts-list");
  const search = shell.querySelector("input");
  const create = shell.querySelector("[data-create]");
  let accounts = [];

  function draw() {
    const query = search.value.trim().normalize("NFKC").toLowerCase();
    const items = accounts.filter(member => member.username.toLowerCase().includes(query));
    status.textContent = items.length ? `显示 ${items.length} / ${accounts.length} 个个人账号`
      : query ? "没有匹配的用户。" : "暂无个人账号。";
    list.innerHTML = items.length ? `<table><thead><tr><th scope="col">用户名</th><th scope="col">角色</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
      <tbody>${items.map(member => {
        const self = member.username === session.username;
        return `<tr><th scope="row"><div class="member-name"><span class="avatar">${icon("user", 16)}</span>
          <span>${esc(member.username)}${self ? '<small>当前账号</small>' : ""}</span></div></th>
          <td data-label="角色">${roles[member.role]}</td><td data-label="状态"><span class="member-badge ${member.enabled ? "is-enabled" : ""}">${member.enabled ? "已启用" : "已停用"}</span></td>
          <td class="member-actions">${self ? '<a class="mg-btn mg-small" href="#account">个人账号</a>'
            : `<button type="button" class="mg-btn mg-small" data-action="edit" data-username="${esc(member.username)}" aria-label="编辑用户 ${esc(member.username)}">编辑</button>
               <button type="button" class="mg-btn mg-small" data-action="password" data-username="${esc(member.username)}" aria-label="重置密码 ${esc(member.username)}">重置密码</button>`}</td></tr>`;
      }).join("")}</tbody></table>` : "";
  }

  async function load() {
    list.setAttribute("aria-busy", "true");
    try {
      accounts = await api("/api/admin/accounts");
      search.disabled = create.disabled = false;
      draw();
    } catch (error) {
      search.disabled = create.disabled = true;
      list.replaceChildren();
      status.innerHTML = `${esc(error.message)} <button class="text-action" type="button">重试</button>`;
      status.querySelector("button").onclick = load;
    } finally {
      list.removeAttribute("aria-busy");
    }
  }

  search.oninput = draw;
  create.onclick = () => editMember(shell, ctx, "create", null, load);
  list.onclick = event => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const member = accounts.find(item => item.username === button.dataset.username);
    if (member) editMember(shell, ctx, button.dataset.action, member, load);
  };
  await load();
}

export function renderAccount(container, ctx) {
  const { api, esc, icon, requireLogin, session, signOut } = ctx;
  const name = session.username || (session.auth_required ? "共享会话" : "本地体验");
  const type = session.username ? "个人账号" : name;
  container.innerHTML = `<section class="adm-page mg-page personal-page">
    <header class="adm-title"><h1>个人账号</h1></header>
    <section class="account-summary" aria-label="当前账号信息">
      <div class="member-name"><span class="avatar">${icon("user", 20)}</span><strong>${esc(name)}</strong></div>
      <dl><div><dt>当前角色</dt><dd>${roles[session.role] || "管理员"}</dd></div>
        <div><dt>登录方式</dt><dd>${type}</dd></div></dl>
    </section>
    ${session.username ? `<section class="account-settings"><h2>修改密码</h2>
      <p>保存后会退出此账号的所有会话，请使用新密码重新登录。</p>
      <form class="mg-editor"><label>当前密码<input name="current" type="password" maxlength="1024" autocomplete="current-password" required></label>
      <input name="username" value="${esc(session.username)}" autocomplete="username" hidden>${newPasswordFields}${actions("修改密码")}</form></section>`
    : `<p class="account-note">${session.auth_required ? "共享密码由部署管理员维护。" : '创建个人管理员账号后，可使用账号登录。<a href="#accounts">前往用户管理</a>'}</p>`}
    ${session.auth_required ? '<button type="button" class="secondary-btn account-signout" data-signout>退出登录</button>' : ""}
  </section>`;
  container.querySelector("[data-signout]")?.addEventListener("click", event => signOut(event.currentTarget));
  const form = container.querySelector("form");
  if (form) handleSubmit(form, async values => {
    await api("/api/account/password", jsonRequest("PUT", { current_password: values.get("current"), password: values.get("password") }));
    requireLogin("密码已修改，请重新登录。", session.username);
  });
}
