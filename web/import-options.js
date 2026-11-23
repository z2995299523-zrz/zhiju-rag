export function optionsForm(values = {}) {
  const rows = (values.header_rows || []).filter(Number.isInteger);
  const pages = (values.ocr_pages || []).filter(Number.isInteger);
  return `<div class="mg-fields mg-parse-fields">
    <label>表头行（如 3–4，留空自动识别）<input data-header-rows value="${rows.length ? rows[0] + "-" + rows.at(-1) : ""}"></label>
    <label>补充 OCR 的 PDF 页码（最多 5 页）<input data-ocr-pages value="${pages.join(",")}"></label>
    <label><input type="checkbox" data-headerless ${values.headerless ? "checked" : ""}> 按无表头数据处理</label>
    <label><input type="checkbox" data-hidden ${values.include_hidden ? "checked" : ""}> 包含 Excel 隐藏内容</label>
  </div>`;
}

export function readOptions(root) {
  const options = {};
  const header = root.querySelector("[data-header-rows]").value.trim();
  if (header) {
    const match = /^(\d+)(?:\s*[-–至]\s*(\d+))?$/.exec(header);
    const first = Number(match?.[1]), last = Number(match?.[2] || first);
    if (!match || first < 1 || last > 50000 || last < first || last - first >= 12) throw new Error("请输入连续的表头行，最多 12 行");
    options.header_rows = Array.from({length: last - first + 1}, (_,i) => first + i);
  }
  if (root.querySelector("[data-headerless]").checked) {
    if (header) throw new Error("无表头模式不能同时指定表头行");
    options.headerless = true;
  }
  if (root.querySelector("[data-hidden]").checked) options.include_hidden = true;
  const pages = root.querySelector("[data-ocr-pages]").value.trim();
  if (pages) {
    options.ocr_pages = pages.split(/[，,、\s]+/).map(Number);
    if (options.ocr_pages.length > 5 || new Set(options.ocr_pages).size !== options.ocr_pages.length ||
      options.ocr_pages.some(n => !Number.isInteger(n) || n < 1 || n > 1200)) throw new Error("请输入最多 5 个不同的有效 PDF 页码");
  }
  return options;
}

export function batchDescription({source, options}) {
  const rows = options.header_rows;
  const parts = ["本批来源 · " + (source || "默认来源")];
  if (rows?.length) parts.push("表头第 " + (rows.length > 1 ? rows[0] + "–" + rows.at(-1) : rows[0]) + " 行");
  if (options.headerless) parts.push("无表头");
  if (options.include_hidden) parts.push("包含隐藏内容");
  if (options.ocr_pages?.length) parts.push("补充 OCR 第 " + options.ocr_pages.join("、") + " 页");
  return parts.join(" · ");
}

export function warningsOf(item) {
  const coverage = {indexed:"已提取", partial:"部分提取", review:"需复核"}[item.coverage?.status] || "旧版未记录";
  return ["提取范围：" + coverage, ...(item.warnings || []).map(w =>
    [w.message, (w.locations || []).slice(0,5).join("、")].filter(Boolean).join(" · "))].join("\n");
}
