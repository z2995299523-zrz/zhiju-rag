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
from .retrieval import observe_rankings, score_evidence, validate_evidence_labels
from .storage import Store


DEFAULT_CASES = Path(__file__).resolve().parent.parent / "eval" / "seed_cases.jsonl"


def evidence_errors(store, result):
    from .connectors import ConnectorError, snapshot_rows
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
                path = source.get("records_path")
                if path is None:
                    archived = conn.execute("SELECT result FROM history WHERE id=?", (result["id"],)).fetchone()
                    stored = json.loads(archived["result"]) if archived else {}
                    path = next((s.get("records_path", "") for s in stored.get("citations", []) if s["id"] == sid), "")
                try:
                    same_rows = row is not None and snapshot_rows(row["body"], path) == source["rows"]
                except ConnectorError:
                    same_rows = False
                if not same_rows or hashlib.sha256(row["body"]).hexdigest() != source["sha256"]:
                    errors.append(f"API snapshot mismatch [{sid}]")
            else:
                errors.append(f"unknown source kind [{sid}]")
    return errors


def assess_retrieval(store, case, rankings, top_k):
    expect = case["expect"]
    if "evidence" not in case:
        reason = ("data_only" if expect["intent"] == "data" else
                  "no_positive_evidence" if expect["status"] == "no_evidence" else "unlabeled")
        return {"status": "skipped", "reason": reason}, []
    if expect["intent"] not in {"documents", "mixed"}:
        raise ValueError("预期文档依据只适用于文档或综合查询")
    labels = case["evidence"]
    validate_evidence_labels(labels)
    # Typos, obsolete document versions or stale locators are annotation errors.
    # These must not lower first-stage recall for an otherwise valid corpus.
    columns = {"filename": "d.filename", "sha256": "d.sha256", "locator": "c.locator", "chunk_id": "c.id"}
    with store.db() as conn:
        for label in labels:
            where = " AND ".join(columns[key] + "=?" for key in label)
            found = conn.execute(f"""SELECT 1 FROM documents d JOIN chunks c ON c.document_id=d.id
                                     WHERE d.active=1 AND {where} LIMIT 1""", tuple(label.values())).fetchone()
            if not found:
                return {"status": "invalid_label", "expected": label}, [f"预期依据在当前工作区找不到 {label!r}"]
    if len(rankings) != 1:
        return {"status": "not_measured"}, [f"已标注问题实际执行了 {len(rankings)} 次文档检索"]
    metrics = score_evidence(rankings[0], labels, top_k)
    errors = [f"{entry['stage']} {entry['expected']!r}" for entry in metrics["evidence"] if entry["stage"] != "hit"]
    return {"status": "measured", **metrics}, errors


def evaluate_case(store, case, names, top_k):
    expect = case["expect"]
    if "intent" not in expect or "status" not in expect:
        raise ValueError("expect needs intent and status")
    with observe_rankings() as rankings:
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
    metrics, retrieval_errors = assess_retrieval(store, case, rankings, top_k)
    return errors + retrieval_errors, metrics


def check_case(store, case, names):
    errors, _ = evaluate_case(store, case, names, top_k=3)
    return errors


def summarize_retrieval(cases, top_k):
    measured = [case["retrieval"] for case in cases if case["retrieval"]["status"] == "measured"]
    total = sum(item["expected"] for item in measured)
    candidate_hits = sum(item["candidate_hits"] for item in measured)
    top_k_hits = sum(item["top_k_hits"] for item in measured)
    return {"measured_cases": len(measured), "other_cases": len(cases) - len(measured), "expected": total,
            "candidate_hits": candidate_hits, "top_k_hits": top_k_hits, "top_k": top_k,
            "candidate_recall": candidate_hits / total if total else None,
            "top_k_recall": top_k_hits / total if total else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="UTF-8 JSONL case file")
    parser.add_argument("--profile", default="research-finance", help="Industry profile name or JSON file")
    parser.add_argument("--top-k", type=int, default=3, help="Evaluate this many positions after ranking (default 3)")
    parser.add_argument("--report", type=Path, help="Write a UTF-8 JSON report with per-case metrics and evidence ranks")
    args = parser.parse_args()
    if not 1 <= args.top_k <= 180:
        parser.error("--top-k must be between 1 and 180")
    lines = args.cases.read_text(encoding="utf-8-sig").splitlines()
    cases = []
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
                errors, metrics = evaluate_case(store, case, names, args.top_k)
            except Exception as exc:
                errors = [f"{type(exc).__name__} {exc}"]
                metrics = {"status": "not_measured"}
            cases.append({"id": identifier, "passed": not errors, "errors": errors, "retrieval": metrics})
            if errors:
                print(f"FAIL {identifier}  " + " | ".join(errors))
            else:
                print(f"PASS {identifier}")
            if metrics["status"] == "measured":
                print(f"  候选召回 {metrics['candidate_hits']}/{metrics['expected']} ({metrics['candidate_method']})"
                      f"  重排 Top-{args.top_k} {metrics['top_k_hits']}/{metrics['expected']} ({metrics['ranking_method']})")
            else:
                print("  文档检索未计分 " + metrics.get("reason", metrics["status"]))
    summary = summarize_retrieval(cases, args.top_k)
    if summary["expected"]:
        print(f"候选召回率 {summary['candidate_recall']:.1%}  重排 Top-{args.top_k} 召回率 {summary['top_k_recall']:.1%}"
              f"  已计分 {summary['measured_cases']} 题，{summary['expected']} 项依据；未计分 {summary['other_cases']} 题")
    else:
        print("无可计分的文档依据，候选召回率和重排召回率均为 N/A")
    passed = sum(case["passed"] for case in cases)
    failed = len(cases) - passed
    if args.report:
        report = {"profile": args.profile, "passed": passed, "failed": failed, "retrieval": summary, "cases": cases}
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Passed {passed} / {passed + failed}   Failed {failed}")
    if passed + failed == 0:
        print("No evaluation cases supplied.", file=sys.stderr)
    return 0 if passed and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
