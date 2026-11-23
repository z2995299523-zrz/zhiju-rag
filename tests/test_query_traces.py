import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from main import create_app
from ragcore import engine
from ragcore.connectors import ConnectorError


class QueryTraceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "RAG_ADMIN_PASSWORD": "test-admin", "RAG_READER_PASSWORD": "test-reader",
            "RAG_COOKIE_SECURE": "0", "RAG_LLM_BASE_URL": "", "RAG_EMBED_BASE_URL": "",
        }))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.app = create_app(self.directory, seed=True)
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1"))
        self.client.post("/api/login", json={"password": "test-admin"}).raise_for_status()

    def traces(self, url=""):
        response = self.client.get("/api/admin/query-traces" + url)
        response.raise_for_status()
        return response.json()

    def test_mixed_stage_times_persist_and_cached_replay_adds_no_execution(self):
        retrieve = engine.document_sources

        def delayed(*args):
            time.sleep(.018)
            return retrieve(*args)

        request = {"question": "材料研发平台 2026 年预算执行率是多少，按什么口径计算？",
                   "mode": "mixed", "request_id": str(uuid4())}
        with patch.object(engine, "document_sources", side_effect=delayed):
            res = self.client.post("/api/query/stream", json=request)
        self.assertIn("event: done", res.text)
        run, = self.traces()["items"]
        self.assertEqual(run["outcome"], "answered")
        self.assertEqual(run["request_id"], request["request_id"])
        self.assertGreaterEqual(run["duration_ms"], 18)
        self.assertNotIn(request["question"], json.dumps(run, ensure_ascii=False))
        self.assertEqual([step["name"] for step in run["stages"]],
                         ["parse", "parameters", "data", "retrieval", "generation", "archive"])
        self.assertGreaterEqual(run["stages"][3]["duration_ms"], 18)
        self.assertTrue(all(s["duration_ms"] >= 0 and s["status"] == "ok" for s in run["stages"]))
        self.assertAlmostEqual(sum(s["duration_ms"] for s in run["stages"]), run["duration_ms"], delta=1)
        self.assertEqual(self.client.get(f"/api/history/{run['history_id']}").json()["question"], request["question"])
        self.client.post("/api/query/stream", json=request).raise_for_status()
        with TestClient(create_app(self.directory), base_url="http://127.0.0.1") as restarted:
            restarted.post("/api/login", json={"password": "test-admin"}).raise_for_status()
            self.assertEqual(restarted.get("/api/admin/query-traces").json()["items"], [run])

    def test_failures_empty_and_reader_boundary(self):
        request = {"question": "保密业务参数", "mode": "data", "connector_id": 1}
        with patch.object(engine, "data_source", side_effect=ConnectorError("private-upstream-test-detail")):
            result = self.client.post("/api/query", json=request).json()
        self.assertEqual(result["status"], "error")
        run, = self.traces()["items"]
        self.assertEqual(run["outcome"], "error")
        self.assertEqual([(s["name"], s["status"]) for s in run["stages"] if s["status"] != "ok"], [("data", "failed")])
        self.assertNotIn("private-upstream-test-detail", json.dumps(run))

        broken = {**request, "connector_id": 999}
        self.assertEqual(self.client.post("/api/query", json=broken).status_code, 404)
        with patch.object(engine, "document_sources", return_value=[]):
            self.client.post("/api/query", json={"question": "未匹配内容", "mode": "documents"}).raise_for_status()
        page = self.traces("?limit=1")
        empty = page["items"][0]
        self.assertEqual(empty["outcome"], "no_evidence")
        self.assertIn(("retrieval", "empty"), [(s["name"], s["status"]) for s in empty["stages"]])
        failure = self.traces(f"?before={page['next_before']}&limit=1")["items"][0]
        self.assertIsNone(failure["history_id"])
        self.assertEqual(failure["outcome"], "error")
        self.assertEqual([(s["name"], s["status"]) for s in failure["stages"]], [("parse", "failed")])
        with TestClient(self.app, base_url="http://127.0.0.1") as reader:
            self.assertEqual(reader.get("/api/admin/query-traces").status_code, 401)
            reader.post("/api/login", json={"password": "test-reader"}).raise_for_status()
            self.assertEqual(reader.get("/api/admin/query-traces").status_code, 403)

    def test_embedding_failure_preserves_answer_and_identifies_fallback(self):
        with (
            patch.dict(os.environ, {"RAG_EMBED_BASE_URL": "http://127.0.0.1:8/v1", "RAG_EMBED_MODEL": "test"}),
            patch("ragcore.models._embed", side_effect=TimeoutError("private-upstream-detail")),
        ):
            self.client.post("/api/query", json={
                "question": "科研项目经费报销需要哪些材料？", "mode": "documents"
            }).raise_for_status()
        run, = self.traces()["items"]
        self.assertEqual(run["outcome"], "answered")
        step = next(stage for stage in run["stages"] if stage["name"] == "retrieval")
        self.assertEqual(step["notes"], ["lexical_fallback"])
        self.assertNotIn("private-upstream-detail", json.dumps(run))
