function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(text) {
  const node = element("button", "btn", text);
  node.type = "button";
  return node;
}

function onDispose(signal, dispose) {
  signal.addEventListener("abort", dispose, { once: true });
  window.addEventListener("pagehide", event => { if (!event.persisted) dispose(); }, { signal });
}

export async function renderSlides(pane, bytes, locator, signal) {
  const { PptxRenderer } = await import("./vendor/pptx-browser-4.1.5.mjs");
  if (signal.aborted) return;
  const deck = new PptxRenderer();
  onDispose(signal, () => deck.destroy());
  await deck.load(bytes);
  if (signal.aborted) { deck.destroy(); return; }
  const { cx, cy } = deck.slideSize;
  if (!deck.slideCount || deck.slideCount > 1200 || !(cx > 0 && cy > 0)) throw new Error("Invalid slide size");
  const indexFromCitation = Number(/^第\s*(\d+)\s*张幻灯片/.exec(locator || "")?.[1]) - 1;
  const width = Math.max(1, Math.min(1280, Math.floor(1920 * cx / cy)));
  const tools = element("div", "file-view-tools");
  const previous = button("上一张"), next = button("下一张");
  const picker = element("select", "view-select");
  picker.setAttribute("aria-label", "跳转到幻灯片");
  for (let i = 0; i < deck.slideCount; i++) picker.add(new Option(`第 ${i + 1} 张 / ${deck.slideCount}`, i));
  tools.append(previous, picker, next);
  const surface = element("div", "slide-surface");
  pane.replaceChildren(tools, surface);

  async function show(index) {
    picker.value = index;
    previous.disabled = picker.disabled = next.disabled = true;
    const status = element("p", "pane-status", "正在绘制幻灯片…");
    status.setAttribute("role", "status");
    surface.replaceChildren(status);
    const canvas = element("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `第 ${index + 1} 张幻灯片`);
    try {
      await deck.renderSlide(index, canvas, width);
      if (!signal.aborted) surface.replaceChildren(canvas);
    } catch {
      if (!signal.aborted) status.textContent = "此幻灯片无法预览，可以继续翻页或下载原件。";
    } finally {
      previous.disabled = index === 0;
      picker.disabled = false;
      next.disabled = index + 1 >= deck.slideCount;
    }
  }
  previous.onclick = () => void show(Number(picker.value) - 1);
  next.onclick = () => void show(Number(picker.value) + 1);
  picker.onchange = () => void show(Number(picker.value));
  await show(indexFromCitation >= 0 && indexFromCitation < deck.slideCount ? indexFromCitation : 0);
}

function citeCell(locator) {
  const match = /^工作表「(.+)」 · ([A-Z]{1,3})(\d+)/s.exec(locator || "");
  if (!match) return {};
  const [, sheet, col, row] = match;
  const column = [...col].reduce((value, c) => value * 26 + c.charCodeAt(0) - 64, 0) - 1;
  return { sheet, column, row: Number(row) - 1, address: col + row };
}

function makeTable(view, address) {
  const table = element("table", "source-grid");
  table.setAttribute("aria-label", "工作表单元格");
  const header = table.createTHead().insertRow();
  const corner = element("th", "row-index");
  corner.setAttribute("aria-label", "行号");
  header.append(corner);
  for (const column of view.columns) {
    const head = element("th", "", column.name);
    head.scope = "col";
    head.style.minWidth = column.width + "px";
    header.append(head);
  }
  const body = table.createTBody();
  for (const row of view.rows) {
    const tr = body.insertRow();
    const number = element("th", "row-index", String(row.number));
    number.scope = "row";
    tr.append(number);
    for (const cell of row.cells) {
      const td = tr.insertCell();
      const content = element("div", "sheet-value", cell.text);
      td.rowSpan = cell.rowSpan;
      td.colSpan = cell.colSpan;
      td.title = cell.address + (cell.formula ? " · " + cell.formula : "");
      td.classList.toggle("sheet-cited", cell.address === address);
      td.append(content);
    }
  }
  return table;
}

export function renderWorkbook(pane, bytes, locator, signal) {
  const worker = new Worker("/reader-sheet-worker.js");
  const citation = citeCell(locator);
  const tools = element("div", "file-view-tools");
  const picker = element("select", "view-select");
  picker.setAttribute("aria-label", "工作表");
  const previousRow = button("上一组行"), nextRow = button("下一组行");
  const previousCol = button("上一组列"), nextCol = button("下一组列");
  const range = element("span", "sheet-range");
  range.setAttribute("aria-live", "polite");
  const controls = [picker, previousRow, nextRow, previousCol, nextCol];
  tools.append(...controls, range);
  const grid = element("div", "sheet-scroll");
  grid.tabIndex = 0;
  grid.setAttribute("aria-label", "工作表预览区域");
  pane.replaceChildren(tools, grid);
  let requestId = 0, timer;

  function stop() { clearTimeout(timer); worker.terminate(); }
  onDispose(signal, stop);

  function fail() {
    stop();
    grid.replaceChildren(element("p", "pane-status", "工作簿读取失败，可以下载原件查看。"));
    range.textContent = "";
  }

  function read({ sheet, row, column }, buffer) {
    for (const control of controls) control.disabled = true;
    range.textContent = "正在读取工作表…";
    clearTimeout(timer);
    timer = setTimeout(fail, 30000);
    worker.postMessage({ ticket: ++requestId, sheet, row, column, buffer }, buffer ? [buffer] : []);
  }

  worker.onerror = fail;
  worker.onmessage = ({ data }) => {
    if (data.ticket !== requestId || signal.aborted) return;
    clearTimeout(timer);
    if (data.error) { fail(); return; }
    const { names, selected, view } = data;
    if (!picker.options.length) for (const name of names) picker.add(new Option(name, name));
    picker.value = selected;
    picker.disabled = false;
    for (const [control, direction, coordinates] of [
      [previousRow, view.previousRow, { row: view.previousRow, column: view.first?.c }],
      [nextRow, view.nextRow, { row: view.nextRow, column: view.first?.c }],
      [previousCol, view.previousColumn, { row: view.first?.r, column: view.previousColumn }],
      [nextCol, view.nextColumn, { row: view.first?.r, column: view.nextColumn }],
    ]) {
      control.disabled = direction == null;
      control.onclick = () => read({ sheet: selected, ...coordinates });
    }
    if (view.rows.length) {
      const first = view.columns[0].name + view.rows[0].number;
      const last = view.columns.at(-1).name + view.rows.at(-1).number;
      range.textContent = first + " – " + last;
      grid.replaceChildren(makeTable(view, selected === citation.sheet ? citation.address : null));
      grid.scrollTo(0, 0);
    } else {
      range.textContent = "";
      grid.replaceChildren(element("p", "pane-status", "此工作表为空。"));
    }
  };
  picker.onchange = () => read({ sheet: picker.value });
  read(citation, bytes);
}
