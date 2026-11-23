const pageSize = 12;
const sourceFormats = new Set(["txt", "md", "csv", "tsv", "json", "jsonl", "ndjson", "htm", "html", "xlsx"]);

export function compareChunks(shell, ctx, file, makeDialog) {
  const { esc, api } = ctx;
  const base = "/api/documents/" + encodeURIComponent(file.id);
  const dialog = makeDialog(shell, ctx, "分块对照 · " + file.filename, `
    <div class="cmp-switch" aria-label="对照视图">
      <button type="button" data-switch="chunks" aria-pressed="true">检索分块</button>
      <button type="button" data-switch="source" aria-pressed="false">原件</button>
    </div>
    <div class="cmp-columns" data-pane="chunks">
      <section class="cmp-panel cmp-source" aria-label="原件">
        <header><h3>原件</h3><a href="${base}/download" download>下载原件</a></header>
        <div class="cmp-location"></div>
        <div class="cmp-sheet-tools" hidden></div>
        <div class="cmp-source-body" tabindex="0" aria-label="原件内容"></div>
      </section>
      <section class="cmp-panel cmp-chunks" aria-label="检索分块">
        <header><h3>检索分块</h3><span class="cmp-count"></span></header>
        <div class="cmp-list" aria-label="选择分块"><p role="status">正在读取分块…</p></div>
        <div class="cmp-pages" hidden>
          <button class="mg-btn mg-small" type="button" data-prev>上一页</button>
          <span aria-live="polite"></span>
          <button class="mg-btn mg-small" type="button" data-next>下一页</button>
        </div>
      </section>
    </div>`, "mg-compare");
  const columns = dialog.querySelector(".cmp-columns");
  const list = dialog.querySelector(".cmp-list");
  const source = dialog.querySelector(".cmp-source-body");
  const location = dialog.querySelector(".cmp-location");
  const sheetTools = dialog.querySelector(".cmp-sheet-tools");
  const pages = dialog.querySelector(".cmp-pages");
  const previous = pages.querySelector("[data-prev]");
  const next = pages.querySelector("[data-next]");
  const extension = file.filename.split(".").pop().toLowerCase();
  const life = new AbortController();
  let sourceRequest;
  let data;
  let selected;
  let busy = false;
  dialog.addEventListener("close", () => { life.abort(); sourceRequest?.abort(); }, { once:true });

  function switchPane(name) {
    columns.dataset.pane = name;
    dialog.querySelectorAll("[data-switch]").forEach(button =>
      button.setAttribute("aria-pressed", String(button.dataset.switch === name)));
  }
  dialog.querySelectorAll("[data-switch]").forEach(button =>
    button.addEventListener("click", () => switchPane(button.dataset.switch)));

  function drawWindow(view, focusColumn) {
    const selectedRow = n => view.first !== null && n >= view.first && n <= view.last;
    const rowFlag = row => selectedRow(row.number) ? ' class="cmp-match"' : "";
    if (view.kind === "sheet") {
      const fields = view.fields || [];
      const focused = fields.find(f => f.column === focusColumn);
      const focusLetter = focused?.address.match(/^[A-Z]+/)?.[0];
      source.innerHTML = `<table class="cmp-grid"><caption>${esc(view.sheet)} · 原件单元格</caption>
        <thead><tr><th scope="col">行</th>${view.columns.map(c => '<th scope="col">' + esc(c) + '</th>').join("")}</tr></thead>
        <tbody>${view.rows.map(row => `<tr${rowFlag(row)}><th scope="row">${esc(row.number)}</th>
          ${row.cells.map((c, i) => '<td' + (selectedRow(row.number) && view.columns[i] === focusLetter ? ' class="cmp-field-match"' : '') + '>' + esc(c) + '</td>').join("")}</tr>`).join("")}</tbody></table>`;
      sheetTools.hidden = !(fields.length || view.previous_column || view.next_column);
      sheetTools.innerHTML = fields.length ? `<label>定位字段 <select aria-label="定位原件字段">
        <option value="">选择字段</option>${fields.map(f => `<option value="${Number(f.column)}" ${f.column === focusColumn ? "selected" : ""}>${esc(f.address)} · ${esc(f.label)}</option>`).join("")}
        </select></label>` : "";
      sheetTools.querySelector("select")?.addEventListener("change", e => {
        if (e.target.value) showSource(data.chunks[selected], Number(e.target.value), true);
      });
    } else {
      source.innerHTML = (view.first === null ? '<p class="cmp-note">原文件开头 · 此分块记录了字段路径，没有行号。</p>' : "") +
        '<div class="cmp-raw">' + view.rows.map(row => `<div${rowFlag(row)}>
          <span class="cmp-ln">${esc(row.number)}</span><code>${row.prefix_clipped ? "…" : ""}${esc(row.text) || " "}${row.suffix_clipped ? "…" : ""}</code></div>`).join("") + "</div>";
    }
    if (view.kind === "sheet") {
      for (const [label, col] of [["上一组列",view.previous_column],["下一组列",view.next_column]]) {
        if (!col) continue;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "mg-btn mg-small";
        button.textContent = label;
        button.onclick = () => showSource(data.chunks[selected], col);
        sheetTools.append(button);
      }
    }
    if (view.range_clipped && view.kind !== "sheet") source.insertAdjacentHTML("beforeend", '<p class="cmp-note">此节点较长，完整内容可下载原件核对。</p>');
    source.scrollTop = 0;
    source.scrollLeft = 0;
    const first = source.querySelector(".cmp-match");
    if (first && first.getBoundingClientRect().top > source.getBoundingClientRect().bottom - 30) {
      source.scrollTop += first.getBoundingClientRect().top - source.getBoundingClientRect().top - 24;
    }
  }

  async function showSource(chunk, column = null, focusField = false) {
    sourceRequest?.abort();
    const request = new AbortController();
    sourceRequest = request;
    location.textContent = chunk.locator;
    sheetTools.hidden = true;
    if (data.original_kind === "pdf" || data.original_kind === "image") {
      const pdf = data.original_kind === "pdf";
      const page = pdf && /^第 (\d+) 页/.exec(chunk.locator)?.[1];
      const url = base + "/original" + (page ? "#page=" + page : "");
      location.insertAdjacentHTML("beforeend", ` <a href="${url}" target="_blank" rel="noopener">打开原版</a>`);
      if (source.firstElementChild?.getAttribute("src") === url) return;
      const preview = document.createElement(pdf ? "iframe" : "img");
      preview.setAttribute(pdf ? "title" : "alt", file.filename);
      preview.src = url;
      source.replaceChildren(preview);
      return;
    }
    if (!sourceFormats.has(extension)) {
      source.innerHTML = '<p class="cmp-note">此格式需要本机应用显示原始版式。请下载原件，按上方位置核对所选分块。</p>';
      return;
    }
    source.innerHTML = '<p class="cmp-note" role="status">正在读取原件位置…</p>';
    try {
      const params = new URLSearchParams({chunk_id:chunk.id});
      if (column) params.set("column", column);
      const view = await api(base + "/source-window?" + params, { signal: request.signal });
      if (!request.signal.aborted && dialog.isConnected) drawWindow(view, focusField ? column : null);
    } catch (error) {
      if (request.signal.aborted || !dialog.isConnected) return;
      source.innerHTML = `<p class="cmp-note" role="alert">${esc(error.message || "原件位置读取失败")}</p>
        <button class="mg-btn mg-small" type="button">重试读取原件</button>`;
      source.querySelector("button").addEventListener("click", () => showSource(chunk, column, focusField));
    }
  }

  function selectChunk(index, userAction) {
    selected = index;
    [...list.querySelectorAll(".cmp-chunk")].forEach((button, i) =>
      button.setAttribute("aria-pressed", String(i === selected)));
    showSource(data.chunks[index]);
    if (userAction && window.matchMedia("(max-width: 720px)").matches) {
      switchPane("source");
      dialog.querySelector('[data-switch="source"]').focus({preventScroll:true});
    }
  }

  async function load(offset) {
    if (busy) return;
    busy = true;
    previous.disabled = next.disabled = true;
    try {
      const page = await api(base + "/preview?limit=" + pageSize + "&offset=" + offset, { signal: life.signal });
      if (life.signal.aborted || !dialog.isConnected) return;
      data = page;
      dialog.querySelector(".cmp-count").textContent = "共 " + data.total + " 块";
      pages.hidden = data.total <= pageSize;
      pages.querySelector("span").textContent = (Math.floor(data.offset / pageSize) + 1) + " / " + Math.ceil(data.total / pageSize);
      previous.disabled = data.offset === 0;
      next.disabled = data.offset + data.chunks.length >= data.total;
      list.innerHTML = data.chunks.map((chunk, i) => `<button type="button" class="cmp-chunk" aria-pressed="false">
        <span class="cmp-chunk-heading"><strong>分块 ${data.offset + i + 1}</strong><span>${esc(chunk.locator)}</span></span>
        <span class="cmp-excerpt">${esc([chunk.context,chunk.text].filter(Boolean).join("\n"))}</span></button>`).join("") || '<p>此版本没有可检索分块。</p>';
      list.querySelectorAll(".cmp-chunk").forEach((button, i) => button.addEventListener("click", () => selectChunk(i, true)));
      list.scrollTop = 0;
      if (data.chunks.length) selectChunk(0, false);
    } catch (error) {
      if (life.signal.aborted || !dialog.isConnected) return;
      sourceRequest?.abort();
      source.replaceChildren();
      location.textContent = "";
      pages.hidden = true;
      list.innerHTML = `<p role="alert">${esc(error.message || "分块读取失败")}</p><button class="mg-btn mg-small" type="button">重试</button>`;
      list.querySelector("button").addEventListener("click", () => load(offset));
    } finally { busy = false; }
  }
  previous.addEventListener("click", () => load(Math.max(0, data.offset - pageSize)));
  next.addEventListener("click", () => load(data.offset + pageSize));
  load(0);
}
