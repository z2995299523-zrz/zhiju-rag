"""Run labeled queries against a fresh, offline industry workspace."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from .engine import answer_query
from .storage import Store


DEFAULT_CASES = Path(__file__).resolve().parent.parent / "eval" / "seed_cases.jsonl"


def evidence_errors(store, result):
    errors = []
    if result["status"] == "answered" and not result["citations"]:
        errors.append("answered without evidence")
    with store.db() as conn:
        for source in result["citations"]:
            sid = source["id"]
            if f"[{sid}]" not in result["answer"]:
                errors.append(f"missing citation marker [{sid}]")
            if source["kind"] == "document":
                row = conn.execute(
                    """SELECT d.content, d.filename, c.text, c.locator FROM chunks c
                       JOIN documents d ON c.document_id=d.id WHERE c.id=? AND d.id=?""",
                    (source["chunk_id"], source["document_id"]),
                ).fetchone()
                if (row is None or row["filename"] != source["title"] or row["locator"] != source["locator"]
                        or not source["excerpt"] or source["excerpt"] not in row["text"]
                        or hashlib.sha256(row["content"]).hexdigest() != source["sha256"]):
                    errors.append(f"document evidence mismatch [{sid}]")
            elif source["kind"] == "api":
                row = conn.execute("SELECT body FROM api_snapshots WHERE history_id=? AND source_id=?",
                                   (result["id"], sid)).fetchone()
                if (row is None or hashlib.sha256(row["body"]).hexdigest() != source["sha256"]
                        or json.loads(row["body"]) != source["rows"]):
                    errors.append(f"API snapshot mismatch [{sid}]")
            else:
                errors.append(f"unknown source kind [{sid}]")
    return errors


def check_case(store, case, names):
    expect = case["expect"]
    if "intent" not in expect or "status" not in expect:
        raise ValueError("expect needs intent and status")
    response = answer_query(store, "offline-evaluation", case["question"], case.get("mode", "auto"),
                            case.get("params", {}), None)
    sources = response["citations"]
    actual = {
        "intent": response["intent"], "status": response["status"], "params": response["params"],
        "connector": names.get(response["connector_id"]), "missing": response["missing"],
        "kinds": sorted({source["kind"] for source in sources}),
        "files": sorted({source["title"] for source in sources if source["kind"] == "document"}),
    }
    errors = evidence_errors(store, response)
    for key, wanted in expect.items():
        if key == "contains":
            errors.extend(f"answer missing {value!r}" for value in wanted if value not in response["answer"])
        elif key not in actual:
            raise ValueError(f"unknown expected field {key!r}")
        elif actual[key] != (sorted(wanted) if key in {"kinds", "files"} else wanted):
            errors.append(f"{key} expected {wanted!r}, got {actual[key]!r}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="UTF-8 JSONL case file")
    parser.add_argument("--profile", default="research-finance", help="Industry profile name or JSON file")
    args = parser.parse_args()
    lines = args.cases.read_text(encoding="utf-8-sig").splitlines()
    passed = failed = 0
    with TemporaryDirectory() as directory, patch.dict(os.environ, {
        "RAG_LLM_BASE_URL": "", "RAG_EMBED_BASE_URL": "", "RAG_INDUSTRY_PROFILE": args.profile,
    }):
        store = Store(directory)
        store.initialize()
        configs = store.connectors()
        if any(config["url"] != "demo://budget" for config in configs):
            parser.error("离线评估请使用演示接口或仅文档模板；真实接口应在对应环境单独验收")
        names = {config["id"]: config["name"] for config in configs}
        for line, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            identifier = f"line {line}"
            try:
                case = json.loads(raw)
                identifier = case["id"]
                errors = check_case(store, case, names)
            except Exception as exc:
                errors = [f"{type(exc).__name__} {exc}"]
            if errors:
                failed += 1
                print(f"FAIL {identifier}  " + " | ".join(errors))
            else:
                passed += 1
                print(f"PASS {identifier}")
    print(f"Passed {passed} / {passed + failed}   Failed {failed}")
    if passed + failed == 0:
        print("No evaluation cases supplied.", file=sys.stderr)
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
