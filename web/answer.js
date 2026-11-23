const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
})[ch]);

function inline(text, citations, allowShortRefs) {
  const token = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[(?:S)?\d+\]|【(?:S)?\d+】)/g;
  return text.split(token).map((part, index) => {
    if (!(index % 2)) return escape(part);
    if (part.startsWith("`")) return "<code>" + escape(part.slice(1, -1)) + "</code>";
    if (part.startsWith("**")) return "<strong>" + inline(part.slice(2, -2), citations, allowShortRefs) + "</strong>";
    if (part.startsWith("*")) return "<em>" + inline(part.slice(1, -1), citations, allowShortRefs) + "</em>";
    const key = part.slice(1, -1);
    const at = citations.findIndex(source => String(source.id) === key || (allowShortRefs && String(source.id) === "S" + key));
    if (at < 0) return escape(part);
    return `<button type="button" class="ev-cite inline-cite" data-citation="${escape(citations[at].id)}" aria-haspopup="dialog" aria-expanded="false" aria-label="查看依据 ${at + 1}">[${at + 1}]</button>`;
  }).join("");
}

function markdownBlocks(text, citations, incomplete) {
  const blocks = [];
  // A numbered footnote copied from a document is distinct from an answer's
  // [S1] citation. Older archived answers can use plain numeric references.
  const allowShortRefs = !incomplete && !/(?:\[S\d+\]|【S\d+】)/.test(text);
  const rich = line => inline(line, citations, allowShortRefs);
  let group = "", lines = [];
  let firstNumber = 1;
  function flush() {
    if (group === "table-text") {
      // Preserve quoted document tables verbatim. API result grids come only from
      // final returned rows, and incomplete model tables stay out of a live turn.
      if (!incomplete) blocks.push("<p>" + lines.map(rich).join("<br>") + "</p>");
    } else if (group === "pre") blocks.push("<pre><code>" + escape(lines.join("\n")) + "</code></pre>");
    else if (group === "ul" || group === "ol") {
      const start = group === "ol" && firstNumber !== 1 ? ' start="' + firstNumber + '"' : "";
      blocks.push("<" + group + start + ">" + lines.map(item => "<li>" + item.map(rich).join("<br>") + "</li>").join("") + "</" + group + ">");
    } else if (group) blocks.push("<" + group + ">" + lines.map(rich).join("<br>") + "</" + group + ">");
    group = ""; lines = [];
  }
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      if (group === "pre") flush();
      else { flush(); group = "pre"; }
      continue;
    }
    if (group === "pre") { lines.push(line); continue; }
    if (!line.trim()) {
      // Markdown permits blank lines between list items. Keep them in one
      // list so both browser numbering and screen-reader counts stay correct.
      if (group !== "ol" && group !== "ul") flush();
      continue;
    }
    if (/^\s*\|/.test(line)) {
      if (group !== "table-text") { flush(); group = "table-text"; }
      lines.push(line);
      continue;
    }
    const heading = /^#{1,6}\s+(.*)/.exec(line);
    if (heading) { flush(); blocks.push("<h4>" + rich(heading[1]) + "</h4>"); continue; }
    const list = /^\s*(?:([-*+]|\d{1,3}[.)])\s+|(\d{1,3}[、）])\s*)(\S.*)/.exec(line);
    if (list) {
      const marker = list[1] || list[2];
      const type = /^\d/.test(marker) ? "ol" : "ul";
      if (group !== type) { flush(); group = type; firstNumber = parseInt(marker, 10); }
      lines.push([list[3]]);
      continue;
    }
    if ((group === "ol" || group === "ul") && /^(?: {2,}|\t)/.test(line)) {
      lines[lines.length - 1].push(line.trimStart());
      continue;
    }
    const quote = /^>\s?(.*)/.exec(line);
    const type = quote ? "blockquote" : "p";
    if (type !== group) { flush(); group = type; }
    lines.push(quote ? quote[1] : line);
  }
  flush();
  return blocks;
}

// Retain completed paragraph nodes, including focused citation buttons, as new
// chunks arrive. Only the unfinished or newly resolved blocks change.
const previousBlocks = new WeakMap();
function updateNode(current, next) {
  if (current.nodeName !== next.nodeName ||
      (next.nodeType === Node.ELEMENT_NODE && current.dataset.citation !== next.dataset.citation)) {
    current.replaceWith(next);
  } else if (next.nodeType === Node.TEXT_NODE) {
    if (next.data.startsWith(current.data)) current.appendData(next.data.slice(current.length));
    else if (current.data !== next.data) current.data = next.data;
  } else {
    if (current.tagName === "OL" && current.start !== next.start) current.start = next.start;
    const children = [...next.childNodes];
    for (let i = 0; i < children.length; i++) {
      if (current.childNodes[i]) updateNode(current.childNodes[i], children[i]);
      else current.append(children[i]);
    }
    while (current.childNodes.length > children.length) current.lastChild.remove();
  }
}

export function renderMarkdown(container, result, incomplete = false) {
  const blocks = markdownBlocks(result.answer || "", result.citations || [], incomplete);
  const previous = previousBlocks.get(container) || [];
  for (let i = 0; i < blocks.length; i++) {
    if (previous[i] === blocks[i]) continue;
    const node = container.children[i];
    if (node) {
      const template = document.createElement("template");
      template.innerHTML = blocks[i];
      updateNode(node, template.content.firstElementChild);
    }
    else container.insertAdjacentHTML("beforeend", blocks[i]);
  }
  while (container.children.length > blocks.length) container.lastElementChild.remove();
  previousBlocks.set(container, blocks);
}

export async function* events(response) {
  if (!response.body || !response.headers.get("Content-Type")?.includes("text/event-stream")) {
    throw new Error("服务未返回回答流，请重试。");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, {stream:!done});
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let type = "message";
        const data = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) type = line.slice(6).trim();
          if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        }
        if (data.length) yield {type, data:JSON.parse(data.join("\n"))};
      }
      if (done) return;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
