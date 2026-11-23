"""Run the selected read-only retrieval plan, then persist its evidence."""
import asyncio
import hashlib
import json
import re
import threading
from contextlib import aclosing
from pathlib import Path

from . import model_config, models, retrieval
from .connectors import ConnectorError, ConnectorRead, execute
from .query_trace import QueryTrace, active_stage, stage_note
from .storage import pack


# Connector configuration and transport metadata belong to administration. Query
# results may carry business filters and rows; archived replies use the same view.
_PRIVATE_FIELD = re.compile(
    r"token|password|passwd|credential|secret|api.?key|access.?key|private.?key|authorization|cookie|"
    r"(?:^|[_-])(?:url|uri|host|port|endpoint|headers?|ip_address)(?:$|[_-])|"
    r"(?:url|uri|hostname|endpoint|ipaddress|sql)$|^sql[_-]?(?:statement|text|query)$|密钥|秘钥|令牌|密码|凭据|内部地址", re.I)
_PRIVATE_VALUE = re.compile(r"(?:\b[a-z][a-z\d+.-]{0,20}://|\bBearer\s+\S+|\b(?:localhost|[\w.-]{1,253}\.internal)(?=[:/\s]|$)|"
                            r"\b(?:127|10)\.\d+\.\d+\.\d+\b|"
                            r"\b192\.168\.\d+\.\d+\b|\b172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+\b)", re.I)


def display_fields(value):
    if isinstance(value, list):
        return [display_fields(item) for item in value]
    if isinstance(value, dict):
        return {key: display_fields(item) for key, item in value.items() if not _PRIVATE_FIELD.search(key)}
    if isinstance(value, str) and _PRIVATE_VALUE.search(value):
        return "已隐藏"
    return value


def visible_filters(params, definitions):
    if not isinstance(params, dict):
        return None
    return [{"label": label, "value": params[key]} for key, label in definitions
            if params.get(key) not in (None, "") and not _PRIVATE_FIELD.search(key)
            and not _PRIVATE_FIELD.search(label) and not _PRIVATE_VALUE.search(label)
            and not _PRIVATE_VALUE.search(str(params[key]))]


def workspace_answer(result, *, show_sql=False):
    """Expose business evidence, keeping the internal request and raw snapshot in the archive."""
    answer = result.get("answer", "")
    citations = []
    params = result.get("params") if isinstance(result.get("params"), dict) else None
    for source in result.get("citations") or []:
        if not isinstance(source, dict):
            continue
        if source.get("kind") == "document":
            citations.append(source)
            continue
        if source.get("kind") != "api":
            citations.append({k: source[k] for k in ("id", "title", "kind") if k in source})
            continue
        # Names and values were not stored separately in early snapshots. Use only
        # their saved business parameters, never re-fetch a connector or parse its URL.
        visible = {k: source[k] for k in ("id", "kind", "connector_id", "title", "fetched_at", "demo",
                                          "total_count", "sha256", "rows_sha256", "metric_definition") if k in source}
        visible["title"] = display_fields(visible.get("title"))
        if show_sql and isinstance(source.get("executed_sql"), str):
            visible["executed_sql"] = source["executed_sql"]
        if source.get("demo"):
            visible["request"] = "GET demo://budget"
        visible["filters"] = (source["filters"] if isinstance(source.get("filters"), list) else
                              visible_filters(params, [(key, key) for key in (params or {})]))
        visible["locator"] = f"{source['total_count']} 条记录" if "total_count" in source else ""
        if isinstance(source.get("rows"), list):
            visible["rows"] = display_fields(source["rows"])
            if visible["rows"] != source["rows"]:
                visible["redacted"] = True
                # Only rewrite the saved API paragraph. If an older answer format
                # cannot be matched safely, keep its redacted rows available.
                before = (describe_rows(source, result["question"]) if answer and source.get("id") and
                          all(isinstance(row, dict) for row in source["rows"]) else None)
                answer = (answer.replace(before, describe_rows(visible, result["question"]))
                          if before and before in answer else "此回答包含受限字段，请在来源中查看可展示的记录。")
        citations.append(visible)
    return {**result, "citations": citations, "answer": answer, "params": display_fields(params) if params is not None else {}}


def document_sources(store, question):
    ranked = retrieval.rank_document_chunks(store, question).ranked
    sources = []
    for item in retrieval.grounded_excerpts(question, ranked, limit=3, max_chars=520):
        sources.append({"kind": "document", "title": item["filename"], "document_id": item["document_id"],
                        "chunk_id": item["id"], "locator": item["locator"], "text": item["text"],
                        "excerpt": item["excerpt"], "sha256": item["sha256"]})
    return sources


def data_source(config, params, read=None):
    result = execute(config, params, include_raw=True, read=read)
    rows = result["rows"]
    digest = hashlib.sha256(pack(rows).encode()).hexdigest()
    source = {"kind": "api", "title": config["name"], "connector_id": config["id"],
            "locator": f"{config['records_path'] or '$'} · {len(rows)} 条记录",
            "text": json.dumps(rows[:4], ensure_ascii=False, indent=2)[:5000],
            "rows": rows, "request": result["request"],
            "filters": visible_filters(result["applied_params"], [(p["name"], p["label"]) for p in config["parameters"]]),
            "fetched_at": result["fetched_at"],
            "sha256": result["response_sha256"], "rows_sha256": digest, "records_path": config["records_path"],
            "total_count": len(rows), "demo": config["url"] == "demo://budget", "_raw": result["raw_body"]}
    if source["demo"] and rows and "预算执行率" in rows[0]:
        source["metric_definition"] = "预算执行率 = 已执行金额（万元）÷ 预算金额（万元）× 100%，结果按整百分比展示。"
    if result.get("executed_sql"):
        source["executed_sql"] = result["executed_sql"]
    return source


def row_lines(source, question):
    marker = f"[{source['id']}]"
    rows = source["rows"]
    if not rows:
        yield f"接口按本次条件返回 0 条记录。{marker}"
        return
    if (source.get("demo") and len(rows) == 1 and "执行率" in question and
            {"项目名称", "年度", "预算执行率", "已执行金额（万元）", "预算金额（万元）"} <= rows[0].keys()):
        row = rows[0]
        yield (f"{row['项目名称']} {row['年度']} 年的预算执行率为 {row['预算执行率']}，"
               f"已执行 {row['已执行金额（万元）']} 万元，年度预算 {row['预算金额（万元）']} 万元。{marker}")
        return
    for row in rows[:4]:
        pairs = []
        for key, value in list(row.items())[:10]:
            display = value if isinstance(value, str) else pack(value)
            pairs.append(f"{key} = {display[:120]}")
        yield "，".join(pairs) + " " + marker
    if len(rows) > 4 or any(len(row) > 10 for row in rows[:4]):
        yield f"接口共返回 {len(rows)} 条记录，完整字段见证据快照。{marker}"


def describe_rows(source, question):
    return "\n".join(row_lines(source, question))


def query_plan(store, question, mode, selected_id):
    configs = store.connectors()
    if selected_id is not None:
        configs = [c for c in configs if c["id"] == selected_id]
        if not configs:
            raise KeyError("数据接口不存在")
        if mode == "auto":
            mode = "data"
    plan = retrieval.route_question(question, configs, mode)
    if selected_id is None and mode == "auto" and not plan.get("ambiguous_params") and models.model_enabled():
        plan = models.refine_route(question, configs, plan, mode)
    config = next((c for c in configs if c["id"] == plan["connector_id"]), None)
    return plan, config, bool(configs)


async def query_events(store, owner, question, mode, overrides, selected_id, *,
                       stream_model=True, show_sql=False, parent_id=None, return_archive=False, request_id=None):
    trace = QueryTrace(mode, request_id)
    token = active_stage.set(None)
    try:
        trace.start("parse")
        with model_config.workspace(store):
            events = _generate(store, owner, question, mode, overrides, selected_id, stream_model, show_sql,
                               parent_id, return_archive, request_id, trace)
            async with aclosing(events):
                async for name, data in events:
                    if name == "done":
                        result = data["result"]
                        await trace.finish(store, result["status"], result["id"])
                    yield name, data
    except (GeneratorExit, asyncio.CancelledError):
        if not trace.finished:
            trace.mark("stopped")
            await trace.finish(store, "stopped")
        raise
    except Exception:
        trace.mark("failed")
        await trace.finish(store, "error")
        raise
    finally:
        active_stage.reset(token)


async def _generate(store, owner, question, mode, overrides, selected_id, stream_model, show_sql,
                    parent_id, return_archive, request_id, trace):
    yield "status", {"message": "正在判断需要查阅的资料和数据…"}
    plan, config, has_configs = await asyncio.to_thread(query_plan, store, question, mode, selected_id)
    params = {**plan["params"], **overrides}
    intent = plan["intent"]
    citations, notes, missing = [], [], []
    data_needed = intent in ("data", "mixed")
    docs_needed = intent in ("documents", "mixed")
    needs_input = False
    failed = False
    if data_needed:
        trace.start("parameters")
        yield "status", {"message": "正在核对查询条件…"}
        if config is None:
            trace.mark("no_connector")
            needs_input = has_configs
            notes.append("请选择要查询的数据接口。" if has_configs else "当前工作区尚未配置数据接口。")
        else:
            specs = config["parameters"]
            missing = [s["label"] for s in specs if s["required"] and params.get(s["name"]) in (None, "")]
            for label in plan.get("missing", []):
                key = next((s["name"] for s in specs if s["label"] == label), None)
                if key and key not in overrides and label not in missing:
                    missing.append(label)
            ambiguous = [key for key in plan.get("ambiguous_params", []) if overrides.get(key) in (None, "")]
            if ambiguous:
                missing.extend(s["label"] for s in specs if s["name"] in ambiguous and s["label"] not in missing)
            if missing:
                trace.mark("needs_input")
                needs_input = True
                notes.append(plan.get("clarification", "") if ambiguous else "请补充查询条件：" + "、".join(missing) + "。")
            else:
                trace.start("data")
                yield "status", {"message": "正在查询业务数据…"}
                read = ConnectorRead()
                try:
                    citations.append(await asyncio.to_thread(data_source, config, params, read))
                    if not citations[-1]["rows"]:
                        trace.mark("empty")
                except asyncio.CancelledError:
                    read.cancel()
                    raise
                except ConnectorError as exc:
                    trace.mark("failed")
                    failed = True
                    notes.append(f"数据接口未完成查询。{exc}")
    if docs_needed:
        trace.start("retrieval")
        yield "status", {"message": "正在检索文档并定位相关原文…"}
        subject = retrieval.document_question(question) if intent == "mixed" else question
        docs = await asyncio.to_thread(document_sources, store, subject)
        citations.extend(docs)
        if not docs:
            trace.mark("empty")
            notes.append("已导入的文件中未检索到足以回答这个问题的原文。")
    trace.start("generation")
    snapshots = []
    for index, source in enumerate(citations, start=1):
        source["id"] = f"S{index}"
        if source["kind"] == "api":
            snapshots.append((source["id"], source["sha256"], source.pop("_raw")))
    document_cites = [s for s in citations if s["kind"] == "document"]
    if any("· OCR 识别" in source["locator"] for source in document_cites):
        notes.append("标注 OCR 的片段由图片识别得到，金额和编号请对照引用的原页核验。")
    useful = bool(document_cites) or any(s.get("rows") for s in citations)
    if needs_input:
        status = "needs_input"
    elif failed:
        status = "partial" if useful else "error"
    elif not useful:
        status = "no_evidence"
    elif intent == "mixed" and not (document_cites and any(s.get("rows") for s in citations)):
        status = "partial"
    else:
        status = "answered"
    result = {"question": question, "intent": intent, "status": status, "answer": "",
              "citations": citations, "params": params, "connector_id": plan["connector_id"], "missing": missing,
              "reason": plan.get("reason", "")}
    if parent_id is not None:
        result["parent_id"] = parent_id
    visible_sources = workspace_answer(result, show_sql=show_sql)["citations"]
    yield "evidence", {"citations": visible_sources}
    yield "status", {"message": "正在整理回答…"}
    narrative = []
    for source, visible in zip(citations, visible_sources):
        if source["kind"] == "api":
            prefix = "\n\n" if narrative else ""
            lines = []
            for line in row_lines(visible, question):
                lines.append(line)
                yield "delta", {"text": prefix + line}
                prefix = "\n"
            narrative.append(describe_rows(source, question) if visible.get("redacted") else "\n".join(lines))
    generated = []
    if document_cites and models.model_enabled():
        prefix = "\n\n" if narrative else ""
        if stream_model:
            async with aclosing(models.stream_answer(question, document_cites)) as upstream:
                async for part in upstream:
                    if part:
                        yield "delta", {"text": prefix + part}
                        generated.append(part)
                        prefix = ""
        else:
            selected = await asyncio.to_thread(models.synthesize, question, document_cites)
            if selected:
                generated.append(selected)
                yield "delta", {"text": prefix + selected}
        if not generated:
            stage_note("extractive_fallback")
    if generated:
        narrative.append("".join(generated))
    else:
        for source in document_cites:
            passage = f"{source['excerpt']} [{source['id']}]"
            yield "delta", {"text": ("\n\n" if narrative else "") + passage}
            narrative.append(passage)
    for note in notes:
        yield "delta", {"text": ("\n\n" if narrative else "") + note}
        narrative.append(note)
    result["answer"] = "\n\n".join(narrative)
    trace.start("archive")
    # A disconnect can occur while SQLite is waiting for its writer lock.
    cancelled = threading.Event()
    try:
        saved = await asyncio.to_thread(store.record, owner, result, snapshots, cancelled=cancelled, request_id=request_id)
    finally:
        cancelled.set()
    yield "done", {"result": saved if return_archive else workspace_answer(saved, show_sql=show_sql)}


def answer_query(store, owner, question, mode, overrides, selected_id):
    """Keep the synchronous query and offline evaluation entry points."""
    async def collect():
        events = query_events(store, owner, question, mode, overrides, selected_id, stream_model=False, return_archive=True)
        async with aclosing(events):
            async for event, payload in events:
                if event == "done":
                    return payload["result"]
    return asyncio.run(collect())


def query_examples(store):
    from .seed import connector_signature, finance_suggestions
    configs = [c for c in store.connectors() if display_fields(c["name"]) == c["name"]]
    with store.db() as conn:
        doc = conn.execute("""SELECT d.filename,d.sha256, (SELECT substr(text,1,180) FROM chunks
                              WHERE document_id=d.id ORDER BY id LIMIT 1) AS opening
                              FROM documents d WHERE active=1 AND chunk_count>0 ORDER BY id DESC LIMIT 1""").fetchone()
        row = conn.execute("SELECT value FROM settings WHERE name='industry_examples'").fetchone()
        curated = json.loads(row["value"]) if row else {}
        if not row and any(c["url"] == "demo://budget" for c in configs):
            curated = finance_suggestions()  # Legacy workspaces predate saved templates.
        template_docs = curated.get("documents", {})
        original = set()
        if doc and doc["sha256"] == template_docs.get(doc["filename"]):
            for filename in {ex["document"] for ex in curated["examples"] if ex.get("document")}:
                if conn.execute("""SELECT 1 FROM documents WHERE filename=? AND sha256=?
                                   AND active=1 AND chunk_count>0""", (filename, template_docs.get(filename))).fetchone():
                    original.add(filename)
    questions = {}
    if doc:
        name = Path(doc["filename"]).stem
        opening = (doc["opening"] or "").strip().lstrip("#").strip()
        topic = re.split(r"[。！？.!?\n]", opening, maxsplit=1)[0][:36].strip()
        questions["documents"] = (f"《{name}》中关于“{topic}”有哪些说明？" if topic and topic != name
                                  else f"《{name}》说明了哪些内容？")
    if configs:
        query = f"查询「{configs[-1]['name']}」的数据记录"
        questions["data"] = query + "？"
        if doc:
            questions["mixed"] = f"{query}，并说明《{name}》中的相关要求。"
    # New documents and connectors should take precedence over demo suggestions.
    template_connectors = curated.get("connectors", {})
    matching = {c["name"] for c in configs if connector_signature(c) == template_connectors.get(c["name"])}
    if configs and configs[-1]["name"] not in matching:
        matching.clear()
    for example in curated.get("examples", []):
        if example.get("document") and example["document"] not in original:
            continue
        if example.get("connector") and example["connector"] not in matching:
            continue
        questions[example["mode"]] = example["question"]
    labels = {"documents": "查文档", "data": "查数据", "mixed": "综合查询"}
    return {"examples": [{"mode": mode, "label": label, "question": questions[mode]}
                         for mode, label in labels.items() if mode in questions]}
