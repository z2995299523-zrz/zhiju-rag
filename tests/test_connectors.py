import hashlib
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from ragcore import connectors


def budget_config():
    return {
        "name": "项目预算", "url": "demo://budget",
        "parameters": [
            {"name": "project", "label": "项目名称"},
            {"name": "year", "label": "年度", "type": "integer"},
        ],
    }


class ConnectorTests(unittest.TestCase):
    def test_demo_evidence_matches_filters(self):
        config = budget_config()
        result = connectors.execute(config, {"year": "2026", "project": "材料研发平台"})
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["rows"][0]["预算执行率"], "75%")
        encoded = json.dumps(result["rows"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(result["response_sha256"], hashlib.sha256(encoded).hexdigest())
        self.assertNotIn("raw_body", result)
        self.assertEqual(connectors.execute(config, {"year": "2026", "project": "材料研发平台"},
                                            include_raw=True)["raw_body"], encoded)
        self.assertIn("year=2026", result["request"])
        self.assertEqual(connectors.execute(config, {"year": "2024"})["rows"], [])

    def test_rejects_invalid_parameters(self):
        config = budget_config()
        config["parameters"][0]["required"] = True
        for values in ({"year": 2025}, {"y": 2025}, {"project": "X", "year": 2.5}):
            with self.subTest(values=values), self.assertRaises(connectors.ConnectorError):
                connectors.execute(config, values)

    def test_target_configuration_and_credentials(self):
        base = {"name": "查询", "url": "https://reports.example/data"}
        with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS": "reports.example"}):
            connectors.validate_config({**base, "token_env": "RAG_CONNECTOR_REPORTS"})
            for change in (
                {"url": "http://reports.example/data", "token_env": "RAG_CONNECTOR_REPORTS"},
                {"token_env": "OTHER_APP_KEY"},
                {"url": "https://reports.example:9876/data"},
                {"url": "https://name:pass@reports.example/data"},
                {"url": "https://reports.example/data?token=example"},
            ):
                with self.subTest(change=change), self.assertRaises(connectors.ConnectorError):
                    connectors.validate_config({**base, **change})

    def test_address_policy(self):
        with patch.dict(os.environ, {"RAG_ALLOW_PRIVATE_NETWORK": "0"}):
            self.assertFalse(connectors._permitted_ip("10.1.2.3"))
            self.assertFalse(connectors._permitted_ip("127.0.0.1"))
        with patch.dict(os.environ, {"RAG_ALLOW_PRIVATE_NETWORK": "1"}):
            self.assertTrue(connectors._permitted_ip("10.1.2.3"))
            self.assertFalse(connectors._permitted_ip("169.254.1.1"))
            self.assertFalse(connectors._permitted_ip("224.0.0.5"))

    def test_http_response_hash_path_and_redirect(self):
        body = '{"data": {"items": [{"value": 24}]}}'.encode()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/other")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            authority = "127.0.0.1:" + str(server.server_port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS": authority, "RAG_ALLOW_PRIVATE_NETWORK": "1"}):
                    config = {
                        "name": "报表", "url": f"http://{authority}/data", "records_path": "data.items",
                        "parameters": [{"name": "date", "type": "date"}],
                    }
                    with self.assertRaises(connectors.ConnectorError):
                        connectors.execute(config, {"date": "2026-02-30"})
                    result = connectors.execute(config, {"date": "2026-11-23"})
                    self.assertEqual(result["rows"], [{"value": 24}])
                    self.assertEqual(result["response_sha256"], hashlib.sha256(body).hexdigest())
                    with self.assertRaisesRegex(connectors.ConnectorError, "302"):
                        connectors.execute({**config, "url": f"http://{authority}/redirect"}, {})
                    self.assertEqual(requests, ["/data?date=2026-11-23", "/redirect"])
            finally:
                server.shutdown()
                thread.join(2)

    def test_cancel_waiting_http_read_closes_upstream(self):
        waiting, closed = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                waiting.set()
                self.connection.settimeout(3)
                if not self.connection.recv(1):
                    closed.set()

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, ThreadPoolExecutor(2) as pool:
            server.timeout = 4
            authority = "127.0.0.1:" + str(server.server_port)
            read = connectors.ConnectorRead()
            pool.submit(server.handle_request)
            with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS": authority, "RAG_ALLOW_PRIVATE_NETWORK": "1"}):
                result = pool.submit(connectors.execute, {"name": "业务查询", "url": f"http://{authority}/rows"},
                                     {}, read=read)
                self.assertTrue(waiting.wait(3))
                read.cancel()
                with self.assertRaises(connectors.ConnectorError):
                    result.result(timeout=3)
                self.assertTrue(closed.wait(3))

    def test_raw_snapshot_and_lossless_numbers(self):
        config = {"name": "财务", "url": "https://finance.example/rows", "records_path": "rows",
                  "parameters": [{"name": "amount", "type": "number"}]}
        body = (b'{"rows": [{"amount": 0.10, "exact": 7.25, "precise": 0.12345678901234567890, '
                b'"id": 9007199254740993, "tiny": 1e-10000}], "source": 1}\n')
        with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS": "finance.example"}), \
                patch.object(connectors, "_get_body", return_value=body):
            result = connectors.execute(config, {"amount": "123456789012345678.01"}, include_raw=True)
        self.assertEqual(result["rows"], [{"amount": "0.10", "exact": 7.25, "precise": "0.12345678901234567890",
                                           "id": "9007199254740993", "tiny": "1e-10000"}])
        self.assertEqual(result["raw_body"], body)
        self.assertEqual(result["response_sha256"], hashlib.sha256(body).hexdigest())
        self.assertIn("amount=123456789012345678.01", result["request"])


if __name__ == "__main__":
    unittest.main()
