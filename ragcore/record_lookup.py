"""Verified single-value lookups after a rejected model answer."""
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

from .answer_blocks import _EN_DATE, _MONTH_NAMES
from .storage import CHUNK_FIELDS


_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?(?!\d)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年?(?!\d)")
_START_KEYS = {"date", "start_date", "dtstart", "日期", "开始日期", "发生日期"}
_ID_FIELDS = re.compile(r"名称|站点|厂站|地区|单位|编号|编码|代码|型号|姓名|\b(?:area|city|name|station|code|id|model|region)\b", re.I)
_UNKNOWN = {"", "-", "—", "na", "n/a", "null", "none", "nan"}


def _normal(value):
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _target(question, anchor, source):
    question = _normal(question)
    parts = re.split(r"[?？]", question, maxsplit=1)
    if len(parts) == 2 and parts[1].strip() and not re.fullmatch(r"请(?:保留|沿用)(?:原文|名称)[^。?，,与和]{0,16}(?:文字|写法|格式)。?", parts[1].strip()):
        return None
    q = parts[0].strip()
    scope = re.match(r"^在(.{1,60}?)(?:数据|清单|文档|表格)中[,，]\s*", q)
    if scope:
        if scope[1] not in _normal(source["title"] + " " + source.get("context","")):
            return None
        q = q[scope.end():]
    if anchor:
        token = _normal(anchor)
        if q.count(token) != 1:
            return None
        q = q.replace(token,"",1)
    dates = list(_DATE.finditer(q))
    if len(dates) > 1:
        return None
    period = ""
    if dates:
        m = dates[0]
        try:
            period = date(*map(int,m.groups())).isoformat()
        except ValueError:
            return None
        q = q[:m.start()] + q[m.end():]
    else:
        years = list(_YEAR.finditer(q))
        if len(years) > 1:
            return None
        if years:
            m, = years
            period, q = m[1], q[:m.start()] + q[m.end():]
    compact = re.sub(r"\s+", "", q)
    match = re.fullmatch(r"(?:在|于|的|所对应的|对应的)*(.{1,60}?)(?:是(?:多少|什么)|为多少|有多少)", compact)
    return (match[1], period) if match else None


def _matches(label, target, context, *, event=False):
    compact = re.sub(r"\s+", "", _normal(label))
    if compact == target:
        return True
    if compact == "populationestimate":
        return target in {"估计人口","人口估计数","人口估计值"} or (
            "resident population" in _normal(context) and target in {"常住人口估计数","常住人口估计值"})
    return event and compact == "summary" and target in {"事件名称","活动名称","日程名称","假期名称","名称"}


def _rows(conn, where, args):
    # A limit reached means uniqueness was not established. Never choose the first match.
    items = conn.execute(f"""SELECT {CHUNK_FIELDS} FROM chunks c JOIN documents d ON d.id=c.document_id
                      WHERE d.active=1 AND ({where}) ORDER BY c.id LIMIT 65""", args).fetchall()
    if len(items) >= 65:
        return None
    return [{**dict(row),"metadata":json.loads(row["metadata"])} for row in items]


def _source(row, excerpt, needles, claim):
    ranges = []
    for needle in needles:
        if excerpt.count(needle) != 1:
            return None
        start = excerpt.index(needle)
        ranges.append({"start": start, "end":start+len(needle)})
    return {"kind":"document","title":row["filename"],"document_id":row["document_id"],"chunk_id":row["id"],
            "locator":row["locator"],"text":row["text"],"excerpt":excerpt,"source_key":row["source_key"],
            "context":row["context"],"metadata":row["metadata"],"sha256":row["sha256"],
            "highlights":[{"claim":claim,"ranges":ranges}]}


def _table(conn, question, sources):
    anchors = set()
    for source in sources:
        for field in source["text"].split(" ｜ "):
            key, sep, value = field.partition("：")
            if (sep and _ID_FIELDS.search(key) and 2 <= len(value) <= 160 and not value.isdecimal()
                    and _normal(value) in _normal(question)):
                anchors.add((key,value))
    if len(anchors) != 1:
        return None
    key, value = anchors.pop()
    query = _target(question,value,sources[0])
    if not query:
        return None
    target, period = query
    row_key = key+"："+value
    possible = _rows(conn,"json_type(c.metadata,'$.header_rows')='array' AND instr(lower(c.text),lower(?))>0",(value,))
    rows = [r for r in possible or [] if any(_ID_FIELDS.search(label) and _normal(cell)==_normal(value)
            for label, sep, cell in (part.partition("：") for part in r["text"].split(" ｜ ")) if sep)]
    if len(rows) != 1 or rows[0]["id"] not in {s["chunk_id"] for s in sources}:
        return None
    row, = rows
    # A split row may contain another same-named field outside this column group.
    if " · 第 " in row["locator"] or "文本字符" in row["locator"]:
        return None
    if row["metadata"].get("sheet") and conn.execute("""SELECT count(*) FROM chunks WHERE document_id=?
             AND json_extract(metadata,'$.sheet')=? AND json_extract(metadata,'$.row')=?""",
             (row["document_id"],row["metadata"]["sheet"],row["metadata"]["row"])).fetchone()[0] != 1:
        return None
    hits = []
    for part in row["text"].split(" ｜ "):
        label, sep, raw = part.partition("：")
        if not sep or _normal(raw) in _UNKNOWN or len(raw) > 100 or raw.startswith("="):
            continue
        if re.search(r"（第 \d+ 列）",label):
            label = re.sub(r"（第 \d+ 列）$", "", label)
        label_year = re.search(r"\s*/\s*((?:19|20)\d{2})\s*$",label)
        field_period = label_year[1] if label_year else ""
        base = label[:label_year.start()].strip() if label_year else label
        month = re.search(r"\s*\(as of ([^()]*)\)\s*$",base,re.I)
        if month:
            md = _EN_DATE.fullmatch(month[1])
            if not md or not field_period:
                continue
            try:
                field_period = date(int(field_period),_MONTH_NAMES.index(md[1].casefold())+1,int(md[2])).isoformat()
            except ValueError:
                continue
            base = base[:month.start()].strip()
        if field_period == period and _matches(base,target,row["context"]):
            hits.append((part,raw))
    if len(hits) != 1:
        return None
    field, raw = hits[0]
    claim = f"{value}{' 在 '+period+' ' if period else ''}的{target}为 {raw}。"
    evidence = _source(row,row["text"],[row_key,field],claim)
    return (claim,[evidence]) if evidence else None


def _json(conn, question, sources):
    query = _target(question,"",sources[0])
    if not query or len(query[1]) != 10:
        return None
    target, period = query
    spellings = {period, period.replace("-","")}
    possible = _rows(conn, "json_type(c.metadata,'$.pointer')='text' AND (instr(c.text,?) OR instr(c.text,?))",tuple(spellings))
    matches = []
    for row in possible or []:
        pointer = row["metadata"]["pointer"]
        record = row["metadata"].get("record_pointer")
        if not record or not re.fullmatch(r".*/\d+",record):
            continue
        try:
            obj = json.loads(row["text"])
        except ValueError:
            continue
        path = pointer.removeprefix(record+"/").split("/")
        if (len(path) in {1,2} and path[0].casefold() in _START_KEYS and (len(path)==1 or path[1]=="0")
                and isinstance(obj,(str,int)) and str(obj) in spellings):
            matches.append((row,row["text"],record))
        if pointer == record and isinstance(obj,dict):
            if len([k for k in obj if k.casefold() in _START_KEYS]) > 1:
                return None
            for k,v in obj.items():
                if k.casefold() in _START_KEYS and str(v) in spellings:
                    matches.append((row,json.dumps(k,ensure_ascii=False)+": "+json.dumps(v,ensure_ascii=False),record))
    if len(matches) != 1:
        return None
    anchor, date_quote, record = matches[0]
    if anchor["id"] not in {s["chunk_id"] for s in sources}:
        return None
    parents = _rows(conn,"c.document_id=? AND json_extract(c.metadata,'$.pointer')=?",(anchor["document_id"],record))
    if not parents or len(parents)!=1:
        return None
    parent, = parents
    try:
        obj = json.loads(parent["text"])
    except ValueError:
        return None
    if not isinstance(obj,dict):
        return None
    fields = [(k,v) for k,v in obj.items() if _matches(k,target,parent["context"],event="/vevent/" in record)]
    if len(fields)!=1:
        return None
    key, value = fields[0]
    if not isinstance(value,(str,int)) or isinstance(value,bool) or not 0 < len(str(value)) <= 100 or _normal(value) in _UNKNOWN:
        return None
    value_quote = json.dumps(key,ensure_ascii=False)+": "+json.dumps(value,ensure_ascii=False)
    if value_quote not in parent["text"] or date_quote not in anchor["text"]:
        return None
    claim = f"{period} 对应的{target}为{value}。"
    if anchor["id"] == parent["id"]:
        sources = [_source(parent,parent["text"],[date_quote,value_quote],claim)]
    else:
        sources = [_source(anchor,date_quote,[date_quote],claim),_source(parent,value_quote,[value_quote],claim)]
    return (claim,sources) if all(sources) else None


def direct_record_answer(store, question, sources):
    groups = {}
    for source in sources:
        groups.setdefault(source["document_id"],[]).append(source)
    match = None
    with store.db() as conn:
        for candidates in groups.values():
            suffix = Path(candidates[0]["title"]).suffix.casefold()
            if suffix in {".xlsx",".csv",".tsv"}:
                result = _table(conn,question,candidates)
            elif suffix == ".json":
                result = _json(conn,question,candidates)
            else:
                continue
            if result is not None:
                if match is not None:
                    return None
                match = result
    return match
