"""Administrator model settings, transport, and restart regressions."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import create_app
from ragcore import model_secrets


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {
            "RAG_ADMIN_PASSWORD": "admin-test", "RAG_READER_PASSWORD": "reader-test", "RAG_COOKIE_SECURE": "0",
            "RAG_LLM_BASE_URL": "", "RAG_LLM_MODEL": "", "RAG_LLM_API_KEY": "",
            "RAG_EMBED_BASE_URL": "", "RAG_EMBED_MODEL": "", "RAG_EMBED_API_KEY": "",
            "HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.app = create_app(self.temp.name, seed=True)
        self.admin = TestClient(self.app, base_url="http://127.0.0.1")
        self.enterContext(self.admin)
        self.admin.post("/api/login", json={"password": "admin-test"}).raise_for_status()
        self.requests = []
        calls = self.requests

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers.get("Authorization"), payload))
                mime, status = "application/json", 200
                if payload["model"] == "unavailable":
                    body, status = b'{"error":"do-not-echo-provider-text"}', 403
                elif self.path.endswith("/embeddings"):
                    body = json.dumps({"data": [{"index": i, "embedding": [1, 2]}
                                                for i, _ in enumerate(payload["input"])]}).encode()
                elif len(payload["messages"]) > 1:
                    data = json.loads(payload["messages"][-1]["content"])
                    first = data["sources"][0]
                    text = "报销时需提交报销申请单和有效发票。"
                    answer = json.dumps({"text": text, "support": [{"id": first["id"], "quote": first["text"]}]})
                    if payload.get("stream"):
                        delta = json.dumps({"choices": [{"delta": {"content": answer}}]})
                        body, mime = f"data: {delta}\n\ndata: [DONE]\n\n".encode(), "text/event-stream"
                    else:
                        body = json.dumps({"choices": [{"message": {"content": answer}}]}).encode()
                else:
                    body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}/v1"
        self.endpoint = "/api/admin/models"

    def save(self, kind, **overrides):
        draft = {"mode": "workspace", "base_url": self.base, "model": kind + "-test", **overrides}
        response = self.admin.put(self.endpoint + "/" + kind, json=draft)
        response.raise_for_status()
        return response.json()

    def test_admin_permissions_safe_failures_and_draft_does_not_save(self):
        reader = TestClient(self.app, base_url="http://127.0.0.1")
        self.addCleanup(reader.close)
        for code in [401, 403]:
            self.assertEqual(reader.get(self.endpoint).status_code, code)
            for method, url in ((reader.put, "/llm"), (reader.post, "/llm/test")):
                result = method(self.endpoint + url, json={"mode": "workspace", "base_url": self.base, "model": "x"})
                self.assertEqual(result.status_code, code)
            if code == 401:
                reader.post("/api/login", json={"password": "reader-test"}).raise_for_status()
        path = self.endpoint + "/llm/test"
        draft = {"mode": "workspace", "base_url": self.base, "model": "unavailable", "api_key": "fixture-value"}
        rejected = self.admin.post(path, json=draft)
        self.assertEqual(rejected.status_code, 422)
        for sensitive in ("fixture-value", "do-not-echo-provider-text"):
            self.assertNotIn(sensitive, rejected.text)
        self.assertEqual(self.admin.get(self.endpoint).json()["llm"]["source"], "none")
        for base in ("http://remote.invalid/v1", "https://user:pass@remote.invalid/v1", self.base + "?key=fixture"):
            denied = self.admin.post(path, json={**draft, "base_url": base})
            self.assertEqual(denied.status_code, 422)
        self.assertEqual(len(self.requests), 1)

    def test_embedding_concurrent_probe_validates_two_batches(self):
        self.save("embedding")
        response = self.admin.post(self.endpoint + "/embedding/test?concurrent_batches=true")
        response.raise_for_status()
        report = response.json()
        self.assertEqual((report["batches"], report["batch_size"], report["dimension"]), (2, 8, 2))
        self.assertEqual([len(payload["input"]) for _, _, payload in self.requests], [8, 8])
        self.assertEqual(self.admin.post(self.endpoint + "/llm/test?concurrent_batches=true").status_code, 422)

    def test_real_calls_use_saved_profiles_and_survive_restart(self):
        for kind in ("llm", "embedding"):
            state = self.save(kind, api_key=kind + "-fixture-key")
            self.assertEqual((state["source"], state["enabled"], state["has_api_key"]), ("workspace", True, True))
            self.assertNotIn("fixture-key", str(state))
            tested = self.admin.post(self.endpoint + f"/{kind}/test")
            tested.raise_for_status()
            self.assertTrue(tested.json()["ok"])
        question = {"question": "科研项目经费报销需要哪些材料？", "mode": "documents"}
        returned = self.admin.post("/api/query", json=question).json()
        self.assertEqual(returned["status"], "answered")
        self.assertIn("报销时需提交报销申请单和有效发票。 [S1]", returned["answer"])
        stream = self.admin.post("/api/query/stream", json=question)
        self.assertIn("event: done", stream.text)
        self.assertNotIn("fixture-key", stream.text)
        self.assertTrue(any(payload.get("stream") for _, _, payload in self.requests))
        self.assertTrue(any(len(payload.get("input", [])) > 1 for _, _, payload in self.requests))
        for _, header, payload in self.requests:
            self.assertEqual(header, "Bearer " + payload["model"].replace("-test", "-fixture-key"))
        with TestClient(create_app(self.temp.name, seed=False), base_url="http://127.0.0.1") as restarted:
            restarted.post("/api/login", json={"password": "admin-test"}).raise_for_status()
            for kind in ("llm", "embedding"):
                self.assertTrue(restarted.get(self.endpoint).json()[kind]["has_api_key"])
                restarted.post(self.endpoint + f"/{kind}/test").raise_for_status()
        for file in Path(self.temp.name).iterdir():
            self.assertNotIn(b"fixture-key", file.read_bytes())

    def test_environment_override_key_retention_and_destination_change(self):
        draft = {"mode": "workspace", "base_url": self.base, "model": "edited"}
        env = {"RAG_LLM_BASE_URL": self.base, "RAG_LLM_MODEL": "from-env", "RAG_LLM_API_KEY": "env-fixture"}
        probe = self.endpoint + "/llm/test"
        with patch.dict(os.environ, env):
            self.assertEqual(self.admin.get(self.endpoint).json()["llm"]["source"], "environment")
            self.save("llm")
            self.admin.post(probe).raise_for_status()
            self.assertEqual(self.requests[-1][1], "Bearer env-fixture")
            self.save("llm", api_key="replacement-fixture")
            self.save("llm", mode="disabled")
            self.assertEqual(self.admin.post(probe).status_code, 422)
            self.save("llm", api_key="  ")
            self.admin.post(probe).raise_for_status()
            self.assertEqual(self.requests[-1][1], "Bearer replacement-fixture")
            new_base = self.base.replace("/v1", "/another/v1")
            self.admin.post(probe, json={**draft, "base_url": new_base}).raise_for_status()
            self.assertIsNone(self.requests[-1][1])
            self.assertEqual(self.admin.get(self.endpoint).json()["llm"]["base_url"], self.base)
            self.assertFalse(self.save("llm", base_url=new_base)["has_api_key"])
            self.save("llm", api_key="replacement-fixture")
            self.assertFalse(self.save("llm", clear_api_key=True)["has_api_key"])
            self.admin.post(probe).raise_for_status()
            self.assertIsNone(self.requests[-1][1])
            self.assertEqual(self.save("llm", mode="environment")["model"], "from-env")
            self.admin.post(probe).raise_for_status()
            self.assertEqual(self.requests[-1][1], "Bearer env-fixture")

    def test_credential_binding_and_protected_key_permissions(self):
        cipher = model_secrets.seal(self.temp.name, "llm", self.base, "fixture-only")
        self.assertNotIn("fixture-only", cipher)
        self.assertEqual(model_secrets.unseal(self.temp.name, "llm", self.base, cipher), "fixture-only")
        with self.assertRaises(model_secrets.CredentialError):
            model_secrets.unseal(self.temp.name, "llm", self.base + "/elsewhere", cipher)
        if os.name != "nt":
            path = Path(self.temp.name) / ".model-credentials.key"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.chmod(0o644)
            with self.assertRaises(model_secrets.CredentialError):
                model_secrets.unseal(self.temp.name, "llm", self.base, cipher)
