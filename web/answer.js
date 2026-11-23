const escape = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
})[ch]);

function inline(text, citations) {
  const token = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[(?:S)?\d+\]|【(?:S)?\d+】)/g;
  return text.split(token).map((part, index) => {
    if (!(index % 2)) return escape(part);
    if (part.startsWith("`")) return "<code>" + escape(part.slice(1, -1)) + "</code>";
    if (part.startsWith("**")) return "<strong>" + inline(part.slice(2, -2), citations) + "</strong>";
    if (part.startsWith("*")) return "<em>" + inline(part.slice(1, -1), citations) + "</em>";
    const key = part.slice(1, -1);
    const at = citations.findIndex(source => String(source.id) === key || String(source.id) === "S" + key);
    if (at < 0) return escape(part);
    return `<button type="button" class="ev-cite inline-cite" data-citation="${escape(citations[at].id)}" aria-haspopup="dialog" aria-expanded="false" aria-label="查看依据 ${at + 1}">[${at + 1}]</button>`;
  }).join("");
}

function markdownBlocks(text, citations, streaming) {
  const blocks = [];
  let group = "", lines = [];
  function flush() {
    if (group === "table-text") {
      // Preserve quoted document tables verbatim. API result grids come only from
      // final returned rows, and incomplete model tables stay out of a live turn.
      if (!streaming) blocks.push("<p>" + lines.map(escape).join("<br>") + "</p>");
    } else if (group === "pre") blocks.push("<pre><code>" + escape(lines.join("\n")) + "</code></pre>");
    else if (group === "ul" || group === "ol") {
      blocks.push("<" + group + ">" + lines.map(line => "<li>" + inline(line, citations) + "</li>").join("") + "</" + group + ">");
    } else if (group) blocks.push("<" + group + ">" + lines.map(line => inline(line, citations)).join("<br>") + "</" + group + ">");
    group = ""; lines = [];
  }
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      if (group === "pre") flush();
      else { flush(); group = "pre"; }
      continue;
    }
    if (group === "pre") { lines.push(line); continue; }
    if (!line.trim()) { flush(); continue; }
    if (/^\s*\|/.test(line)) {
      if (group !== "table-text") { flush(); group = "table-text"; }
      lines.push(line);
      continue;
    }
    const heading = /^#{1,6}\s+(.*)/.exec(line);
    if (heading) { flush(); blocks.push("<h4>" + inline(heading[1], citations) + "</h4>"); continue; }
    const list = /^\s*([-*+]|\d+[.)])\s+(.*)/.exec(line);
    const quote = /^>\s?(.*)/.exec(line);
    const type = list ? (/\d/.test(list[1]) ? "ol" : "ul") : quote ? "blockquote" : "p";
    if (type !== group) { flush(); group = type; }
    lines.push(list ? list[2] : quote ? quote[1] : line);
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
    const children = [...next.childNodes];
    for (let i = 0; i < children.length; i++) {
      if (current.childNodes[i]) updateNode(current.childNodes[i], children[i]);
      else current.append(children[i]);
    }
    while (current.childNodes.length > children.length) current.lastChild.remove();
  }
}

export function renderMarkdown(container, result, streaming = false) {
  const blocks = markdownBlocks(result.answer || "", result.citations || [], streaming);
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
