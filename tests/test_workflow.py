"""HTTP-level acceptance tests for the offline evidence and access workflows."""

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from main import create_app
from ragcore import engine


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(os.environ, {
            "RAG_ADMIN_PASSWORD": "test-admin", "RAG_READER_PASSWORD": "test-reader",
            "RAG_COOKIE_SECURE": "0", "RAG_LLM_BASE_URL": "", "RAG_EMBED_BASE_URL": "",
        })
        settings.start()
        self.addCleanup(settings.stop)
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.app = create_app(data_dir=workspace.name, seed=True)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.login(self.client, "test-admin")

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def login(self, client, password):
        response = client.post("/api/login", json={"password": password})
        self.ok(response)
        return response

    def ask(self, question, *, client=None, **kwargs):
        answer = self.ok((client or self.client).post("/api/query", json={"question": question, **kwargs}))
        self.assertEqual(answer["question"], question)
        return answer

    def upload(self, name, content):
        return self.ok(self.client.post("/api/documents", files={"files": (name, content)}))

    def stream(self, client, payload):
        response = client.post("/api/query/stream", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return [(name.removeprefix("event: "), json.loads(data.removeprefix("data: ")))
                for name, data in (block.splitlines() for block in response.text.strip().split("\n\n"))]

    def test_streamed_examples_match_archive_and_current_workspace(self):
        examples = self.ok(self.client.get("/api/query/examples"))["examples"]
        self.assertEqual([ex["mode"] for ex in examples], ["documents", "data", "mixed"])
        for example in examples:
            events = self.stream(self.client, {"question": example["question"], "mode": example["mode"]})
            self.assertEqual((events[0][0], events[-1][0]), ("status", "done"))
            result = events[-1][1]["result"]
            self.assertEqual(result["status"], "answered", example)
            self.assertEqual(result["answer"], "".join(data["text"] for event, data in events if event == "delta"))
            self.assertEqual(result["citations"], dict(events)["evidence"]["citations"])
            self.assertEqual(self.ok(self.client.get(f"/api/history/{result['id']}")), result)
        self.assertIn("预算执行率", result["citations"][0]["metric_definition"])
        rows = self.stream(self.client, {"question":"查询所有科研预算记录", "mode":"data"})
        self.assertGreater(len([d for n,d in rows if n == "delta"]), 1)
        self.assertIn("正在查询业务数据…", [d["message"] for n,d in rows if n == "status"])
        self.assertEqual("".join(d["text"] for n,d in rows if n == "delta"), rows[-1][1]["result"]["answer"])
        with self.app.state.store.db() as conn:
            conn.execute("UPDATE documents SET active=0")
            conn.execute("DELETE FROM connectors")
        self.assertEqual(self.ok(self.client.get("/api/query/examples")), {"examples": []})
        self.upload("设备登记手册.txt", "设备登记需要提供资产编号和验收日期。".encode())
        updated, = self.ok(self.client.get("/api/query/examples"))["examples"]
        self.assertEqual(updated["mode"], "documents")
        self.assertIn("设备登记手册", updated["question"])
        self.assertEqual(self.stream(self.client, updated)[-1][1]["result"]["status"], "answered")

    def test_failed_model_after_first_delta_sends_error_and_no_history(self):
        async def failed(question, citations):
            yield citations[0]["excerpt"][:1]
            raise ValueError("provider stream stopped")
        with patch("ragcore.models.model_enabled", return_value=True), patch("ragcore.models.stream_answer", new=failed):
            with self.assertLogs("main", "ERROR"):
                events = self.stream(self.client, {"question":"科研项目经费报销需要哪些材料？", "mode":"documents"})
        self.assertEqual([name for name, _ in events][-2:], ["delta", "error"])
        self.assertNotIn("provider stream stopped", events[-1][1]["message"])
        self.assertEqual(self.ok(self.client.get("/api/history")), [])

    def test_request_id_recovers_lost_done_and_serializes_concurrent_retries(self):
        payload = {"request_id": str(uuid4()), "question":"科研项目经费报销需要哪些材料？", "mode":"documents"}
        started = threading.Event()
        retry_arrived = threading.Event()
        store = self.app.state.store
        real_reserve = store.reserve_query
        real_search = engine.document_sources
        def reserve(*args):
            real_reserve(*args)
            if started.is_set():
                retry_arrived.set()
        def delayed_search(*args):
            started.set()
            self.assertTrue(retry_arrived.wait(5))
            return real_search(*args)
        with patch.object(store, "reserve_query", side_effect=reserve), patch.object(engine, "document_sources", side_effect=delayed_search) as search:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.stream, self.client, payload)
                self.assertTrue(started.wait(5))
                other = pool.submit(self.stream, self.client, payload)
                result = first.result()[-1]
                self.assertEqual(other.result(), [result])
            self.assertEqual(search.call_count, 1)
        self.assertEqual(result[0], "done")
        self.assertEqual(self.stream(self.client, payload), [result])
        conflict = self.client.post("/api/query/stream", json={**payload, "params":{"year":2026}})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.ok(self.client.get("/api/history"))), 1)
        with TestClient(self.app, base_url="http://127.0.0.1") as reader:
            self.login(reader, "test-reader")
            own = self.stream(reader, payload)[-1][1]["result"]
            self.assertNotEqual(own["id"], result[1]["result"]["id"])

    def test_stream_permissions_include_sql_only_for_admin_and_never_in_deltas(self):
        sql = "select private_column from stock"
        raw = json.dumps({"executed_sql": sql, "records": [{"stock": 1, "sql": sql}]}).encode()
        host = "warehouse.example.test"
        with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS":host}):
            config = self.ok(self.client.post("/api/connectors", json={
                "name":"库存查询", "url":f"https://{host}/stock", "records_path":"records"}))
            query = {"question":"查询库存", "mode":"data", "connector_id":config["id"]}
            with TestClient(self.app, base_url="http://127.0.0.1") as reader, patch("ragcore.connectors._get_body", return_value=raw):
                self.assertEqual(reader.post("/api/query/stream", json=query).status_code, 401)
                self.login(reader, "test-reader")
                for client in (reader, self.client):
                    events = self.stream(client, query)
                    self.assertEqual(events[-1][0], "done")
                    result = events[-1][1]["result"]
                    evidence, = dict(events)["evidence"]["citations"]
                    self.assertEqual(evidence["rows"], [{"stock":1}])
                    self.assertNotIn(sql, result["answer"])
                    self.assertEqual(result["answer"], "".join(d["text"] for n,d in events if n == "delta"))
                    self.assertEqual(evidence.get("executed_sql"), sql if client is self.client else None)
                    history = self.ok(client.get(f"/api/history/{result['id']}"))
                    self.assertEqual(result, history)
                    for representation in ("rows", "raw"):
                        url = f"/api/history/{result['id']}/sources/S1/{representation}"
                        self.assertEqual(client.get(url).status_code, 200 if client is self.client else 403)

    def test_stream_aborts_and_closes_provider_before_archiving(self):
        closed = threading.Event()
        async def stalled(question, citations):
            try:
                yield citations[0]["excerpt"][:1]
                await asyncio.Future()
            finally:
                closed.set()

        async def disconnect_after_delta():
            path = "/api/query/stream"
            body = json.dumps({"mode":"documents", "question":"科研项目经费报销需要哪些材料？"}).encode()
            cookie = "rag_session=" + self.client.cookies["rag_session"]
            scope = {"type":"http", "asgi":{"version":"3.0","spec_version":"2.4"}, "http_version":"1.1",
                     "scheme":"http", "server":("127.0.0.1",80), "client":("127.0.0.1",4321),
                     "method":"POST", "path":path, "raw_path":path.encode(), "query_string":b"",
                     "headers":[(b"host",b"127.0.0.1"),(b"cookie",cookie.encode()),
                                (b"content-type",b"application/json"),(b"content-length",str(len(body)).encode())]}
            first = True
            disconnect = asyncio.Event()
            async def receive():
                nonlocal first
                if first:
                    first = False
                    return {"type":"http.request","body":body}
                await disconnect.wait()
                return {"type":"http.disconnect"}
            async def send(message):
                data = message.get("body", b"")
                self.assertNotIn(b"event: done", data)
                if b"event: delta" in data:
                    disconnect.set()
            await asyncio.wait_for(self.app(scope, receive, send), timeout=5)
            self.assertTrue(disconnect.is_set())
        with patch("ragcore.models.model_enabled", return_value=True), patch("ragcore.models.stream_answer", new=stalled):
            asyncio.run(disconnect_after_delta())
        self.assertTrue(closed.is_set())
        self.assertEqual(self.ok(self.client.get("/api/history")), [])

    def test_seeded_questions_return_checkable_evidence(self):
        doc = self.ask("科研项目经费报销需要哪些材料？")
        self.assertEqual((doc["intent"], doc["status"]), ("documents", "answered"))
        self.assertIn("发票", doc["answer"])
        for evidence in doc["citations"]:
            download = self.client.get(f"/api/documents/{evidence['document_id']}/download")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(hashlib.sha256(download.content).hexdigest(), evidence["sha256"])
            self.assertIn(evidence["excerpt"], download.content.decode())
            self.assertTrue(evidence["locator"])
            self.assertIn(f"[{evidence['id']}]", doc["answer"])

        question = "材料研发平台 2026 年预算执行率是多少？"
        data = self.ask(question)
        self.assertEqual((data["intent"], data["status"]), ("data", "answered"))
        self.assertEqual(data["params"], {"project": "材料研发平台", "year": 2026})
        self.assertIn("75%", data["answer"])
        api, = data["citations"]
        self.assertEqual(api["kind"], "api")
        row, = api["rows"]
        self.assertEqual((row["年度"], row["项目名称"], row["预算执行率"]), (2026, "材料研发平台", "75%"))
        serialized = json.dumps(api["rows"], ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(serialized).hexdigest(), api["rows_sha256"])
        self.assertTrue(datetime.fromisoformat(api["fetched_at"]).tzinfo)
        self.assertIn("demo://budget", api["request"])

        mixed = self.ask("材料研发平台 2026 年预算执行率是多少，按什么口径计算？")
        self.assertEqual((mixed["intent"], mixed["status"]), ("mixed", "answered"))
        self.assertEqual({c["kind"] for c in mixed["citations"]}, {"api", "document"})
        for source in mixed["citations"]:
            self.assertIn(f"[{source['id']}]", mixed["answer"])
        self.assertIn("75%", mixed["answer"])
        self.assertIn("年度预算", mixed["answer"])

        self.ok(self.client.delete(f"/api/connectors/{data['connector_id']}"))
        self.assertEqual(self.ok(self.client.get(f"/api/history/{data['id']}")), data)
        fresh = self.ask(question, mode="data")
        self.assertEqual((fresh["status"], fresh["citations"]), ("no_evidence", []))

    def test_import_versioning_failed_replacement_and_historical_original(self):
        filename = "Neodymium.md"
        old_bytes = b"Neodymium registration requires an amber label."
        new_bytes = b"Neodymium registration requires a green label."
        old, = self.upload(filename, old_bytes)["imported"]
        duplicate, = self.upload(filename, old_bytes)["imported"]
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(old["id"], duplicate["id"])
        question = "What label does Neodymium registration require?"
        historical = self.ask(question)
        self.assertEqual(historical["status"], "answered")
        self.assertIn("amber label", historical["answer"])

        overview = self.ok(self.client.get("/api/overview"))
        failed = self.upload(filename, b"")
        self.assertEqual(failed["imported"], [])
        self.assertEqual([e["filename"] for e in failed["errors"]], [filename])
        self.assertEqual(self.ok(self.client.get("/api/overview")), overview)
        surviving = self.ask(question)
        self.assertEqual([c["document_id"] for c in surviving["citations"]], [old["id"]])

        new, = self.upload(filename, new_bytes)["imported"]
        self.assertNotEqual(new["id"], old["id"])
        listed = [d for d in self.ok(self.client.get("/api/documents")) if d["filename"] == filename]
        self.assertEqual([d["id"] for d in listed], [new["id"]])
        fresh = self.ask(question)
        self.assertIn("green label", fresh["answer"])
        self.assertNotIn("amber label", fresh["answer"])
        self.assertEqual([c["document_id"] for c in fresh["citations"]], [new["id"]])
        self.ok(self.client.delete(f"/api/documents/{new['id']}"))
        self.assertEqual(self.ask(question)["status"], "no_evidence")
        replay = self.ok(self.client.get(f"/api/history/{historical['id']}"))
        self.assertEqual(replay, historical)
        original = self.client.get(f"/api/documents/{replay['citations'][0]['document_id']}/download")
        self.assertEqual(original.content, old_bytes)
        self.assertEqual(original.headers["etag"].strip('"'), hashlib.sha256(old_bytes).hexdigest())

    def test_manual_filters_clarification_and_different_kinds_of_absence(self):
        config, = self.ok(self.client.get("/api/connectors"))
        ident = config["id"]
        for spec in config["parameters"]:
            spec["required"] = True
        self.ok(self.client.put(f"/api/connectors/{ident}", json=config))
        question = "预算执行率是多少？"
        clarification = self.ask(question, mode="data", connector_id=ident)
        self.assertEqual(clarification["status"], "needs_input")
        self.assertEqual(set(clarification["missing"]), {"项目名称", "年度"})
        self.assertEqual(clarification["citations"], [])
        actual = self.ask(question, connector_id=ident, params={"project": "材料研发平台", "year": 2026})
        self.assertEqual(actual["status"], "answered")
        self.assertIn("75%", actual["answer"])

        ambiguous = self.ask("材料研发平台 2025 年和 2026 年预算执行率对比")
        self.assertEqual((ambiguous["status"], ambiguous["citations"]), ("needs_input", []))
        self.assertIn("年度", ambiguous["missing"])
        none = self.ask(question, connector_id=ident, params={"project": "不存在的项目", "year": 2026})
        self.assertEqual(none["status"], "no_evidence")
        self.assertEqual(none["citations"][0]["rows"], [])
        unrelated = self.ask("长颈鹿睡觉时为什么站立？")
        self.assertEqual((unrelated["status"], unrelated["citations"]), ("no_evidence", []))

    def test_readers_history_admin_permissions_and_session_revocation(self):
        admin_query = self.ask("科研项目经费报销需要哪些材料？")
        with TestClient(self.app, base_url="http://127.0.0.1") as reader:
            self.assertEqual(reader.get("/api/history").status_code, 401)
            self.assertEqual(reader.post("/api/login", json={"password": "incorrect"}).status_code, 401)
            session = self.login(reader, "test-reader")
            cookie = session.headers["set-cookie"].lower()
            self.assertIn("httponly", cookie)
            self.assertIn("samesite=strict", cookie)
            self.assertEqual(self.ok(reader.get("/api/session"))["role"], "reader")
            self.assertEqual(reader.post("/api/documents", files={"files": ("test.txt", b"a")}).status_code, 403)
            self.assertEqual(reader.get("/api/audit").status_code, 403)
            self.assertEqual(reader.get(f"/api/history/{admin_query['id']}").status_code, 404)
            self.assertEqual(reader.get(f"/api/history/{admin_query['id']}/sources/S1/raw").status_code, 404)
            own = self.ask("科研项目经费报销需要哪些材料？", client=reader)
            self.assertEqual([item["id"] for item in self.ok(reader.get("/api/history"))], [own["id"]])
            seen_by_admin = {item["id"] for item in self.ok(self.client.get("/api/history"))}
            self.assertTrue({own["id"], admin_query["id"]} <= seen_by_admin)
            for origin in ("https://outside.example", "https://127.0.0.1"):
                with self.subTest(origin=origin):
                    other_origin = reader.post("/api/query", json={"question": "报销材料"},
                                               headers={"Origin": origin})
                    self.assertEqual(other_origin.status_code, 403)
            token = reader.cookies.get("rag_session")
            self.ok(reader.post("/api/logout"))
            expired = reader.get("/api/history", headers={"Cookie": f"rag_session={token}"})
            self.assertEqual(expired.status_code, 401)

    def test_http_connector_query_snapshot_survives_upstream_change(self):
        requests = []
        upstream = {"data": {"records": [{"sku": "A42", "quantity": 17}]}}

        class Inventory(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                body = json.dumps(upstream, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        with HTTPServer(("127.0.0.1", 0), Inventory) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{server.server_port}"
                with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS": host, "RAG_ALLOW_PRIVATE_NETWORK": "1"}):
                    settings = {"name": "库存接口", "url": f"http://{host}/inventory", "keywords": ["库存"],
                                "records_path": "data.records", "parameters": [
                                    {"name": "sku", "label": "货号", "type": "string", "required": True}]}
                    config = self.ok(self.client.post("/api/connectors", json=settings))
                    arguments = {"connector_id": config["id"], "params": {"sku": "A42"}}
                    result = self.ask("查库存", **arguments)
                    self.assertEqual(result["status"], "answered")
                    snapshot, = result["citations"]
                    self.assertEqual(snapshot["rows"], upstream["data"]["records"])
                    raw = json.dumps(upstream, separators=(",", ":")).encode()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), snapshot["sha256"])
                    upstream["data"]["records"][0]["quantity"] = 19
                    fresh = self.ask("查库存", **arguments)
                    self.assertEqual(fresh["citations"][0]["rows"][0]["quantity"], 19)
                    self.assertEqual(self.ok(self.client.get(f"/api/history/{result['id']}")), result)
                    snap_url = f"/api/history/{result['id']}/sources/{snapshot['id']}"
                    response = self.client.get(snap_url + "/raw")
                    self.assertEqual(response.content, raw)
                    self.assertEqual(response.headers["etag"].strip('"'), snapshot["sha256"])
                    records = self.client.get(snap_url + "/rows")
                    self.assertEqual(records.json(), [{"sku": "A42", "quantity": 17}])
                    self.assertEqual(hashlib.sha256(records.content).hexdigest(), snapshot["rows_sha256"])
                    self.assertEqual(requests, ["/inventory?sku=A42"] * 2)
            finally:
                server.shutdown()
                thread.join()

    def test_model_selected_connector_keeps_multi_year_clarification(self):
        config, = self.ok(self.client.get("/api/connectors"))
        second = self.ok(self.client.post("/api/connectors", json={**config, "name": config["name"] + "二号"}))
        question = "材料研发平台 2025 年和 2026 年预算执行率对比"
        selection = {"intent": "data", "connector_id": second["id"], "params": {"year": "2025"}}
        with patch.dict(os.environ, {"RAG_LLM_BASE_URL": "http://127.0.0.1/v1", "RAG_LLM_MODEL": "test"}):
            with patch("ragcore.models._chat_json", return_value=selection):
                answer = self.ask(question)
        self.assertEqual(answer["status"], "needs_input")
        self.assertIn("年度", answer["missing"])
        self.assertNotIn("year", answer["params"])
        self.assertEqual(answer["citations"], [])


if __name__ == "__main__":
    unittest.main()
