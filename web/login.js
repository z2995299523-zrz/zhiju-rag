export function renderLogin(container, { icon, signIn, message, username }) {
  container.innerHTML = `<section class="login-page" aria-labelledby="login-title">
    <div class="login-story">
      <div class="login-brand"><span class="brand-mark">${icon("layers", 23)}</span><span>澄知 RAG</span></div>
      <div class="login-story-content">
        <p class="login-lead">企业知识<br>工作台</p>
        <p>检索内部文档与业务数据，点击引文查看原始依据。</p>
        <ul class="login-sources" aria-label="工作区功能">
          <li>${icon("book", 17)} 文档检索</li>
          <li>${icon("database", 17)} 业务查数</li>
          <li>${icon("layers", 17)} 来源核验</li>
        </ul>
      </div>
    </div>
    <div class="login-panel">
      <div class="login-card">
        <h1 id="login-title">登录工作区</h1>
        <p class="login-intro" id="login-intro"></p>
        <div class="login-modes" role="group" aria-label="登录方式">
          <button type="button" data-mode="personal" aria-pressed="true">个人账号</button>
          <button type="button" data-mode="shared" aria-pressed="false">共享密码</button>
        </div>
        <p class="login-notice" role="status" hidden></p>
        <form id="login-form">
          <div class="login-field" data-username>
            <label for="username">用户名</label>
            <input id="username" name="username" required maxlength="64" autocomplete="username" autocapitalize="none" spellcheck="false" placeholder="输入用户名">
          </div>
          <div class="login-field">
            <label for="password" data-password-label>密码</label>
            <div class="login-password">
              <input id="password" name="password" type="password" required maxlength="1024" autocomplete="current-password" placeholder="输入密码">
              <button type="button" class="icon-button" data-toggle-password aria-label="显示密码" title="显示密码">${icon("eye", 19)}</button>
            </div>
          </div>
          <p class="login-error" id="login-error" role="alert" hidden></p>
          <button class="primary-btn login-submit" type="submit">登录 ${icon("arrow", 17)}</button>
        </form>
      </div>
    </div>
  </section>`;
  const form = container.querySelector("form");
  const modes = [...container.querySelectorAll("[data-mode]")];
  const userField = form.querySelector("[data-username]");
  const user = form.elements.username;
  const password = form.elements.password;
  const toggle = form.querySelector("[data-toggle-password]");
  const errorBox = form.querySelector("#login-error");
  const submit = form.querySelector("[type=submit]");
  const notice = container.querySelector(".login-notice");
  let personal = true;

  function showPassword(visible) {
    password.type = visible ? "text" : "password";
    const label = visible ? "隐藏密码" : "显示密码";
    toggle.setAttribute("aria-label", label);
    toggle.title = label;
    toggle.innerHTML = icon(visible ? "eyeOff" : "eye", 19);
  }

  function showError(message = "") {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  function choose(mode) {
    personal = mode === "personal";
    for (const button of modes) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    userField.hidden = user.disabled = !personal;
    form.querySelector("[data-password-label]").textContent = personal ? "密码" : "工作区共享密码";
    container.querySelector("#login-intro").textContent = personal ? "使用管理员分配给你的账号。" : "使用管理员提供的工作区密码。";
    password.value = "";
    showPassword(false);
    showError();
  }

  choose("personal");
  user.value = username || "";
  if (message) {
    notice.textContent = message;
    notice.hidden = false;
  }
  for (const button of modes) button.onclick = () => {
    if (button.getAttribute("aria-pressed") === "false") choose(button.dataset.mode);
  };
  toggle.onclick = () => showPassword(password.type === "password");
  form.oninput = () => { if (!errorBox.hidden) showError(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    const name = user.value.trim();
    if (personal && !name) {
      showError("请输入用户名。");
      user.focus();
      return;
    }
    showError();
    showPassword(false);
    submit.disabled = true;
    toggle.disabled = true;
    user.readOnly = password.readOnly = true;
    for (const button of modes) button.disabled = true;
    form.setAttribute("aria-busy", "true");
    submit.textContent = "正在登录…";
    try {
      await signIn({ username: personal ? name : undefined, password: password.value });
    } catch (error) {
      if (!form.isConnected) return;
      showError(error.message);
      password.value = "";
    } finally {
      submit.disabled = false;
      toggle.disabled = false;
      user.readOnly = password.readOnly = false;
      for (const button of modes) button.disabled = false;
      form.removeAttribute("aria-busy");
      submit.innerHTML = `登录 ${icon("arrow", 17)}`;
      if (form.isConnected && !errorBox.hidden) password.focus();
    }
  };
  if (message) (username ? password : user).focus({ preventScroll: true });
}
