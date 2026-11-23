const byId = id => document.getElementById(id);
const locationParams = new URLSearchParams(location.search);
const embedded = window.parent !== window && locationParams.get("embedded") === "1";
document.documentElement.classList.toggle("reader-embedded", embedded);
const positiveId = value => /^\d+$/.test(value || "") && Number.isSafeInteger(Number(value)) && Number(value) > 0 ? Number(value) : null;
const documentId = positiveId(locationParams.get("id"));
const citationId = positiveId(locationParams.get("chunk"));
const base = "/api/documents/" + documentId;
const pageSize = 12;
let snapshot = null;
let controller = null;
let lastRequest = null;
let searchTerm = "";
let citationPage = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function displaySize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1048576).toFixed(1) + " MB";
}

function showMessage(title, detail, { retry = false, login = false, download = false } = {}) {
  byId("reader").hidden = true;
  byId("message-panel").hidden = false;
  byId("message-title").textContent = title;
  byId("message-detail").textContent = detail;
  byId("retry").hidden = !retry;
  byId("login").hidden = !login;
  byId("message-download").hidden = !download;
  if (download) byId("message-download").href = base + "/download";
  byId("native-pane").replaceChildren();
}

async function missingPreviewRoute(response, signal) {
  // FastAPI returns a generic 404 for an unregistered route. Missing documents
  // and missing citations have their own messages from the registered endpoint.
  const error = await response.json().catch(() => null);
  if (error?.detail !== "Not Found") return false;
  try {
    const schema = await fetch("/api/openapi.json", { credentials: "same-origin", cache: "no-store", signal });
    if (!schema.ok) return false;
    const { paths } = await schema.json();
    return Boolean(paths?.["/api/documents/{doc_id}/download"]?.get && !paths["/api/documents/{doc_id}/preview"]);
  } catch {
    return false;
  }
}

function highlight(node, text, term) {
  if (!term) {
    node.textContent = text;
    return;
  }
  // Match the server's ASCII-insensitive substring search and retain source offsets.
  const fold = s => s.replace(/[A-Z]/g, c => c.toLowerCase());
  const body = fold(text);
  const needle = fold(term);
  let start = 0;
  let found = body.indexOf(needle);
  while (found !== -1) {
    node.append(document.createTextNode(text.slice(start, found)), element("mark", "", text.slice(found, found + term.length)));
    start = found + term.length;
    found = body.indexOf(needle, start);
  }
  node.append(document.createTextNode(text.slice(start)));
}

function scrollToChunk(id) {
  const chunk = byId("chunk-" + id);
  if (chunk) {
    chunk.focus({ preventScroll: true });
    const bounds = chunk.getBoundingClientRect();
    if (bounds.top < 0 || bounds.bottom > window.innerHeight) chunk.scrollIntoView({ block: "start" });
  }
}

function renderChunks(data) {
  const sections = document.createDocumentFragment();
  const pointers = document.createDocumentFragment();
  for (const chunk of data.chunks) {
    const cited = Number(chunk.id) === citationId;
    const locator = String(chunk.locator || "原文片段");
    const section = element("article", "chunk" + (cited ? " cited" : ""));
    section.id = "chunk-" + chunk.id;
    section.tabIndex = -1;
    const title = element("h2");
    const link = element("a");
    const linkParams = new URLSearchParams({ id: String(documentId), chunk: String(chunk.id) });
    if (embedded) linkParams.set("embedded", "1");
    link.href = "/reader.html?" + linkParams;
    link.title = "打开此位置的独立链接";
    highlight(link, locator, searchTerm);
    title.append(link);
    if (cited) title.append(element("span", "cite-label", "引用位置"));
    const content = element("p", "chunk-text");
    highlight(content, String(chunk.text || ""), searchTerm);
    section.append(title);
    if (chunk.context) {
      const context = element("p", "chunk-context");
      highlight(context, String(chunk.context), searchTerm);
      section.append(context);
    }
    section.append(content);
    sections.append(section);
    const anchor = element("a", cited ? "cited" : "", locator);
    anchor.href = "#" + section.id;
    anchor.addEventListener("click", event => { event.preventDefault(); scrollToChunk(chunk.id); });
    const item = element("li");
    item.append(anchor);
    pointers.append(item);
    if (cited) citationPage = pageFromLocator(locator);
  }
  if (!data.chunks.length) {
    sections.append(element("p", "empty", searchTerm ? "没有匹配的片段，可更换关键词或清空搜索。" : "这个文档没有可阅读的文本片段。"));
  }
  byId("chunks").replaceChildren(sections);
  byId("locators").replaceChildren(pointers);
  const showOutline = data.chunks.length > 1;
  byId("outline").hidden = !showOutline;
  byId("reading-layout").classList.toggle("with-outline", showOutline);
}

function pageFromLocator(locator) {
  const match = /^第\s*(\d+)\s*页/.exec(locator);
  return match ? positiveId(match[1]) : null;
}

function nativeAddress() {
  const visibleCitation = snapshot.chunks.find(chunk => chunk.id === citationId);
  const page = pageFromLocator(visibleCitation?.locator || snapshot.chunks[0]?.locator || "") || citationPage;
  const suffix = snapshot.original_kind === "pdf" && page ? "#page=" + page : "";
  return base + "/original" + suffix;
}

function displayMode(native) {
  if (!snapshot) return;
  native = native && ["pdf", "image"].includes(snapshot.original_kind);
  byId("text-pane").hidden = native;
  byId("native-pane").hidden = !native;
  byId("search-form").hidden = native;
  byId("open-original").hidden = !native;
  byId("text-mode").setAttribute("aria-pressed", String(!native));
  byId("original-mode").setAttribute("aria-pressed", String(native));
  if (!native) return;
  const address = nativeAddress();
  byId("open-original").href = address;
  if (byId("native-pane").firstElementChild?.getAttribute("src") === address) return;
  const title = snapshot.document.filename;
  const viewer = element(snapshot.original_kind === "pdf" ? "iframe" : "img");
  if (viewer.tagName === "IFRAME") viewer.title = title + " PDF 原版";
  else viewer.alt = title;
  viewer.src = address;
  byId("native-pane").replaceChildren(viewer);
}

function render(data) {
  const doc = data.document;
  document.title = doc.filename + " · 知据";
  byId("filename").textContent = doc.filename;
  byId("extension").textContent = (doc.filename.match(/\.([^.]+)$/)?.[1] || "").toUpperCase();
  byId("file-size").textContent = displaySize(doc.size);
  const date = new Date(doc.created_at);
  byId("created").textContent = doc.created_at && Number.isFinite(date.getTime()) ? "导入于 " + date.toLocaleDateString("zh-CN") : "";
  byId("archived").hidden = Boolean(doc.active);
  byId("checksum").hidden = !doc.sha256;
  byId("digest").textContent = doc.sha256 || "";
  byId("download").href = base + "/download";
  const hasNative = ["pdf", "image"].includes(data.original_kind);
  byId("view-options").hidden = !hasNative;
  byId("text-label").hidden = hasNative;
  byId("format-help").hidden = hasNative;
  byId("original-mode").textContent = data.original_kind === "pdf" ? "PDF 原版" : "原图";
  displayMode(false);
  byId("message-panel").hidden = true;
  byId("reader").hidden = false;
  renderChunks(data);
  const page = Math.floor(data.offset / data.limit) + 1;
  const count = Math.max(1, Math.ceil(data.total / data.limit));
  byId("summary").textContent = (searchTerm ? "搜索结果" : "文档全文") + " · " +
    (data.total ? (data.offset + 1) + "–" + (data.offset + data.chunks.length) + " / " + data.total + " 段" : "0 段");
  byId("back-to-cite").hidden = !citationId;
  byId("pagination").hidden = byId("quick-pages").hidden = count <= 1;
  byId("previous").disabled = byId("quick-prev").disabled = page <= 1;
  byId("next").disabled = byId("quick-next").disabled = page >= count;
  byId("page-count").textContent = count;
  byId("page-number").max = count;
  byId("page-number").value = page;
}

async function load({ offset = 0, q = "", focus = null, scroll = false } = {}) {
  controller?.abort();
  const request = new AbortController();
  controller = request;
  lastRequest = { offset, q, focus, scroll };
  const args = new URLSearchParams({ offset: String(offset), limit: String(pageSize) });
  if (q) args.set("q", q);
  if (focus) args.set("chunk_id", String(focus));
  byId("content").setAttribute("aria-busy", "true");
  if (!byId("reader").hidden) byId("summary").textContent = "正在读取…";
  try {
    const response = await fetch(base + "/preview?" + args, {
      credentials: "same-origin", cache: "no-store", signal: request.signal,
      headers: { Accept: "application/json" }
    });
    if (controller !== request) return;
    if (response.status === 401 || response.status === 403) {
      snapshot = null;
      byId("chunks").replaceChildren();
      byId("locators").replaceChildren();
      showMessage("需登录后阅读", "登录工作台后，回到本页继续阅读。", { login: true, retry: true });
      return;
    }
    if (response.status === 404) {
      const legacy = await missingPreviewRoute(response, request.signal);
      if (controller !== request) return;
      if (legacy) {
        showMessage("当前工作台版本不支持在线预览",
          "请从项目目录运行 start.cmd，在启动器打开的工作台中查看文档。也可以先下载原件。", { download: true });
        return;
      }
      showMessage("未找到文档或指定片段", "可以回到文档库查看仍可阅读的版本。");
      if (focus) {
        const fallback = element("button", "btn", "打开文档首页");
        fallback.type = "button";
        fallback.onclick = () => { fallback.remove(); load(); };
        byId("message-panel").querySelector(".message-actions").append(fallback);
      }
      return;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (controller !== request) return;
    snapshot = data;
    searchTerm = q;
    render(data);
    if (focus && data.focus_chunk_id) scrollToChunk(data.focus_chunk_id);
    else if (scroll) byId("content").scrollIntoView({ block: "start" });
  } catch (error) {
    if (request.signal.aborted || controller !== request) return;
    showMessage("文档读取失败", "请确认工作台服务仍在运行，然后重试。", { retry: true });
  } finally {
    if (controller === request) byId("content").setAttribute("aria-busy", "false");
  }
}

function changePage(direction) {
  if (!snapshot) return;
  const offset = snapshot.offset + direction * snapshot.limit;
  if (offset >= 0 && offset < snapshot.total) load({ q: searchTerm, offset, scroll: true });
}

byId("text-mode").addEventListener("click", () => displayMode(false));
byId("original-mode").addEventListener("click", () => displayMode(true));
byId("retry").addEventListener("click", () => load(lastRequest || {}));
byId("back-to-cite").addEventListener("click", () => {
  byId("search-input").value = "";
  if (!searchTerm && byId("chunk-" + citationId)) scrollToChunk(citationId);
  else load({ focus: citationId });
});
byId("search-form").addEventListener("submit", e => {
  e.preventDefault();
  load({ q: byId("search-input").value.trim() });
});
byId("search-input").addEventListener("input", e => {
  if (!e.target.value && (searchTerm || lastRequest?.q)) load();
});
byId("previous").addEventListener("click", () => changePage(-1));
byId("quick-prev").addEventListener("click", () => changePage(-1));
byId("next").addEventListener("click", () => changePage(1));
byId("quick-next").addEventListener("click", () => changePage(1));
byId("pagination").addEventListener("submit", e => {
  e.preventDefault();
  const page = byId("page-number").valueAsNumber;
  if (snapshot && Number.isInteger(page) && page >= 1 && (page - 1) * snapshot.limit < snapshot.total)
    load({ q: searchTerm, offset: (page - 1) * snapshot.limit, scroll: true });
});
if (documentId) load({ focus: citationId });
else showMessage("文档链接无效", "请从文档库或回答中的引用重新打开。");
if (embedded) {
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      window.parent.postMessage("reader:close", location.origin);
    }
  });
}
