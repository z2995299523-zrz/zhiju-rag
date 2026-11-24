"""HTTP regressions for the knowledge reader and the question-first workspace."""
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from main import create_app


class ReaderApiTests(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(os.environ, {
            "RAG_ADMIN_PASSWORD": "admin-test", "RAG_READER_PASSWORD": "reader-test",
            "RAG_COOKIE_SECURE": "0", "RAG_LLM_BASE_URL": "", "RAG_EMBED_BASE_URL": "",
        })
        settings.start()
        self.addCleanup(settings.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = create_app(temp.name, seed=False)
        self.admin = TestClient(self.app, base_url="http://127.0.0.1")
        self.admin.__enter__()
        self.addCleanup(self.admin.__exit__, None, None, None)
        self.reader = TestClient(self.app, base_url="http://127.0.0.1")
        self.addCleanup(self.reader.close)
        for client, password in ((self.admin, "admin-test"), (self.reader, "reader-test")):
            self.ok(client.post("/api/login", json={"password": password}))

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upload(self, filename, data):
        result = self.ok(self.admin.post("/api/documents", files={"files": (filename, data)}))
        self.assertEqual(result["errors"], [])
        return result["imported"][0]["id"]

    def add_stored(self, filename, data):
        # Model preexisting parsed/OCR imports without making OCR a reader dependency.
        store = self.app.state.store
        with store.db() as conn:
            return store.insert_document(conn, filename, data, [{"text": "Archived scan", "locator": "第 1 页"}])["id"]

    def test_import_comparison_reads_original_version_and_raw_table_lines(self):
        source = "\n货号,备注,金额\n" + "\n".join(f"A{i},常规,{i}" for i in range(8)) + '\nZ,"两行\n备注",22'
        doc = self.upload("对照.csv", source.encode("gb18030"))
        chunks = self.ok(self.admin.get(f"/api/documents/{doc}/preview"))["chunks"]
        chunk = chunks[-1]
        root = f"/api/documents/{doc}/source-window"
        args = {"chunk_id": chunk["id"]}
        self.assertEqual(self.reader.get(root, params=args).status_code, 403)
        original = self.ok(self.admin.get(root, params=args))
        self.assertEqual((original["first"], original["last"]), (11, 12))
        lines = {row["number"]: row["text"] for row in original["rows"]}
        self.assertEqual([lines[n] for n in (2, 11, 12)], ["货号,备注,金额", 'Z,"两行', '备注",22'])
        self.assertIn("金额：22", chunk["text"])
        new = self.upload("对照.csv", b"id,n\nchanged,1")
        self.assertEqual(self.admin.get(f"/api/documents/{new}/source-window", params=args).status_code, 404)
        self.assertEqual(self.ok(self.admin.get(root, params=args)), original)

    def test_json_original_window_follows_escaped_pointer_beyond_initial_rows(self):
        original = [{"a/b~": [f"entry-{i}"]} for i in range(40)]
        for name, binary, prefix in (
                ("compact.json", json.dumps(original).encode(), ""),
                ("pretty.json", json.dumps(original, indent=2).encode(), ""),
                ("events.jsonl", b'{}\n\n' + json.dumps(original).encode(), "第 3 行")):
            doc = self.upload(name, binary)
            selected = self.ok(self.admin.get(f"/api/documents/{doc}/preview", params={"q":"entry-39"}))["chunks"]
            chunk, = selected
            self.assertIn(prefix, chunk["locator"])
            view = self.ok(self.admin.get(f"/api/documents/{doc}/source-window", params={"chunk_id":chunk["id"]}))
            self.assertEqual(view["pointer"], "/39/a~1b~0/0")
            self.assertIn("entry-39", view["rows"][0]["text"])
            self.assertNotIn("entry-38", view["rows"][0]["text"])
            if name == "pretty.json":
                self.assertGreater(view["first"], 24)
            elif prefix:
                self.assertEqual(view["first"], 3)

    def test_import_comparison_xlsx_keeps_formula_blank_header_and_sheet_coordinates(self):
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.title = "原表"
        sheet.append(["设备", None, "数量"])
        sheet.append(["设备A", "原版", "=1+2"])
        blob = io.BytesIO()
        book.save(blob)
        book.close()
        doc = self.upload("备件.xlsx", blob.getvalue())
        chunk = self.ok(self.admin.get(f"/api/documents/{doc}/preview"))["chunks"][-1]
        source = self.ok(self.admin.get(f"/api/documents/{doc}/source-window", params={"chunk_id": chunk["id"]}))
        self.assertEqual((source["kind"], source["sheet"], source["first"], source["last"]), ("sheet", "原表", 2, 2))
        self.assertEqual(source["rows"][0]["cells"][:3], ["设备", "", "数量"])
        self.assertEqual(source["rows"][1]["cells"][:3], ["设备A", "原版", "=1+2"])
        self.assertIn("第 2 列：原版", chunk["text"])

    def test_conversation_replay_search_and_branch_ownership(self):
        root = self.ok(self.reader.post("/api/query", json={"question": "unique first question", "mode": "documents"}))
        def follow(parent, question):
            body = {"question": question, "mode": "documents", "parent_id": parent}
            reply = self.reader.post("/api/query/stream", json=body)
            self.assertEqual(reply.status_code, 200)
            done = next(frame for frame in reply.text.split("\n\n") if frame.startswith("event: done\n"))
            return json.loads(done.split("data: ", 1)[1])["result"]["id"]
        middle = follow(root["id"], "second question")
        leaf = follow(middle, "third question")
        branch = follow(root["id"], "separate branch")
        for ident, expected in ((leaf, [root["id"], middle]), (branch, [root["id"]])):
            replay = self.ok(self.reader.get(f"/api/history/{ident}"))
            self.assertEqual([r["id"] for r in replay["predecessors"]], expected)
        listing = self.ok(self.reader.get("/api/history", params={"threads": True, "q": "unique first"}))
        self.assertEqual([r["id"] for r in listing], [branch, leaf])
        self.assertFalse(self.ok(self.admin.get(f"/api/history/{leaf}"))["can_continue"])
        foreign = self.admin.post("/api/query/stream", json={"question": "follow", "parent_id": leaf})
        self.assertEqual(foreign.status_code, 404)

    def test_reader_results_and_archive_do_not_expose_connector_transport(self):
        url = "https://inventory.example.test/private/stock"
        with patch.dict(os.environ, {"RAG_ALLOWED_HOSTS": "inventory.example.test"}):
            config = self.ok(self.admin.post("/api/connectors", json={
                "name": "Stock", "url": url, "token_env": "RAG_CONNECTOR_STOCK", "parameters": [
                    {"name": "sku", "label": "货号", "type": "string", "required": True}
                ]
            }))
            catalog, = self.ok(self.reader.get("/api/connectors"))
            self.assertEqual(catalog.keys(), {"id", "name", "description", "parameters"})
            raw = b'{"count":6,"access_token":"private-value"}'
            fetched = {"rows": [json.loads(raw)], "request": "GET " + url + "?sku=x", "raw_body": raw, "applied_params": {"sku": "x"},
                       "fetched_at": "2026-01-01T00:00:00+00:00", "response_sha256": hashlib.sha256(raw).hexdigest()}
            with patch("ragcore.engine.execute", return_value=fetched):
                result = self.ok(self.reader.post("/api/query", json={
                    "question": "Stock", "mode": "data", "connector_id": config["id"], "params": {"sku": "x"}
                }))
            self.assertEqual(result["citations"][0]["filters"], [{"label": "货号", "value": "x"}])
            self.assertEqual(result["citations"][0]["rows"], [{"count": 6}])
            self.assertEqual(result["citations"][0]["returned_count"], 1)
            self.assertIsNone(result["citations"][0]["total_count"])
            self.assertFalse(result["citations"][0]["complete"])
            self.assertNotIn("private-value", json.dumps(result))
            self.assertNotIn(url, json.dumps(result))
            # Existing databases contain request URLs and no filter-label snapshots.
            with self.app.state.store.db() as conn:
                original = json.loads(conn.execute("SELECT result FROM history WHERE id=?", (result["id"],)).fetchone()[0])
                source = original["citations"][0]
                self.assertEqual(source["request"], fetched["request"])
                del source["filters"]
                source["request"] = "GET " + url + "?sku=x"
                conn.execute("UPDATE history SET result=? WHERE id=?", (json.dumps(original), result["id"]))
            replay = self.ok(self.reader.get(f"/api/history/{result['id']}"))
            self.assertEqual(replay["citations"][0]["filters"], [{"label": "sku", "value": "x"}])
            self.assertNotIn("private-value", json.dumps(replay))
            self.assertNotIn(url, json.dumps(replay))
            endpoint = f"/api/history/{result['id']}/sources/S1"
            for suffix in ("rows", "raw"):
                self.assertEqual(self.reader.get(endpoint + "/" + suffix).status_code, 403)
            self.assertEqual(self.admin.get(endpoint + "/raw").content, raw)
            # A clean row set can accompany restricted metadata elsewhere in the
            # original JSON. The two archived representations are checked independently.
            clean_rows = [{"count": 6}]
            with self.app.state.store.db() as conn:
                source["rows"] = clean_rows
                del source["rows_sha256"]
                conn.execute("UPDATE history SET result=? WHERE id=?", (json.dumps(original), result["id"]))
                conn.execute("UPDATE api_snapshots SET body=? WHERE history_id=?",
                             (json.dumps({"url": url, "records": clean_rows}).encode(), result["id"]))
            self.assertEqual(self.reader.get(endpoint + "/rows").json(), clean_rows)
            self.assertEqual(self.reader.get(endpoint + "/raw").status_code, 403)
            # A historical source may have an ID and timestamp but no saved rows.
            with self.app.state.store.db() as conn:
                original["citations"] = [{"id": "S1", "kind": "api"}]
                conn.execute("UPDATE history SET result=? WHERE id=?", (json.dumps(original), result["id"]))
            self.ok(self.reader.get(f"/api/history/{result['id']}"))
            self.assertEqual(self.reader.get(endpoint + "/rows").status_code, 404)
            self.assertEqual(self.reader.get("/reader.html").headers["x-frame-options"], "SAMEORIGIN")
            self.assertEqual(self.reader.get("/").headers["x-frame-options"], "DENY")

    def test_paging_literal_search_and_citation_focus(self):
        rows = ["Zero", "Calibration 90%_done", "TWO", "Calibration 90XYdone", "Last"]
        ident = self.upload("sections.txt", "\n\n".join(rows).encode())
        url = f"/api/documents/{ident}/preview"
        first = self.ok(self.reader.get(url, params={"limit": 2}))
        self.assertEqual(first["total"], 5)
        self.assertEqual([c["text"] for c in first["chunks"]], rows[:2])
        last = self.ok(self.reader.get(url, params={"offset": 1000, "limit": 2}))
        target = last["chunks"][0]["id"]
        self.assertEqual(last["offset"], 4)
        focus = self.ok(self.reader.get(url, params={"chunk_id": target, "limit": 2}))
        self.assertEqual((focus["offset"], focus["focus_chunk_id"]), (4, target))
        literal = self.ok(self.reader.get(url, params={"q": "90%_DONE", "chunk_id": target}))
        self.assertEqual((literal["total"], literal["focus_chunk_id"]), (1, None))
        self.assertEqual(literal["chunks"][0]["text"], rows[1])
        same_locator = self.ok(self.reader.get(url, params={"q": first["chunks"][0]["locator"]}))
        self.assertEqual(same_locator["chunks"][0]["id"], first["chunks"][0]["id"])
        empty = self.ok(self.reader.get(url, params={"q": "missing-term", "offset": 999}))
        self.assertEqual((empty["total"], empty["offset"], empty["chunks"]), (0, 0, []))
        self.assertEqual(self.reader.get(url, params={"offset": -1}).status_code, 422)
        other_id = self.upload("other.txt", b"Different document.")
        self.assertEqual(self.reader.get(f"/api/documents/{other_id}/preview",
                                        params={"chunk_id": target}).status_code, 404)

    def test_open_archived_document_by_original_answer_citation(self):
        old = self.upload("Neodymium.md", b"Neodymium registration requires an amber label.")
        answer = self.ok(self.reader.post("/api/query", json={"question": "What label does Neodymium registration require?"}))
        citation = answer["citations"][0]
        self.assertEqual(citation["document_id"], old)
        new = self.upload("Neodymium.md", b"Neodymium registration requires a green label.")
        self.assertNotEqual(new, old)
        replay = self.ok(self.reader.get(f"/api/history/{answer['id']}"))
        source = replay["citations"][0]
        page = self.ok(self.reader.get(f"/api/documents/{source['document_id']}/preview",
                                       params={"chunk_id": source["chunk_id"]}))
        self.assertFalse(page["document"]["active"])
        self.assertEqual(page["document"]["sha256"], citation["sha256"])
        self.assertEqual(page["focus_chunk_id"], source["chunk_id"])
        self.assertEqual(page["chunks"][0]["locator"], source["locator"])
        self.assertIn(source["excerpt"], page["chunks"][0]["text"])
        self.assertTrue(self.ok(self.reader.get(f"/api/documents/{new}/preview"))["document"]["active"])

    def test_pdf_inline_ranges_and_authenticated_frame_policy(self):
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=100)
        buf = io.BytesIO()
        writer.write(buf)
        data = buf.getvalue()
        ident = self.add_stored("预算.pdf", data)
        preview_url = f"/api/documents/{ident}/preview"
        view = self.ok(self.reader.get(preview_url))
        self.assertEqual(view["original_kind"], "pdf")
        info_url = f"/api/documents/{ident}/original-info"
        info = self.ok(self.reader.get(info_url, params={"chunk_id": view["chunks"][0]["id"]}))
        self.assertEqual((info["original_kind"], info["citation_locator"]), ("pdf", "第 1 页"))
        self.assertEqual(info["document"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertNotIn("chunks", info)
        self.assertEqual(self.reader.get(info_url, params={"chunk_id": 2**40}).status_code, 404)
        url = view["original_url"]
        original = self.reader.get(url)
        self.assertEqual(original.content, data)
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        self.assertEqual(original.headers["etag"], etag)
        self.assertEqual(original.headers["content-type"], "application/pdf")
        self.assertTrue(original.headers["content-disposition"].startswith("inline;"))
        self.assertEqual(original.headers["x-frame-options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", original.headers["content-security-policy"])
        self.assertEqual(self.reader.get(preview_url).headers["x-frame-options"], "DENY")
        head = self.reader.head(url)
        self.assertEqual((head.status_code, head.content, head.headers["content-length"]), (200, b"", str(len(data))))
        for requested, first, last in (("bytes=0-7", 0, 7), ("bytes=-4", len(data)-4, len(data)-1)):
            partial = self.reader.get(url, headers={"Range": requested, "If-Range": etag})
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, data[first:last+1])
            self.assertEqual(partial.headers["content-range"], f"bytes {first}-{last}/{len(data)}")
        stale = self.reader.get(url, headers={"Range": "bytes=0-7", "If-Range": '"previous-version"'})
        self.assertEqual((stale.status_code, stale.content), (200, data))
        outside = self.reader.get(url, headers={"Range": f"bytes={len(data)}-"})
        self.assertEqual((outside.status_code, outside.headers["content-range"]), (416, f"bytes */{len(data)}"))
        self.ok(self.reader.post("/api/logout"))
        for endpoint in (url, preview_url, info_url):
            self.assertEqual(self.reader.get(endpoint).status_code, 401)

    def test_images_and_untrusted_file_types(self):
        pixels = {
            "pixel.png": ("image/png", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR42mNgAAAAAgAB5Sfe/AAAAABJRU5ErkJggg=="),
            "pixel.jpeg": ("image/jpeg", "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AP7//2Q=="),
        }
        for filename, (mime, encoded) in pixels.items():
            data = base64.b64decode(encoded)
            ident = self.add_stored(filename, data)
            page = self.ok(self.reader.get(f"/api/documents/{ident}/preview"))
            self.assertEqual(page["original_kind"], "image")
            image = self.reader.get(page["original_url"])
            self.assertEqual((image.status_code, image.headers["content-type"], image.content), (200, mime, data))
        html = b"<p>Online content</p><script>hiddenContent()</script>"
        doc = self.upload("notice.html", html)
        fake = self.add_stored("bad.jpg", html)
        office = self.add_stored("layout.docx", b"original office bytes")
        self.assertEqual(self.ok(self.reader.get(f"/api/documents/{doc}/original-info"))["original_kind"], "text")
        for ident in (office, fake):
            self.assertIsNone(self.ok(self.reader.get(f"/api/documents/{ident}/original-info"))["original_kind"])
        for ident in (doc, fake):
            view = self.ok(self.reader.get(f"/api/documents/{ident}/preview"))
            self.assertIsNone(view["original_url"])
            self.assertNotIn("hiddenContent", view["chunks"][0]["text"])
            self.assertEqual(self.reader.get(f"/api/documents/{ident}/original").status_code, 415)
            download = self.reader.get(f"/api/documents/{ident}/download")
            self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))

    def test_admin_overview_and_scoped_search_across_pages(self):
        def ask(client, question):
            return self.ok(client.post("/api/query", json={"question": question}))["id"]
        own = ask(self.admin, "Budget ADMIN")
        first = ask(self.reader, "budget 90%_A")
        latest = ask(self.reader, "Budget elsewhere")
        def select(client, **params):
            return [r["id"] for r in self.ok(client.get("/api/history", params=params))]

        self.assertEqual(select(self.admin), [latest, first, own])
        self.assertEqual(select(self.admin, scope="mine"), [own])
        self.assertEqual(select(self.admin, scope="workspace", q="BUDGET", limit=1, offset=2), [own])
        self.assertEqual(select(self.reader), [latest, first])
        self.assertEqual(select(self.reader, scope="mine", q="%_a"), [first])
        self.assertEqual(select(self.reader, q="ADMIN"), [])
        self.assertEqual(self.reader.get("/api/history", params={"scope": "workspace"}).status_code, 403)
        self.assertEqual(self.reader.get("/api/overview").status_code, 403)
        self.assertEqual(self.ok(self.admin.get("/api/overview"))["queries"], 3)


if __name__ == "__main__":
    unittest.main()
