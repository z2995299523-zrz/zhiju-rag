import assert from "node:assert/strict";
import test from "node:test";
import { renderMarkdown, highlightedExcerpt } from "../web/answer.js";

test("two inline citations on the same source highlight separate original ranges including non-BMP text", () => {
  const text = "📄头<原件>甲与乙";
  const source = { highlights:[{ranges:[{start:6,end:7}]},{ranges:[{start:8,end:9}]}] };
  assert.equal(highlightedExcerpt(source,text,0), "📄头&lt;原件&gt;<mark>甲</mark>与乙");
  assert.equal(highlightedExcerpt(source,text,1), "📄头&lt;原件&gt;甲与<mark>乙</mark>");
  assert.equal(highlightedExcerpt(source,text), "📄头&lt;原件&gt;<mark>甲</mark>与<mark>乙</mark>");
  assert.equal(highlightedExcerpt({},text), "<mark>📄头&lt;原件&gt;甲与乙</mark>");
});

function render(answer, citations = [], incomplete = false) {
  const blocks = [];
  const container = { children: blocks, insertAdjacentHTML: (_, html) => blocks.push(html) };
  renderMarkdown(container, { answer, citations }, incomplete);
  return blocks;
}

test("direct lead and comparison keep paragraph breaks, labels and attached citations", () => {
  const blocks = render("分年度如下。\n\n- **2023 年**：93267 万人。 [S1]\n\n- **2024 年**：94350 万人。 [S2]", [{id:"S1"}, {id:"S2"}]);
  assert.equal(blocks[0], "<p>分年度如下。</p>");
  assert.equal(blocks.length, 2);
  assert.match(blocks[1], /^<ul><li><strong>2023 年<\/strong>.*data-citation="S1".*<\/li><li><strong>2024 年<\/strong>.*data-citation="S2".*<\/li><\/ul>$/);
  assert.deepEqual(render("93267 万人。"), ["<p>93267 万人。</p>"]);
});

test("loose ordered steps stay in one list and retain their starting number", () => {
  const blocks = render("3. 打开导航。\n  选择标题。\n\n4. 检查层级。\n\n2024. 上年记录");
  assert.deepEqual(blocks, ['<ol start="3"><li>打开导航。<br>选择标题。</li><li>检查层级。</li></ol>', "<p>2024. 上年记录</p>"]);
  assert.deepEqual(render("1、打开\n\n2、检查"), ["<ol><li>打开</li><li>检查</li></ol>"]);
});

test("archived aliases remain usable and copied footnotes do not become source links", () => {
  const sources = [{id:"S1"}];
  assert.match(render("年度说明 [1]。见资料 [S1]", sources)[0], /说明 \[1\]。见资料 <button/);
  assert.doesNotMatch(render("年度说明 [1]", sources, true)[0], /button/);
  assert.match(render("旧回答【1】", sources)[0], /data-citation="S1"/);
  assert.doesNotMatch(render("未返回的来源 [S9]", sources)[0], /button/);
  assert.match(render("<b>原文</b> [S1]", sources)[0], /&lt;b&gt;原文&lt;\/b&gt;/);
});

test("partial tables stay out of a draft and completed source text keeps its citation", () => {
  const sources = [{id:"S1"}];
  const answer = "前文。\n\n| 城镇人口 | 93267 万人 [S1] |";
  assert.deepEqual(render(answer, sources, true), ["<p>前文。</p>"]);
  assert.match(render(answer, sources)[1], /93267 万人 <button[^>]*data-citation="S1"/);
});
