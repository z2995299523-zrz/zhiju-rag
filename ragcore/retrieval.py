"""Deterministic bilingual retrieval and read-only query planning.

The planner produces parameter values only. Connector URLs, authentication and
request construction remain under the connector's control.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePath


_HAN = r"\u3400-\u9fff"
_TOKENS = re.compile(rf"[{_HAN}]+|[a-z0-9]+(?:[._/-][a-z0-9]+)*")
_EN_STOP = set(
    "a an the and or of by at on in from for to with is are be was were do does "
    "did how what why which where when who can could should would please me my "
    "show tell give find search look up about according information".split()
)
# Replace phrases with boundaries so that joins do not create artificial matches.
_ZH_STOP = re.compile(
    "请帮我|麻烦帮我|告诉我|帮我|请问|麻烦|我想知道|我想了解|我想查|"
    "能否|能不能|能不能够|可以|是否|需要|想要|哪些|什么|怎么|如何|"
    "多少|怎样|有无|一下|有关|相关|根据|请|以及|并且|和|的|了|吗|呢"
)
_SYNONYMS = {
    "报账": "报销", "报帐": "报销", "票据": "发票",
    "所需资料": "所需材料", "报销资料": "报销材料", "报销凭据": "报销凭证",
    "计算方法": "计算公式", "计算方式": "计算公式", "怎么算": "计算公式",
    "开支": "支出", "结余": "余额", "财年": "年度",
    "reimbursements": "reimbursement", "reimburse": "reimbursement",
    "invoices": "invoice", "expenses": "expense", "budgets": "budget",
}
_SYN_RE = re.compile("|".join(
    r"\b" + re.escape(term) + r"\b" if term.isascii() else re.escape(term)
    for term in sorted(_SYNONYMS, key=len, reverse=True)
))
_GENERIC_TERMS = {"项目", "数据", "管理", "信息", "情况", "结果", "查询", "接口",
                  "文件", "文档", "资料", "分析", "project", "data", "query", "api"}


def _normal(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _canonical(value: object) -> str:
    return _SYN_RE.sub(lambda match: _SYNONYMS[match.group()], _normal(value))


def _tokens(value: object) -> Counter[str]:
    text = _ZH_STOP.sub(" ", _canonical(value))
    terms: Counter[str] = Counter()
    for match in _TOKENS.finditer(text):
        word = match.group()
        if re.fullmatch(rf"[{_HAN}]+", word):
            if len(word) == 1:
                terms[word] += 1
            else:
                terms.update(word[i:i + 2] for i in range(len(word) - 1))
        elif word not in _EN_STOP:
            terms[word] += 1
    return terms


def index_terms(value: str) -> list[str]:
    """Index raw tokens and additional canonical forms with term frequencies."""
    terms: Counter[str] = Counter()
    for match in re.finditer(rf"[{_HAN}]+|[a-z0-9_]+", _normal(value)):
        word = match.group()
        if len(word) > 1 and re.fullmatch(rf"[{_HAN}]+", word):
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms[word] += 1
    return list((terms | _tokens(value)).elements())


def _factor(term: str) -> float:
    if term in _GENERIC_TERMS:
        return 0.2
    if len(term) == 1:
        return 0.35
    return 1.0


def structural_text(chunk):
    """Rank by original structural labels without treating page or row numbers as facts."""
    parts = [str(chunk.get("source_key") or ""), str(chunk.get("context") or "")]
    locator = str(chunk.get("locator") or "")
    if "JSON Pointer /" in locator:
        pointer = locator.partition("JSON Pointer ")[2].split(" · ")[0]
        keys = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/") if p and not p.isdigit()]
        parts.extend(keys)
        if any(re.search(r"date|dtstart|dtend|日期", k, re.I) for k in keys):
            raw_date = re.fullmatch(r'\s*"?(\d{4})(\d{2})(\d{2})"?\s*', str(chunk.get("text", "")))
            if raw_date:
                parts.append(f"日期 {raw_date[1]} 年 {raw_date[2]} 月 {raw_date[3]} 日")
    return " ".join(parts).strip()


_LIMIT = re.compile(r"上限|限额|下限|封顶|最高|最低|最多|至少|不超过|\b(?:maximum|minimum|limit|cap)\b")
_ASK_NUMBER = re.compile(r"多少|多少钱|几(?:元|万|人|个|天|件|笔)|具体数值|多高|"
                         r"\b(?:how much|how many)\b")
_MEASURE = re.compile(r"(?:预算|资金|金额|执行率|支出|余额|营业额|收入|成本|费用|库存|销量|"
                      r"销售额|单价|价格|人数|增长率|\b(?:budget|revenue|rate|balance|inventory)\b)")
_NUMBER = re.compile(r"(?<![\d.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?|"
                     r"[零〇一二三四五六七八九十百千万两]+(?=元|万|亿|人|件|天|个|美元)")
_UNLIMITED = re.compile(r"不设(?:上|下)?限|(?:没|无|取消)(?:有)?(?:上限|下限|限额)|"
                        r"不封顶|不限(?:额|量)?|\b(?:unlimited|no (?:maximum|limit|cap))\b")
_FOUNDER = re.compile(r"创始|创办|创立|发起人|\bfound(?:er|ers|ed)\b")
_PHONE = re.compile(r"电话|联系号码|手机号码|热线|总机|\b(?:phone|telephone|hotline)\b")
_EMAIL = re.compile(r"电子邮件|邮箱|\be-?mail\b")


def _has_quantity(text: str) -> bool:
    text = re.sub(r"(?<!\d)(?:19|20)\d{2}(?:\s*[-/]\s*\d{1,2}){1,2}(?!\d)|"
                  r"(?<!\d)(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?)?",
                  " ", text)
    for match in _NUMBER.finditer(text):
        if re.search(r"(?:[×*/÷]|乘以)\s*$", text[:match.start()]):
            continue
        return True
    return False


def filter_evidence(question: str, chunks: list[dict]) -> list[dict]:
    """Exclude chunks incompatible with high-confidence requested fact types.

    Lexical similarity alone cannot establish a phone number, numerical limit
    or a project-specific observation. Apply these checks to semantic hits too.
    """
    q = _normal(question)
    asks_number = bool(_ASK_NUMBER.search(q))
    asks_limit = bool(_LIMIT.search(q)) and (asks_number or bool(re.search(r"\bwhat (?:is|was)\b", q)))
    asks_measure = asks_number and bool(_MEASURE.search(q)) and not (_DOC.search(q) or asks_limit)
    contact_procedure = re.search(r"填写|修改|更新|变更|脱敏|维护|格式|\b(?:update|change|format|mask)\b", q)
    asks_phone = not contact_procedure and bool(_PHONE.search(q))
    asks_email = not contact_procedure and bool(_EMAIL.search(q))
    asks_founder = bool(_FOUNDER.search(q))
    asks_visits = asks_number and "人次" in q
    asks_shortcut = bool(re.search(r"快捷键(?:是(?:什么|哪)|为|有哪)|(?:which|what) (?:keyboard )?shortcut", q))
    needs_fact = any((asks_limit, asks_measure, asks_phone, asks_email, asks_founder, asks_visits, asks_shortcut))
    years = set(_years_from_text(q)) if asks_measure else set()
    projects = _projects_from_text(q, demo=True) if asks_measure else []
    compatible = []
    for chunk in chunks:
        text = str(chunk.get("text", "")).strip()
        # A standalone Markdown heading can locate a section, but contains
        # none of the section's actual evidence.
        if (text.startswith("#") and all(re.match(r"^#{1,6}(?:\s|$)", line.strip())
                                         for line in text.splitlines() if line.strip())):
            continue
        if not text or text == PurePath(str(chunk.get("filename", ""))).stem:
            continue
        if not needs_fact:
            compatible.append(chunk)
            continue
        body = _normal(text)
        location = _normal(str(chunk.get("filename", "")) + " " + structural_text(chunk))
        evidence = body + " " + location
        if asks_shortcut and not re.search(r"\b(?:ctrl|control|alt|shift|cmd|command|windows)\s*[+‐-]\s*\w|"
                                          r"\b(?:f(?:[1-9]|1[0-2])|enter|escape|tab|delete)\b|回车键|空格键", body):
            continue
        if asks_visits and not re.search(r"人次|客流|passenger(?:s| (?:trips|journeys))|ridership|visits", evidence, re.I):
            continue
        if asks_limit and not _UNLIMITED.search(body):
            bound = _LIMIT.search(evidence) or re.search(
                r"每人|每日|每天|每晚|每次|标准|定额|以内|不得高于|不得低于|\bper (?:day|night|person)\b", evidence
            )
            if not bound or not _has_quantity(body):
                continue
        if asks_measure and not _has_quantity(body):
            continue
        if years and not any(re.search(rf"(?<!\d){year}(?!\d)", evidence) for year in years):
            continue
        if projects and not any(project in evidence for project in projects):
            continue
        if asks_phone and not (_PHONE.search(evidence) and re.search(r"\d[\d() -]{4,}\d", body)):
            continue
        if asks_email and not re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", body):
            continue
        if asks_founder and not _FOUNDER.search(body):
            continue
        compatible.append(chunk)
    return compatible


def rank_chunks(question: str, chunks: list[dict], limit: int = 5) -> list[dict]:
    """Return relevant source chunks ranked with BM25 and query-term coverage."""
    query = _tokens(question)
    if not query or not chunks or limit <= 0:
        return []
    candidates = []
    # Foreign names and labels embedded in a question often identify a precise object
    # while the remainder of the question is in another language.
    phrases = []
    if re.search(r"[\u3400-\u9fff]", question):
        phrases = [m[0].lower() for m in re.finditer(
            r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9'-]*(?:[ \t]+[A-Za-z0-9][A-Za-z0-9'-]*)+", question)]
    frequencies: Counter[str] = Counter()
    title_cache: dict[str, Counter[str]] = {}
    for chunk in filter_evidence(question, chunks):
        text = str(chunk.get("text", ""))
        filename = str(chunk.get("filename", ""))
        contextual = structural_text(chunk)
        body = _tokens(" ".join((text, contextual)))
        if not body:
            continue
        if filename not in title_cache:
            title_cache[filename] = _tokens(PurePath(filename).stem)
        title = title_cache[filename]
        matched = query.keys() & (body.keys() | title.keys())
        frequencies.update(matched)
        anchors = sum(1 + bool(re.search(r"\d", p)) for p in phrases if p in _normal(text + " " + contextual))
        candidates.append((chunk, body, title, matched, sum(body.values()), anchors))
    if not candidates:
        return []

    count = len(candidates)
    mean_length = sum(entry[4] for entry in candidates) / count
    # Retain a floor on IDF so that a useful word occurring in all pages of a
    # single imported document can still contribute to coverage.
    weights = {
        term: _factor(term) * (0.65 + math.log1p(
            (count - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
        )) for term in query
    }
    total = sum(_factor(term) for term in query)
    ranked = []
    for chunk, body, title, matched, length, anchors in candidates:
        substantive = [term for term in matched if _factor(term) >= 0.35]
        if not substantive or not any(term in body for term in substantive):
            continue
        coverage = sum(_factor(term) for term in matched) / total
        # One accidental shared Chinese bigram in a long question is weak
        # evidence. Require more overlap for generic or long queries.
        if not anchors and (coverage < 0.16 or (len(substantive) == 1 and coverage < 0.31)):
            continue
        norm = 1.2 * (0.30 + 0.70 * length / mean_length)
        score = 0.0
        for term in matched:
            tf = body[term] + 0.65 * min(title[term], 2)
            score += weights[term] * 2.2 * tf / (tf + norm)
        score = score * (0.45 + 0.55 * coverage) + 3 * anchors
        ranked.append({**chunk, "score": round(score, 5)})
    ranked.sort(key=lambda entry: entry["score"], reverse=True)
    return ranked[:limit]


@dataclass
class DocumentRanking:
    candidates: list[dict]
    ranked: list[dict]
    candidate_method: str
    ranking_method: str


# asyncio.to_thread inherits the evaluation context, including its per-case collector.
_observed_rankings = ContextVar("observed_rankings", default=None)


@contextmanager
def observe_rankings():
    rankings = []
    token = _observed_rankings.set(rankings)
    try:
        yield rankings
    finally:
        _observed_rankings.reset(token)


def rank_document_chunks(store, question: str) -> DocumentRanking:
    """The query, admin diagnostic and offline evaluation share this ranking pass."""
    from . import models

    q = _normal(question)
    # Bridge the standard calendar field names that rarely occur literally in a Chinese question.
    aliases = []
    if "时区" in q:
        aliases.extend(("timezone", "tzid"))
    if "历法" in q:
        aliases.extend(("calscale", "calendar scale"))
    if "超链接" in q:
        aliases.append("hyperlink")
    if "快捷键" in q:
        aliases.append("shortcut")
    if "插入" in q and aliases:
        aliases.append("insert")
    if "窗口" in q and aliases:
        aliases.append("window")
    for term, words in store.document_terms().items():
        needle = re.escape(_normal(term.strip()))
        if term.isascii():
            needle = rf"(?<![a-z0-9_]){needle}(?![a-z0-9_])"
        if re.search(needle, q):
            aliases.extend(_normal(word.strip()) for word in words)
    search_query = " ".join([question, *aliases]) if aliases else question
    candidates, method = store.candidates(search_query, with_method=True)
    scoped = {c["source_key"] for c in candidates if c.get("source_key")
              and len(str(c["source_key"]).strip()) > 1 and _normal(c["source_key"]) in q}
    if scoped and not re.search(r"除了|排除|不含|except|excluding", question, re.I):
        candidates = [c for c in candidates if c.get("source_key") in scoped]
    lexical = rank_chunks(search_query, candidates, limit=8)
    if aliases:
        # A literal filename in the question can outrank the translated field/procedure
        # even on a contents page. Rank its actual English fields separately.
        alternative = rank_chunks(" ".join(aliases), filter_evidence(question, candidates), limit=3)
        lexical = list({item["id"]:item for item in [*alternative, *lexical]}.values())[:8]
    semantic = None
    ranking_method = "lexical"
    if candidates and models.embedding_enabled():
        # Embed a bounded shortlist on large collections so one cold question does not
        # exhaust the upstream deadline by re-encoding 180 passages. Keep exact lexical hits.
        pool = candidates
        if method == "fts5":
            pool = list({c["id"]: c for c in [*lexical, *candidates[:32]]}.values())
        semantic = models.semantic_rank(question, filter_evidence(question, pool), limit=8)
        ranking_method = "hybrid" if semantic else "lexical_fallback"
    if semantic:
        # Fuse positions because lexical scores and cosines use different scales.
        scores, items = {}, {}
        for ranking in (lexical, semantic):
            for place, item in enumerate(ranking):
                key = item["id"]
                scores[key] = scores.get(key, 0) + 1 / (20 + place)
                items[key] = item
        ranked = [items[key] for key in sorted(scores, key=scores.get, reverse=True)]
    else:
        # Keeping the best three avoids letting one unusually short heading eliminate
        # its data row. The lexical stage already applies a minimum coverage gate.
        ranked = [item for index, item in enumerate(lexical)
                  if index < 3 or item["score"] >= lexical[0]["score"] * 0.78]
    ranked = _prefer_requested_records(q, ranked)
    if aliases:
        ranked.sort(key=lambda item: -sum(alias in _normal(item["text"]) for alias in aliases))
    result = DocumentRanking(candidates, ranked, method, ranking_method)
    from .query_trace import stage_metrics
    stage_metrics(candidate_count=len(candidates), ranking_method=ranking_method,
                  lexical_ids=[c["id"] for c in lexical], ranked_ids=[c["id"] for c in ranked[:8]])
    observed = _observed_rankings.get()
    if observed is not None:
        observed.append(result)
    return result


def _prefer_requested_records(question, ranked):
    # Nearby rows or sibling sections often share every measure. When the question names
    # a cell or leaf heading, exclude its unmatched siblings within the same document.
    def group(item):
        return item.get("document_id", item.get("filename")), (item.get("metadata") or {}).get("sheet")

    def table_row(item):
        meta = item.get("metadata") or {}
        if "header_rows" in meta and "row" in meta:
            location = "" if meta.get("sheet") else str(item.get("locator") or "").partition(" · 第 ")[0]
            return group(item), location, meta["row"]

    def requested(item):
        meta = item.get("metadata") or {}
        if meta.get("record_identifiers"):
            for identifier in meta["record_identifiers"]:
                value = _normal(identifier.get("value")).strip()
                if not value or len(value) > 180:
                    continue
                if value.isdigit() and not (str(identifier.get("key") or "").casefold() in question
                                              or re.search(r"编号|编码|代码|\bid\b|\bcode\b", question)):
                    continue
                if re.search(r"(?<![a-z0-9_-])" + re.escape(value) + r"(?![a-z0-9_-])", question):
                    return True
        if "header_rows" in meta:
            fields = item["text"].split(" ｜ ")
            if " ｜ " in (item.get("context") or ""):
                fields.extend(item["context"].split(" ｜ "))
            for field in fields:
                value = _normal(field.partition("：")[2]).strip()
                parts = [s.strip() for s in value.split(",")]
                if len(value) >= 2 and any(c.isalpha() for c in value) and all(p and p in question for p in parts):
                    return True
        elif not meta:
            leaf = _normal(item.get("context")).split(" / ")[-1].strip()
            return len(leaf) >= 2 and leaf in question
        return False

    matches = [requested(item) for item in ranked]
    scoped = {group(item) for item, match in zip(ranked, matches) if match}
    # An entity cell can be in an earlier column group than the requested measure.
    # Keep the original ranking among all groups belonging to the same physical row.
    rows = {row for item, match in zip(ranked, matches) if match and (row := table_row(item)) is not None}
    return [item for item, match in zip(ranked, matches) if match or group(item) not in scoped
            or table_row(item) in rows]


def validate_evidence_labels(labels):
    """Each label identifies one required piece of evidence; fields match exactly."""
    if not isinstance(labels, list) or not labels:
        raise ValueError("文档评测须标注至少一项预期依据")
    seen = set()
    for label in labels:
        if not isinstance(label, dict) or set(label) - {"filename", "sha256", "locator", "chunk_id"}:
            raise ValueError("预期依据只支持 filename、sha256、locator 和 chunk_id")
        if not isinstance(label.get("filename"), str) or not label["filename"].strip():
            raise ValueError("预期依据缺少文件名")
        if "sha256" in label and (not isinstance(label["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", label["sha256"])):
            raise ValueError("预期依据的 sha256 须为 64 位小写十六进制摘要")
        if "locator" in label and (not isinstance(label["locator"], str) or not label["locator"].strip()):
            raise ValueError("预期依据的位置不能为空")
        if "chunk_id" in label and (type(label["chunk_id"]) is not int or label["chunk_id"] < 1):
            raise ValueError("预期依据的 chunk_id 须为正整数")
        key = tuple(sorted(label.items()))
        if key in seen:
            raise ValueError("预期依据包含重复标注")
        seen.add(key)


def score_evidence(ranking: DocumentRanking, labels: list[dict], top_k: int = 3) -> dict:
    validate_evidence_labels(labels)
    if type(top_k) is not int or not 1 <= top_k <= 180:
        raise ValueError("top_k 须为 1 至 180 的整数")

    evidence = []
    for label in labels:
        def matches(chunk):
            return all(chunk.get("id" if key == "chunk_id" else key) == value for key, value in label.items())

        present = any(matches(chunk) for chunk in ranking.candidates)
        position = next((i for i, chunk in enumerate(ranking.ranked, 1) if matches(chunk)), None)
        if not present:
            stage = "candidate_miss"
        elif position is None or position > top_k:
            stage = "ranking_miss"
        else:
            stage = "hit"
        evidence.append({"expected": label, "in_candidates": present, "rank": position, "stage": stage})
    total = len(labels)
    candidate_hits = sum(e["in_candidates"] for e in evidence)
    top_k_hits = sum(e["stage"] == "hit" for e in evidence)
    return {"top_k": top_k, "expected": total, "candidate_count": len(ranking.candidates),
            "ranked_count": len(ranking.ranked), "candidate_hits": candidate_hits, "top_k_hits": top_k_hits,
            "candidate_recall": candidate_hits / total, "top_k_recall": top_k_hits / total,
            "first_relevant_rank": min((e["rank"] for e in evidence if e["rank"] is not None), default=None),
            "candidate_method": ranking.candidate_method, "ranking_method": ranking.ranking_method,
            "evidence": evidence}


def _passages(text: str, max_chars: int):
    """Yield contiguous windows of original text for independently checkable quotes."""
    if len(text) <= max_chars:
        yield text
        return
    spans = [(m.start(), m.end()) for m in re.finditer(
        r"[^。！？!?\n]+(?:[。！？!?]+|\n+|$)|[。！？!?\n]+", text
    )]
    for index, (start, end) in enumerate(spans):
        if end - start > max_chars:
            step = max(1, max_chars // 2)
            for left in range(start, end, step):
                yield text[left:min(left + max_chars, end)]
                if left + max_chars >= end:
                    break
            continue
        for _, next_end in spans[index + 1:]:
            if next_end - start > max_chars:
                break
            end = next_end
        yield text[start:end]


def grounded_excerpts(question: str, ranked: list[dict], limit: int = 3,
                      max_chars: int = 260) -> list[dict]:
    """Select exact substrings. The caller can cite chunk id and original locator."""
    if limit <= 0 or max_chars < 1:
        return []
    query = _tokens(question)
    excerpts = []
    seen = set()
    for chunk in ranked:
        windows = _passages(str(chunk.get("text", "")), max_chars)
        if len(str(chunk.get("text", ""))) > max_chars:
            # Keep the requested factual value in the quoted window too.
            parts = filter_evidence(question, [{**chunk, "text": w} for w in windows])
            windows = (part["text"] for part in parts)
        excerpt = max(windows, key=lambda value: sum(
            _factor(term) for term in _tokens(value).keys() & query.keys()
        ), default="").strip()
        if excerpt.startswith("#"):
            lines = excerpt.splitlines(keepends=True)
            start = 0
            for line in lines:
                if line.strip() and not re.match(r"^#{1,6}(?:\s|$)", line.strip()):
                    break
                start += len(line)
            excerpt = excerpt[start:].strip()
        identity = (chunk.get("document_id", chunk.get("filename")), chunk.get("locator"), excerpt)
        if excerpt and identity not in seen:
            excerpts.append({**chunk, "excerpt": excerpt})
            seen.add(identity)
        if len(excerpts) >= limit:
            break
    return excerpts


_DOC = re.compile(
    r"制度|办法|规定|口径|公式|如何计算|怎样计算|怎么计算|定义|政策|指南|"
    r"流程|手续|哪些材料|什么材料|报销材料|报销资料|注意事项|操作手册|"
    r"文档|文件中|条款|标准|依据|说明书|返回字段|字段含义|返回格式|\b(?:policy|procedure|formula|definition|"
    r"guide|document|manual|calculate|defined|calculated|requirements)\b"
)
_LIVE = re.compile(
    r"实时|接口|实绩|实际|统计|查询|查下|查一下|查一查|查查|列出|给出|明细|余额|"
    r"剩余|还剩|同比|环比|多少|几笔|几条|记录|执行率|已执行|已支出|"
    r"\b(?:actual|spent|balance|remaining|records?|total|latest|api|list|show|fetch|how much|how many)\b"
)
_CLAUSES = re.compile(
    r"[，,；;。！？!?]|(?:然后|同时|并且|以及|并(?=[给查列说统提返展解结告报显帮对])|"
    r"还(?:想|要)|再(?=查|给|列)|和(?=.{0,16}(?:公式|口径|执行率|余额))|"
    r"\b(?:and|then)\b)"
)
_DATA_ANCHOR = re.compile(r"\b(?:19|20)\d{2}(?!\d)|(?:19|20)\d{2}年|今年|去年|本年|"
                          r"上年|当前|实时|实际|明细|查接口|查一下|最新|\bapi\b")


def document_question(question: str) -> str:
    """Prioritize the policy question, preserving the metric it refers to."""
    clauses = [clause.strip() for clause in _CLAUSES.split(question) if clause.strip()]
    document_parts = [c for c in clauses if _DOC.search(_canonical(c)) or _DOC.search(_normal(c))]
    if not document_parts or len(document_parts) == len(clauses):
        return question
    topics = []
    for clause in clauses:
        if clause in document_parts or not _LIVE.search(_canonical(clause)):
            continue
        source = _canonical(clause)
        for name in _projects_from_text(source, demo=True):
            source = source.replace(name, " ")
        source = re.sub(r"\d{4}\s*年(?:度)?|今年|去年|本年|上年|查询|查一下|查查|请查|"
                        r"给出|实际|统计|多少|的|请|列出", " ", source)
        terms = re.findall(rf"[{_HAN}]{{0,8}}(?:率|金额|余额|数量|库存|收入|成本|"
                           rf"费用|利润|支出|预算)|\b[a-z][a-z-]{{2,}}\b", source)
        topics.extend(t for t in terms if t not in _EN_STOP and t not in {"api", "actual", "latest"})
    return " ".join(dict.fromkeys([*document_parts, *topics]))


def _connector_score(question: str, config: dict, query_terms: Counter[str]) -> float:
    keywords = [str(x) for x in config.get("keywords") or [] if x]
    name = _canonical(config.get("name", ""))
    keys = {_canonical(word) for word in keywords}
    explicit = sum(min(5, len(key)) for key in keys
                   if len(key) >= 2 and key not in _GENERIC_TERMS and key in question)
    if len(name) > 1 and name in question:
        explicit += 6
    primary = _tokens(" ".join([name, *keywords]))
    secondary = _tokens(config.get("description", ""))
    primary_overlap = sum(_factor(t) for t in primary.keys() & query_terms.keys())
    desc_overlap = sum(_factor(t) for t in secondary.keys() & query_terms.keys())
    if explicit == 0 and primary_overlap < 0.99 and desc_overlap < 1.99:
        return 0.0
    return explicit * 2.0 + primary_overlap + min(6.0, desc_overlap) * 0.30


def _named_values(question: str, names: list[str], kind: str) -> list[str]:
    names_re = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True) if n)
    if not names_re:
        return []
    name_pattern = (rf"(?:(?<![A-Za-z0-9_])(?:{names_re})(?![A-Za-z0-9_])"
                    rf"\s*(?:=|:|为|是|等于)\s*|(?<!\w)(?:{names_re})\s+)")
    if kind == "integer":
        value = r"([+-]?\d+)(?![.\d])"
    elif kind == "number":
        value = r"([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)(?![.\deE])"
    elif kind == "date":
        value = r"(\d{4}-\d{2}-\d{2})(?!\d)"
    else:
        value = (r'(?:"([^"]{1,180})"|“([^”]{1,180})”|\'([^\']{1,180})\'|'
                 r'([^\s,，;；。?？!！&]{1,180}))')
    return [next(group for group in match.groups() if group is not None)
            for match in re.finditer(name_pattern + value, question, re.I)]


def _projects_from_text(question: str, demo: bool) -> list[str]:
    known = [name for name in ("材料研发平台", "智能制造项目") if demo and name in question]
    # Preserve an explicitly mentioned unknown project, so a failed lookup
    # returns zero rows instead of silently broadening to all projects.
    text = re.sub(
        r"(?:19|20)\d{2}(?:年(?:度)?)?|今年|上年|去年|本年|查询|请问|查一下|"
        r"查查|帮我|查看|统计|给我|列出|各个|所有|全部|目前|关于|请查|比较|对比|"
        r"请|(?<=^)查|预算执行率|执行率|数据接口|以及|分别|、|"
        r"(?<=项目)[和与]|(?<=平台)[和与]|(?<=课题)[和与]|\s+", " ", question
    )
    matches = re.findall(rf"[A-Za-z0-9{_HAN}]{{1,24}}?(?:项目|平台|课题)", text)
    projects = list(known)
    for project in matches:
        project = project.lstrip("的")
        # A known name can appear after a free-form prefix, e.g. 帮忙看看智能制造项目.
        if any(name in project for name in known):
            continue
        if not re.fullmatch(r"(?:各|该|此|本|每|多|个|科研|预算|执行|研发|这些|全体|每个|各类|所有|全部|本年度|年度)?"
                            r"(?:项目|课题|平台)", project):
            projects.append(project)
    return projects


def _years_from_text(question: str) -> list[str]:
    values = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", question)
    offsets = {"前年": -2, "上上年": -2, "去年": -1, "上年": -1, "今年": 0, "本年": 0, "明年": 1}
    year = datetime.now(timezone(timedelta(hours=8))).year
    values.extend(str(year + offsets[match.group()])
                  for match in re.finditer("|".join(offsets), question))
    return values


def _scope_phrase(question):
    # A possessive qualifier may be a department, region or another dimension. The
    # caller can ask for a value instead of dropping the qualifier and reading all rows.
    before, sep, after = question.partition("的")
    if not sep or not after:
        return ""
    phrase = re.split(r"查询|请问|查一下|查看|统计|列出|对比|比较|换成|那么|帮我查|^那|[，,。；;]", before)[-1]
    phrase = re.sub(r"(?:19|20)\d{2}\s*年(?:度)?|今年|去年|本年|上年", "", phrase).strip()
    return "" if re.search(r"全部|所有|每个|各个|全体|不限", phrase) else phrase


def _parameters(question: str, config: dict) -> tuple[dict, list[str], dict[str, str]]:
    params = {}
    missing = []
    ambiguous = {}
    for spec in config.get("parameters") or []:
        key = spec["name"]
        label = spec.get("label") or key
        kind = spec.get("type", "string")
        aliases = spec.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        names = [key, label, *aliases]
        values = _named_values(question, names, kind)
        if kind == "string":
            choices = spec.get("choices", [])
            choice_hits = [c for c in choices if c in question]
            values.extend(c for c in choice_hits if not any(c != other and c in other for other in choice_hits))
            is_department = key.casefold() in {"department", "dept", "department_name"} or label in {"部门", "科室", "部门名称"}
            prefix = _scope_phrase(question)
            if is_department and re.fullmatch(r"[\w\u3400-\u9fff、 ]{1,64}(?:部|科|处|组|室|部门)", prefix):
                values.extend(v.strip() for v in re.split(r"和|与|或|、", prefix))
                if any(re.search(r"某|这个|那个|我们|本部门|那个组", v) for v in values):
                    ambiguous[key] = label
                    continue
            if choices and any(value not in choices for value in values):
                ambiguous[key] = label
                continue
        is_year = key.lower() in {"year", "fiscal_year", "budget_year"} or label in {"年", "年度", "年份", "预算年度"}
        if is_year:
            values.extend(_years_from_text(question))
        elif kind == "date" and len(
            [p for p in config.get("parameters", []) if p.get("type") == "date"]
        ) == 1:
            values.extend(re.findall(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", question))
        elif not values and (key.lower() in {"project", "project_name"} or label in {"项目", "项目名称"}):
            values = _projects_from_text(question, config.get("url") == "demo://budget")
        distinct = list(dict.fromkeys(values))
        if len(distinct) > 1:
            ambiguous[key] = label
            continue
        value = distinct[0] if distinct else None
        if value is not None:
            try:
                if kind == "integer":
                    value = int(value)
                elif kind == "number":
                    if len(value) > 100:
                        raise ValueError
                elif kind == "date":
                    value = date.fromisoformat(value).isoformat()
                params[key] = value
            except (ValueError, OverflowError):
                missing.append(label)
        elif spec.get("required"):
            missing.append(label)
    qualifier = _scope_phrase(question)
    string_fields = [s for s in config.get("parameters", []) if s.get("type", "string") == "string"]
    if (qualifier and string_fields and not any(s.get("required") for s in string_fields)
            and not any(s["name"] in params or s["name"] in ambiguous for s in string_fields)
            and not any(str(v) in qualifier for v in params.values())
            and not re.search(r"制度|规定|口径|公式|方法|指标|参数|所有|全部|预算执行率", qualifier)
            and ":" not in qualifier and "=" not in qualifier):
        for spec in string_fields:
            label = spec.get("label") or spec["name"]
            ambiguous[spec["name"]] = label
    return params, missing, ambiguous


def route_question(question: str, configs: list[dict], mode: str = "auto") -> dict:
    """Plan retrieval using only enabled, preconfigured connectors."""
    if mode not in {"auto", "documents", "data", "mixed"}:
        raise ValueError("查询模式须为 auto、documents、data 或 mixed")
    text = _canonical(question)
    base = {"intent": "documents", "connector_id": None, "params": {}, "missing": [],
            "reason": "检索文档原文", "ambiguous_params": [], "clarification": ""}
    if mode == "documents":
        return base
    eligible = [config for config in configs if config.get("enabled", True)]
    terms = _tokens(question)
    scores = sorted(((_connector_score(text, config, terms), i) for i, config in enumerate(eligible)),
                    key=lambda pair: -pair[0])
    config = None
    if scores and scores[0][0] > 0:
        if len(scores) == 1 or scores[0][0] > scores[1][0] + 0.1:
            config = eligible[scores[0][1]]
    elif mode in {"data", "mixed"} and len(eligible) == 1:
        config = eligible[0]
    if mode == "auto":
        if not scores or scores[0][0] == 0:
            return base
        document_cue = _DOC.search(_normal(question)) or _DOC.search(text)
        clauses = [clause for clause in _CLAUSES.split(text) if clause.strip()]
        live_clause = any(_LIVE.search(clause) and not _DOC.search(clause) for clause in clauses)
        if document_cue:
            if not (live_clause and len(clauses) > 1):
                return base
            intent = "mixed"
        elif _LIVE.search(text) or _DATA_ANCHOR.search(text):
            intent = "data"
        else:
            return base
    else:
        intent = mode
    if config is None:
        return {**base, "intent": intent, "reason": "没有唯一匹配的数据接口"}
    params, missing, ambiguous = _parameters(unicodedata.normalize("NFKC", question), config)
    clarify = ("此接口每次只接受一个%s，请指定本次要查询的值。" % "、".join(ambiguous.values())
               if ambiguous else "")
    return {"intent": intent, "connector_id": config.get("id"), "params": params,
            "missing": missing, "ambiguous_params": list(ambiguous), "clarification": clarify,
            "reason": f"匹配数据接口「{config.get('name', '')}」"}
