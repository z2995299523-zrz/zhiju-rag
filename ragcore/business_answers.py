"""Resolve requested business operations against the connector's declared metrics."""
import re
from decimal import Decimal, InvalidOperation, localcontext


_BUDGET_METRICS = [
    {"field": "预算金额（万元）", "label": "预算金额", "aliases": ["年度预算", "预算"], "unit": "万元", "additive": True},
    {"field": "已执行金额（万元）", "label": "已执行金额", "aliases": ["已执行", "执行金额"], "unit": "万元", "additive": True},
    {"field": "预算执行率", "label": "预算执行率", "aliases": ["执行率"], "unit": "%",
     "definition": "预算执行率 = 已执行金额（万元）÷ 预算金额（万元）× 100%，结果按整百分比展示。"},
]


def matched_metrics(config, question):
    metrics = config.get("metrics") or (_BUDGET_METRICS if config.get("url") == "demo://budget" else [])
    ranked = []
    for metric in metrics:
        matches = [word for word in [metric["label"], metric["field"], *metric.get("aliases", [])]
                   if word and word.casefold() in question.casefold()]
        if matches:
            ranked.append((max(matches, key=len), metric))
    return [metric for word, metric in ranked if not any(word != other and word in other for other, _ in ranked)]


def _operation(question):
    if re.search(r"(?:多少|几)\s*(?:条|笔)(?:\s*记录)?|记录(?:总数|数)|how many records", question, re.I):
        return "count"
    if re.search(r"排名|排序|前\s*[一二三四五六七八九十\d]+(?:名|项|个)|top\s*\d+", question, re.I):
        return "rank"
    if re.search(r"最高|最大|最多|highest|largest", question, re.I):
        return "maximum"
    if re.search(r"最低|最小|最少|lowest|smallest", question, re.I):
        return "minimum"
    if re.search(r"合计|总计|总额|加总|总和|sum of|grand total", question, re.I):
        return "sum"
    return None


def operation_hint(question):
    operation = _operation(question)
    if operation == "rank":
        match = re.search(r"(?:前\s*[零一二两三四五六七八九十\d]+(?:名|项|个)|top\s*\d+)", question, re.I)
        return (match.group() if match else "排名") + ("升序" if re.search(r"升序|从低到高|从小到大", question) else "")
    return {"sum":"合计", "count":"共有多少条记录", "maximum":"最高", "minimum":"最低"}.get(operation, "")


def _row_label(row, metric):
    fields = [(key, value) for key, value in row.items() if key != metric and isinstance(value, (str, int))]
    labels = [(key, value) for key, value in fields
              if re.search(r"名称|部门|项目|地区|分类|年度|编号|(?i:\b(?:name|id|department|region|year)\b)", key)]
    return "、".join(f"{key}为{value}" for key, value in (labels or fields)[:3])


def _rank_size(question):
    match = re.search(r"(?:前|top)\s*([\d一二两三四五六七八九十]+)", question, re.I)
    if not match:
        return None
    value = match[1].replace("两", "二")
    if value.isascii():
        return int(value[:5])
    digits = "零一二三四五六七八九"
    if "十" in value:
        tens, units = value.split("十", 1)
        if (not tens or tens in digits) and (not units or units in digits):
            return (digits.index(tens) if tens else 1) * 10 + (digits.index(units) if units else 0)
    return digits.index(value) if len(value) == 1 and value in digits else None


def _number(value, unit):
    text = str(value).strip()
    if unit and text.endswith(unit):
        text = text[:-len(unit)].strip()
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text) or len(text) > 100:
        raise ValueError
    number = Decimal(text.replace(",", ""))
    if not number.is_finite():
        raise ValueError
    return number


def operation_answer(source, question, metrics):
    """Return (paragraph, fully answered). Source rows have already been redacted."""
    operation = _operation(question)
    if operation is None:
        return "", source.get("has_more") is not True and not (
            source.get("total_count") is not None and source["total_count"] > len(source["rows"]))
    rows = source["rows"]
    total = source.get("total_count")
    if operation == "count":
        if total is not None:
            return f"按当前条件共有 **{total} 条记录**，本次返回 {len(rows)} 条。", True
        return f"本次返回 {len(rows)} 条记录，接口未提供完整总数。", False
    if not source.get("complete"):
        return "目前只有本次返回的明细，尚不能确定完整范围的合计或排名。", False
    if not rows:
        return "按当前筛选条件查询到 0 条记录，当前范围没有可汇总或比较的数值。", True
    if len(metrics) != 1:
        return "已取得明细；当前接口未提供本题所需的汇总或排名结果。", False
    metric, = metrics
    key, label, unit = metric["field"], metric["label"], metric.get("unit", "")
    if operation == "sum" and (not metric.get("additive") or unit in {"%", "％", "‰"}):
        return f"已取得{label}明细，此指标未声明可加，无法直接合计。", False
    try:
        numbers = [_number(row[key], unit) for row in rows]
    except (KeyError, ValueError, InvalidOperation):
        return f"部分记录缺少可核对的{label}数值，无法给出完整范围的结果。", False
    if operation == "sum":
        with localcontext() as context:
            context.prec = 216
            result = format(sum(numbers), "f")
        return f"{label}合计为 **{result}{unit}**，共计入 {len(rows)} 条记录。", True
    if operation == "rank":
        ascending = bool(re.search(r"升序|从低到高|从小到大|ascending", question, re.I))
        ordered = sorted(zip(numbers, rows), key=lambda pair: pair[0], reverse=not ascending)
        requested = _rank_size(question)
        limit = 20 if requested is None else min(20, requested)
        records = [f"{i}. {_row_label(row, key)}（{label} {value:f}{unit}）"
                   for i, (value, row) in enumerate(ordered[:limit], 1)]
        direction = "从低到高" if ascending else "从高到低"
        return f"{label}{direction}排序，本次展示 {len(records)} 条。\n\n" + "\n".join(records), (
            len(records) >= min(len(rows), requested) if requested is not None else len(records) == len(rows))
    extreme = (max if operation == "maximum" else min)(numbers)
    winners = [row for row, num in zip(rows, numbers) if num == extreme]
    detail = []
    for row in winners[:4]:
        if result := _row_label(row, key):
            detail.append(result)
    prefix = "最高" if operation == "maximum" else "最低"
    suffix = "。对应记录：" + "；".join(detail) if detail else ""
    if len(winners) > 4:
        suffix += f"（共 {len(winners)} 条并列）"
    return f"{label}{prefix}为 **{extreme:f}{unit}**{suffix}。", True
