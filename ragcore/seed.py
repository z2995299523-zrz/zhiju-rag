"""Bootstrap an industry workspace once, in the same transaction as its documents."""
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from .connectors import validate_config
from .documents import parse_document_report
from .storage import pack


ROOT = Path(__file__).resolve().parent.parent
MODES = {"documents", "data", "mixed"}


def connector_signature(config):
    fields = {k: v for k, v in config.items() if k != "id"}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def finance_suggestions():
    profile = json.loads((ROOT / "profiles/research-finance.json").read_text(encoding="utf-8"))
    return {"examples": profile["examples"], "documents": {
        name: hashlib.sha256(body.encode()).hexdigest() for name, body in profile["documents"].items()},
        "connectors": {cfg["name"]: connector_signature(validate_config(cfg)) for cfg in profile["connectors"]}}


def initial_profile(seed=None):
    if seed is not None:
        return "research-finance" if seed else "standard"
    selected = os.getenv("RAG_INDUSTRY_PROFILE", "").strip()
    return selected or ("research-finance" if os.getenv("RAG_SEED_DEMO") == "1" else "standard")


def seed_workspace(store, conn, selected):
    if selected == "standard":
        return
    path = ROOT / ("profiles/" + selected + ".json" if selected in {"research-finance", "manufacturing"} else selected)
    with path.open("rb") as file:
        raw = file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("行业模板文件上限为 2 MiB")
    profile = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(profile, dict) or set(profile) - {"name", "documents", "connectors", "examples", "document_terms"}:
        raise ValueError("行业模板格式错误，请参照 profiles 下的示例")
    name = profile.get("name")
    docs, connectors, examples = (profile.get(k, default) for k, default in
                                   (("documents", {}), ("connectors", []), ("examples", [])))
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("行业模板须有名称")
    if not isinstance(docs, dict) or not isinstance(connectors, list) or not isinstance(examples, list):
        raise ValueError("行业模板的 documents 须为对象，connectors 与 examples 须为列表")
    terms = profile.get("document_terms", {})
    if not isinstance(terms, dict) or len(terms) > 128:
        raise ValueError("行业文档词表须为对象，最多定义 128 个查询词")
    for term, words in terms.items():
        if (not 2 <= len(term.strip()) <= 64 or not isinstance(words, list) or not 1 <= len(words) <= 8
                or any(not isinstance(word, str) or not 1 <= len(word.strip()) <= 64 for word in words)):
            raise ValueError("行业查询词需为 2 至 64 个字符，每个词应对应 1 至 8 个文档用词")
    hashes = {}
    for filename, content in docs.items():
        if (not isinstance(content, str) or not content.strip() or "/" in filename or "\\" in filename
                or Path(filename).suffix.lower() not in {".md", ".txt", ".csv", ".tsv", ".json", ".jsonl"}):
            raise ValueError("行业模板文档须使用文本格式的文件名和正文")
        data = content.encode()
        report = parse_document_report(filename, data)
        chunks = report.pop("chunks")
        hashes[filename] = store.insert_document(conn, filename, data, chunks, report=report)["sha256"]
    connector_hashes = {}
    for entry in connectors:
        cfg = validate_config(entry)
        conn.execute("INSERT INTO connectors(name,config) VALUES (?,?)", (cfg["name"], pack(cfg)))
        connector_hashes[cfg["name"]] = connector_signature(cfg)
    used_modes = set()
    for item in examples:
        if not isinstance(item, dict) or set(item) - {"mode", "question", "document", "connector"}:
            raise ValueError("行业示例格式错误")
        mode = item.get("mode")
        if not isinstance(mode, str) or mode not in MODES or mode in used_modes:
            raise ValueError("行业示例的查询模式须有效且唯一")
        if not isinstance(item.get("question"), str) or not 1 <= len(item["question"].strip()) <= 2000:
            raise ValueError("行业示例须填写有效问题")
        if ("document" in item and mode == "data") or ("connector" in item and mode == "documents"):
            raise ValueError("行业示例须按所选模式绑定对应来源")
        if mode in ("documents", "mixed") and (not isinstance(item.get("document"), str) or item["document"] not in docs):
            raise ValueError("行业示例引用的文档不在模板中")
        if mode in ("data", "mixed") and (not isinstance(item.get("connector"), str) or item["connector"] not in connector_hashes):
            raise ValueError("行业示例引用的接口不在模板中")
        used_modes.add(mode)
    conn.executemany("INSERT INTO settings(name,value) VALUES (?,?)", [
        ("industry_name", name), ("document_terms", pack(terms)), ("industry_examples", pack({
            "examples": examples, "documents": hashes, "connectors": connector_hashes}))])
