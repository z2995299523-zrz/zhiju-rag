import MarkdownIt from "./vendor/markdown-it-15.0.3.mjs";

const markdown = new MarkdownIt({ html: false });
const defaultImage = markdown.renderer.rules.image;
const safeImage = /^data:image\/(?:png|jpeg|gif|webp);base64,/i;

markdown.renderer.rules.image = (tokens, index, options, env, renderer) => {
  const token = tokens[index];
  const source = token.attrIndex("src");
  if (source < 0 || !safeImage.test(token.attrs[source][1])) {
    const label = renderer.renderInlineAsText(token.children, options, env);
    return '<span class="inline-image-caption">' + markdown.utils.escapeHtml(label) + '</span>';
  }
  return defaultImage(tokens, index, options, env, renderer);
};

markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  const token = tokens[index];
  const href = token.attrGet("href") || "";
  if (/^https?:\/\//i.test(href)) {
    token.attrSet("target", "_blank");
    token.attrSet("rel", "noopener noreferrer");
  } else {
    token.attrs = token.attrs.filter(([name]) => name !== "href");
  }
  return renderer.renderToken(tokens, index, options);
};

export function renderMarkdownFile(pane, source) {
  const article = document.createElement("article");
  article.className = "markdown-file";
  article.innerHTML = markdown.render(source);
  pane.replaceChildren(article);
}
