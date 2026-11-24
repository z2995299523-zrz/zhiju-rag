import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import create_app
from ragcore import models


class RetrievalCheckApiTests(unittest.TestCase):
    endpoint = "/api/admin/retrieval/evaluate"

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "RAG_ADMIN_PASSWORD": "test-admin", "RAG_READER_PASSWORD": "test-reader",
            "RAG_COOKIE_SECURE": "0", "RAG_EMBED_BASE_URL": "",
        }))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.client = self.enterContext(TestClient(create_app(directory, seed=False)))
        self.login("test-admin")

    def login(self, password):
        self.assertEqual(self.client.post("/api/login", json={"password": password}).status_code, 200)

    def upload(self, name, text):
        response = self.client.post("/api/documents", files={"files": (name, text.encode())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_source_versions_real_ranks_and_reader_permissions(self):
        name = "报销规范.txt"
        document = self.upload(name, "采购类支出报销需提交报销单和验收单。")["imported"][0]
        self.assertGreaterEqual(document["parse_ms"], 0)
        doc_id = document["id"]
        chunk = self.client.get(f"/api/documents/{doc_id}/preview").json()["chunks"][0]
        payload = {"question": "报销需提交什么？", "document_id": doc_id, "chunk_id": chunk["id"]}
        result = self.client.post(self.endpoint, json=payload)
        self.assertEqual(result.status_code, 200, result.text)
        scores = result.json()["metrics"]
        self.assertEqual(scores["evidence"][0]["expected"]["sha256"], document["sha256"])
        self.assertEqual((scores["candidate_hits"], scores["top_k_hits"], scores["first_relevant_rank"]), (1, 1, 1))
        self.assertEqual(result.json()["top_chunks"][0]["chunk_id"], chunk["id"])
        self.assertEqual(self.client.get("/api/history?scope=workspace").json(), [])
        other = self.upload("其他资料.txt", "设备编号需要逐项登记。")["imported"][0]["id"]
        self.assertEqual(self.client.post(self.endpoint, json={**payload, "document_id": other}).status_code, 422)
        self.upload(name, "报销需提交新版本报销单。")
        self.assertEqual(self.client.post(self.endpoint, json=payload).status_code, 404)
        self.login("test-reader")
        self.assertEqual(self.client.post(self.endpoint, json=payload).status_code, 403)

    def test_import_reports_duration_only_when_parser_ran(self):
        self.assertGreaterEqual(self.upload("损坏.json", "{")["errors"][0]["parse_ms"], 0)
        self.assertNotIn("parse_ms", self.upload("x" * 201 + ".txt", "a")["errors"][0])

    def test_reports_measured_similarity_without_returning_vectors(self):
        document = self.upload("设备记录.txt", "设备需要登记设备编号。")['imported'][0]
        payload = {"question": "设备编号如何登记？", "document_id": document["id"]}
        disabled = self.client.post(self.endpoint, json=payload).json()
        self.assertEqual(disabled["embedding"], {"dimension": None, "outcome": None})
        self.assertIsNone(disabled["top_chunks"][0]["semantic_score"])

        def vectors(settings, inputs, timeout):
            return [models._vector([1, 0, 0] if i == 0 else [3, 4, 0]) for i in range(len(inputs))]

        with (patch.dict(os.environ, {"RAG_EMBED_BASE_URL": "http://127.0.0.1:1/v1",
                                      "RAG_EMBED_MODEL": "test-visualization", "RAG_EMBED_MIN_SCORE": "0.50"}),
              patch.object(models, "_request_vectors", side_effect=vectors)):
            response = self.client.post(self.endpoint, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["embedding"], {"dimension": 3, "outcome": "ranked"})
        self.assertAlmostEqual(result["top_chunks"][0]["semantic_score"], 0.6)
