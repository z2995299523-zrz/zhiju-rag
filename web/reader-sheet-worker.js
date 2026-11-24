importScripts("/vendor/xlsx-0.20.3.js");

const { read, utils } = self.XLSX;
const ROWS = 60, COLUMNS = 24;
let workbook;

function windowFor(name, startRow, startColumn) {
  if (!workbook.SheetNames.includes(name)) throw new Error("Unknown sheet");
  const sheet = workbook.Sheets[name];
  if (!sheet["!ref"]) return { rows: [], columns: [] };
  const { s, e } = utils.decode_range(sheet["!ref"]);
  const first = {
    r: Math.min(e.r, Math.max(s.r, startRow ?? s.r)),
    c: Math.min(e.c, Math.max(s.c, startColumn ?? s.c)),
  };
  const last = { r: Math.min(e.r, first.r + ROWS - 1), c: Math.min(e.c, first.c + COLUMNS - 1) };
  const merges = new Map();
  for (const merge of sheet["!merges"] || []) {
    const top = Math.max(first.r, merge.s.r), left = Math.max(first.c, merge.s.c);
    const bottom = Math.min(last.r, merge.e.r), right = Math.min(last.c, merge.e.c);
    for (let r = top; r <= bottom; r++) {
      for (let c = left; c <= right; c++) {
        merges.set(r * COLUMNS + c - first.c, r === top && c === left ? {
          r: merge.s.r, c: merge.s.c, rowSpan: bottom - top + 1, colSpan: right - left + 1,
        } : null);
      }
    }
  }
  const columns = [];
  for (let c = first.c; c <= last.c; c++) {
    const width = sheet["!cols"]?.[c]?.wpx;
    columns.push({ name: utils.encode_col(c), width: Number.isFinite(width) ? Math.max(58, Math.min(width, 380)) : 116 });
  }
  const rows = [];
  for (let r = first.r; r <= last.r; r++) {
    const cells = [];
    for (let c = first.c; c <= last.c; c++) {
      const merge = merges.get(r * COLUMNS + c - first.c);
      if (merge === null) continue;
      const address = utils.encode_cell(merge || { r, c });
      const raw = sheet["!data"]?.[merge?.r ?? r]?.[merge?.c ?? c] ?? sheet[address];
      const formula = raw?.f ? "=" + raw.f : "";
      const text = formula && (raw.v == null || raw.t === "z") ? formula : String(raw?.w ?? raw?.v ?? "");
      cells.push({ address, text, formula, rowSpan: merge?.rowSpan || 1, colSpan: merge?.colSpan || 1 });
    }
    rows.push({ number: r + 1, cells });
  }
  return { rows, columns, first, last,
    previousRow: first.r > s.r ? Math.max(s.r, first.r - ROWS) : null,
    nextRow: last.r < e.r ? last.r + 1 : null,
    previousColumn: first.c > s.c ? Math.max(s.c, first.c - COLUMNS) : null,
    nextColumn: last.c < e.c ? last.c + 1 : null };
}

self.onmessage = ({ data }) => {
  const { ticket, buffer, sheet, row, column } = data;
  try {
    if (buffer) workbook = read(buffer, { type: "array", dense: true, cellStyles: true, cellHTML: false, sheetStubs: true });
    const names = workbook.SheetNames;
    const selected = names.includes(sheet) ? sheet : names.find((_, i) => !workbook.Workbook?.Sheets?.[i]?.Hidden) ?? names[0];
    if (!selected) throw new Error("Empty workbook");
    self.postMessage({ ticket, names, selected, view: windowFor(selected, row, column) });
  } catch {
    self.postMessage({ ticket, error: true });
  }
};
