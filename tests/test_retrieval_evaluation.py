import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ragcore.evaluate import assess_retrieval, evaluate_case, summarize_retrieval
from ragcore.retrieval import DocumentRanking, rank_document_chunks, score_evidence
from ragcore.storage import Store


class RetrievalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        self.store.initialize(seed=False)

    def add(self, filename, lines):
        chunks = [{"locator": f"第 {i} 行", "text": line} for i, line in enumerate(lines, 1)]
        with self.store.db() as conn:
            return self.store.insert_document(conn, filename, "\n".join(lines).encode(), chunks)

    def test_large_workspace_synonym_and_distinct_failure_stages(self):
        guide = self.add("费用指引.md", ["办理报销须附发票与支付凭证。"])
        self.add("其他资料.txt", ["气象观测记录"] * 181)
        with patch("ragcore.models.embedding_enabled", return_value=False):
            ranking = rank_document_chunks(self.store, "报账")
        self.assertEqual((ranking.candidate_method, ranking.ranking_method), ("fts5", "lexical"))
        labels = [{"filename": "费用指引.md", "sha256": guide["sha256"], "locator": "第 1 行"},
                  {"filename": "其他资料.txt", "locator": "第 2 行"}]
        metrics, errors = assess_retrieval(self.store, {"evidence": labels, "expect": {"intent": "documents"}},
                                           [ranking], top_k=1)
        self.assertEqual([row["stage"] for row in metrics["evidence"]], ["hit", "candidate_miss"])
        self.assertEqual(metrics["top_k_recall"], 0.5)
        self.assertEqual(metrics["candidate_recall"], 0.5)
        self.assertIn("candidate_miss", errors[0])

    def test_top_k_position_version_and_locator_are_scored_exactly(self):
        a = {"id": 1, "filename": "设备.txt", "locator": "A1", "sha256": "a" * 64}
        b = {**a, "id": 2, "locator": "A2"}
        c = {**a, "id": 3, "locator": "A3"}
        run = DocumentRanking([a, b, c], [a, b], "all", "lexical")
        labels = [{"filename": row["filename"], "locator": row["locator"], "sha256": row["sha256"]}
                  for row in (a, b, c)]
        labels.append({"filename": "设备.txt", "sha256": "b" * 64})
        metrics = score_evidence(run, labels, top_k=1)
        self.assertEqual([r["rank"] for r in metrics["evidence"]], [1, 2, None, None])
        self.assertEqual([r["stage"] for r in metrics["evidence"]],
                         ["hit", "ranking_miss", "ranking_miss", "candidate_miss"])
        self.assertEqual((metrics["candidate_hits"], metrics["top_k_hits"], metrics["expected"]), (3, 1, 4))
        self.assertEqual(metrics["first_relevant_rank"], 1)
        with self.assertRaisesRegex(ValueError, "重复"):
            score_evidence(run, labels[:1] * 2)

    def test_one_real_query_captures_candidates_and_actual_fusion(self):
        self.add("费用.txt", ["差旅报销要求附上发票。"])
        case = {"question": "差旅报账要求", "mode": "documents", "expect": {"intent": "documents", "status": "answered"},
                "evidence": [{"filename": "费用.txt", "locator": "第 1 行"}]}
        with patch.dict(os.environ, {"RAG_LLM_BASE_URL": ""}), patch("ragcore.models.embedding_enabled", return_value=True), \
                patch("ragcore.models.semantic_rank", side_effect=lambda q, chunks, limit: chunks) as embed:
            errors, metrics = evaluate_case(self.store, case, {}, top_k=3)
        embed.assert_called_once()
        self.assertEqual(errors, [])
        self.assertEqual((metrics["candidate_method"], metrics["ranking_method"]), ("all", "hybrid"))
        self.assertEqual(metrics["top_k_hits"], 1)
        with patch("ragcore.models.embedding_enabled", return_value=True), patch("ragcore.models.semantic_rank", return_value=None):
            self.assertEqual(rank_document_chunks(self.store, "差旅报销").ranking_method, "lexical_fallback")

    def test_missing_and_stale_labels_excluded_from_denominator(self):
        self.add("制度.txt", ["更新后的实际制度。"])
        records = []
        for intent, status, reason in (("data", "answered", "data_only"),
                                       ("documents", "no_evidence", "no_positive_evidence"),
                                       ("mixed", "answered", "unlabeled")):
            report, errors = assess_retrieval(self.store, {"expect": {"intent": intent, "status": status}}, [], 3)
            self.assertEqual((report["reason"], errors), (reason, []))
            records.append({"retrieval": report})
        stale = {"filename": "制度.txt", "sha256": "0" * 64}
        report, errors = assess_retrieval(self.store, {"expect": {"intent": "documents"}, "evidence": [stale]}, [], 3)
        self.assertEqual(report["status"], "invalid_label")
        self.assertTrue(errors)
        records.append({"retrieval": report})
        aggregate = summarize_retrieval(records, top_k=3)
        self.assertEqual(aggregate["other_cases"], 4)
        self.assertIsNone(aggregate["candidate_recall"])
        self.assertIsNone(aggregate["top_k_recall"])
