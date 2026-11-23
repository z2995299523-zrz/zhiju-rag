"""Deterministic wording for connector results and model-free document answers."""
import json
import re
from pathlib import Path

from .retrieval import grounded_excerpts


def _fields(row, question, dimensions=(), *, named_only=False):
    """Keep the requested values together with their identifying columns."""
    values = [(str(k), v) for k, v in row.items()
              if str(k).strip() and v is not None and not isinstance(v, (dict, list))
              and str(v).strip() and len(str(v)) <= 180]
    requested = [(key, value) for key, value in values
                 if (label := re.split(r"[（(]", key)[0].strip()) and label.casefold() in question.casefold()]
    if named_only:
        return requested
    selected = dict(requested or values[:3])
    return [(k, v) for k, v in values if k in selected or k in dimensions]


def _dimensions(rows):
    label = re.compile(r"(?:名称|编号|项目|仓库|型号|日期|年度|年份|地区|城市|部门|类别|季度|月份)$"
                       r"|(?:^|_)(?:id|name|code|year|date|region|department|category|month|quarter)$", re.I)
    keys = dict.fromkeys(key for row in rows for key in row)
    return [key for key in keys if label.search(key) and len({str(row.get(key)) for row in rows}) > 1][:2]


def _description(fields):
    parts = []
    for key, value in fields:
        text = str(value)
        if isinstance(value, bool):
            text = "是" if value else "否"
        unit = re.fullmatch(r"(.+?)[（(](万元|亿元|元|件|台|人|吨|小时|分钟|%)[）)]", key)
        if unit and re.fullmatch(r"[+-]?[\d,]+(?:\.\d+)?", text):
            key = unit[1]
            text += (" " if unit[2] != "%" else "") + unit[2]
        gap = " " if re.match(r"[A-Za-z0-9+\-]", text) else ""
        parts.append(f"{key}为{gap}{text}")
    return "，".join(parts)


def _named_description(fields, question):
    description = _description(_fields(fields, question, named_only=True))
    return description + "。" if description else ""


def data_paragraphs(source, question):
    rows = source["rows"]
    cite = f" [{source['id']}]"
    total = source.get("total_count")
    if source.get("has_more") is True or (total is not None and total > len(rows)):
        scope = f"，上游报告总计 {total} 条" if total is not None else "，后续仍有记录"
        yield f"本页返回 {len(rows)} 条记录{scope}。" + cite
        if not rows:
            return
    if not rows:
        yield ("本次接口返回 0 条记录。" if source.get("complete") is False else
               "按当前筛选条件查询到 0 条记录。") + cite
        return
    row = rows[0]
    if (source.get("demo") and len(rows) == 1 and "执行率" in question and
            {"项目名称", "年度", "预算执行率", "已执行金额（万元）", "预算金额（万元）"} <= row.keys()):
        yield f"{row['项目名称']} {row['年度']} 年的预算执行率为 **{row['预算执行率']}**。" + cite
        yield f"已执行 {row['已执行金额（万元）']} 万元，年度预算为 {row['预算金额（万元）']} 万元。" + cite
        return
    if len(rows) == 1:
        fields = _fields(row, question)
        if fields:
            yield _description(fields) + "。" + cite
        else:
            yield "查询到 1 条记录，字段明细见本次查询结果。" + cite
        return
    yield f"本次查询返回 **{len(rows)} 条记录**。" + cite
    points = []
    dimensions = _dimensions(rows[:4])
    for i, row in enumerate(rows[:4], 1):
        fields = _fields(row, question, dimensions)
        if fields:
            points.append(f"{i}. {_description(fields)}。{cite}")
    if points:
        yield "\n".join(points)
    if len(rows) > 4:
        yield "其余记录可在本次查询的依据中查看。" + cite


def _document_text(question, source):
    excerpt = source["excerpt"].strip()
    if "（表头）" in str(source.get("locator", "")) and not re.search(r"表头|字段名|列名|column names|headers", question, re.I):
        return ""
    asks_value = re.search(r"多少|多高|多远|多长|几(?:元|天|年|人|次|个|件|公里)|how (?:much|many)", question, re.I)
    if asks_value and not re.search(r"\d|[零一二三四五六七八九十两百千万]+\s*(?:元|天|年|人|次|个|件|公里|米)|不限|无上限|不设上限", excerpt):
        return ""
    field_checks = (
        (r"负责人|由谁|谁负责|责任人|who (?:is responsible|owns)", r"负责人|负责[人部]|由.{1,24}(?:负责|审批|办理)|责任人|owner|responsib"),
        (r"截止日期|截止时间|哪天截止|何时截止|deadline", r"截止|最迟|到期|deadline|due date"),
        (r"联系电话|电话号码|联系方式中的电话|phone number", r"\d[\d ()+-]{5,}\d"),
        (r"电子邮箱|邮件地址|email address", r"[\w.+-]+@[\w.-]+\.\w+"),
    )
    for requested, supported in field_checks:
        if re.search(requested, question, re.I) and not re.search(supported, excerpt, re.I):
            return ""
    if excerpt.startswith("{"):
        try:
            fields = json.loads(excerpt)
        except ValueError:
            fields = None
        if isinstance(fields, dict):
            # A timestamp or record ID is not necessarily the value a question asks for.
            return _named_description(fields, question)
    if " ｜ " in excerpt:
        entries = [p.partition("：") for p in excerpt.split(" ｜ ")]
        if all(sep for _, sep, _ in entries):
            fields = {k.strip(): v.strip() for k, _, v in entries if not re.fullmatch(r"第\s*\d+\s*列", k.strip())}
            return _named_description(fields, question)
    if len(excerpt) > 230:
        shorter = grounded_excerpts(question, [{**source, "filename": source["title"], "text": excerpt}],
                                    limit=1, max_chars=230)
        if shorter:
            excerpt = shorter[0]["excerpt"]
    return excerpt


def document_paragraphs(question, sources):
    paragraphs = []
    different_documents = len({(s.get("source_key"), s["title"]) for s in sources}) > 1
    for source in sources:
        text = _document_text(question, source)
        if not text:
            continue
        if different_documents or source.get("source_key"):
            title = Path(source["title"]).stem
            if source.get("source_key"):
                title = f"{source['source_key']} · {title}"
            text = f"《{title}》记载：{text}"
        paragraphs.append(f"{text} [{source['id']}]")
    return paragraphs
