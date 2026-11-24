const byId = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const embedded = window.parent !== window && params.get("embedded") === "1";
document.documentElement.classList.toggle("reader-embedded", embedded);
const positiveId = value => /^\d+$/.test(value || "") && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;
const documentId = positiveId(params.get("id"));
const citationId = positiveId(params.get("chunk"));
const base = "/api/documents/" + documentId;
let controller;

function displaySize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function showMessage(title, detail, { retry = false, login = false, download = false, home = false } = {}) {
  byId("reader").hidden = true;
  byId("original-pane").replaceChildren();
  byId("message-panel").hidden = false;
  byId("message-title").textContent = title;
  byId("message-detail").textContent = detail;
  byId("retry").hidden = !retry;
  byId("login").hidden = !login;
  byId("message-download").hidden = !download;
  byId("message-download").href = base + "/download";
  byId("document-home").hidden = !home;
  if (home) {
    const target = new URL(location.href);
    target.searchParams.delete("chunk");
    byId("document-home").href = target.href;
  }
}

function paneStatus(text) {
  const p = document.createElement("p");
  p.className = "pane-status";
  p.setAttribute("role", "status");
  p.textContent = text;
  byId("original-pane").replaceChildren(p);
}

function decodeOriginal(buffer) {
  const bytes = new Uint8Array(buffer);
  const has = (...prefix) => prefix.every((byte, i) => bytes[i] === byte);
  const utf32LE = has(255, 254, 0, 0);
  if (utf32LE || has(0, 0, 254, 255)) {
    const view = new DataView(buffer);
    const parts = [];
    const codes = [];
    for (let i = 4; i < view.byteLength; i += 4) {
      const code = view.getUint32(i, utf32LE);
      if (code >= 0xd800 && code <= 0xdfff) throw new Error("Invalid UTF-32");
      codes.push(code);
      if (codes.length === 2048) parts.push(String.fromCodePoint(...codes.splice(0)));
    }
    parts.push(String.fromCodePoint(...codes));
    return parts.join("");
  }
  if (has(255, 254)) return new TextDecoder("utf-16le", {fatal:true}).decode(buffer);
  if (has(254, 255)) return new TextDecoder("utf-16be", {fatal:true}).decode(buffer);
  try {
    return new TextDecoder("utf-8", {fatal:true}).decode(buffer);
  } catch {
    return new TextDecoder("gb18030", {fatal:true}).decode(buffer);
  }
}

async function renderText(signal) {
  paneStatus("正在读取原始文件…");
  try {
    const response = await fetch(base + "/download", {cache:"no-store", signal});
    if (signal.aborted) return;
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        showMessage("需登录后阅读", "登录工作台后，回到本页继续阅读。", {login:true, retry:true});
        return;
      }
      throw new Error("HTTP " + response.status);
    }
    const rawText = decodeOriginal(await response.arrayBuffer());
    if (signal.aborted) return;
    const pre = document.createElement("pre");
    pre.className = "source-text";
    pre.tabIndex = 0;
    pre.setAttribute("aria-label", "原始文件内容");
    pre.textContent = rawText;
    byId("original-pane").replaceChildren(pre);
  } catch {
    if (!signal.aborted) paneStatus("原始文本读取失败，可以下载原件查看。");
  }
}

function render({document: doc, citation_locator: locator, original_kind: kind}, signal) {
  const isPdf = kind === "pdf";
  const page = isPdf && /^第\s*(\d+)\s*页/.exec(locator || "")?.[1];
  const address = base + "/original" + (page ? "#page=" + page : "");
  document.title = doc.filename + " · 澄知 RAG";
  byId("filename").textContent = doc.filename;
  byId("file-size").textContent = displaySize(doc.size);
  byId("archived").hidden = doc.active;
  byId("citation-location").hidden = !locator;
  byId("citation-location").textContent = page ? "引用位置 · 第 " + page + " 页" : locator || "";
  byId("checksum").hidden = !doc.sha256;
  byId("digest").textContent = doc.sha256 || "";
  byId("download").href = base + "/download";
  byId("open-original").hidden = !isPdf && kind !== "image";
  byId("open-original").href = address;
  const pane = byId("original-pane");
  pane.className = "original-pane" + (kind ? " show-" + kind : "");
  pane.replaceChildren();
  byId("message-panel").hidden = true;
  byId("reader").hidden = false;
  if (isPdf && navigator.pdfViewerEnabled === false) {
    paneStatus("当前浏览器未启用 PDF 内嵌预览。可在新标签打开或下载原件。");
  } else if (isPdf) {
    const frame = document.createElement("iframe");
    frame.title = doc.filename + " · PDF 原件";
    frame.src = address;
    pane.append(frame);
  } else if (kind === "image") {
    const image = document.createElement("img");
    image.alt = doc.filename;
    image.onerror = () => paneStatus("原图加载失败，可以下载原件查看。");
    image.src = address;
    pane.append(image);
  } else if (kind === "text") {
    void renderText(signal);
  } else {
    paneStatus("此格式请下载原件，使用对应应用查看完整排版。");
  }
}

async function load() {
  controller?.abort();
  controller = new AbortController();
  const {signal} = controller;
  const query = citationId ? "?chunk_id=" + citationId : "";
  showMessage("正在打开文档…", "");
  try {
    const response = await fetch(base + "/original-info" + query, {cache:"no-store", signal});
    if (signal.aborted) return;
    if (response.status === 401 || response.status === 403) {
      showMessage("需登录后阅读", "登录工作台后，回到本页继续阅读。", {login:true, retry:true});
      return;
    }
    if (response.status === 404) {
      const payload = await response.json().catch(() => ({}));
      if (signal.aborted) return;
      if (payload.detail === "Not Found") {
        showMessage("当前服务不支持原件预览", "请打开新版工作台，或直接下载原件。", {download:true});
      } else {
        const missingChunk = payload.detail === "此引用段落不存在于该文档版本";
        showMessage("未找到文档或指定位置", "请回到文档库查看仍可阅读的版本。", {home:missingChunk});
      }
      return;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    const metadata = await response.json();
    if (!signal.aborted) render(metadata, signal);
  } catch {
    if (!signal.aborted) showMessage("文档读取失败", "请确认工作台服务仍在运行，然后重试。", {retry:true});
  }
}

byId("retry").onclick = load;
if (documentId) void load();
else showMessage("文档链接无效", "请从文档库或回答中的引用重新打开。");
if (embedded) {
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      window.parent.postMessage("reader:close", location.origin);
    }
  });
}
