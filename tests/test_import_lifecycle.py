import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from main import create_app
from ragcore.import_schema import PARSER_VERSION
from ragcore.storage import Store


class ImportLifecycleTests(unittest.TestCase):
    def test_migration_retains_ids_and_old_chunks_and_rebuild_is_atomic(self):
        with tempfile.TemporaryDirectory() as path:
            store = Store(path)
            data = b"field,value\na,42"
            with closing(sqlite3.connect(store.path)) as conn, conn:
                conn.executescript("""
                    CREATE TABLE documents(id INTEGER PRIMARY KEY, filename TEXT,size INTEGER,sha256 TEXT,
                        chunk_count INTEGER,created_at TEXT,active INTEGER DEFAULT 1,content BLOB,UNIQUE(filename,sha256));
                    CREATE TABLE chunks(id INTEGER PRIMARY KEY,document_id INTEGER REFERENCES documents(id),locator TEXT,text TEXT);
                """)
                conn.execute("INSERT INTO documents VALUES(7,'stock.csv',?,?,1,'old',1,?)",
                             (len(data), hashlib.sha256(data).hexdigest(), data))
                conn.execute("INSERT INTO chunks VALUES(19,7,'old row','legacy text')")
            store.initialize(seed=False)
            with patch("ragcore.storage.parse_upload_report",side_effect=ValueError("bad parse")), self.assertRaises(ValueError):
                store.reindex(7, "test")
            with store.db() as conn:
                self.assertEqual(conn.execute("SELECT id FROM documents WHERE active=1").fetchone()[0], 7)
            result = store.reindex(7, "test")
            self.assertEqual(result["parser_version"], PARSER_VERSION)
            self.assertNotEqual(result["id"], 7)
            self.assertTrue(store.reindex(result["id"], "test")["duplicate"])
            with store.db() as conn:
                self.assertEqual(tuple(conn.execute("SELECT active,content FROM documents WHERE id=7").fetchone()), (0,data))
                self.assertEqual(conn.execute("SELECT text FROM chunks WHERE id=19").fetchone()[0], "legacy text")
                self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
            self.assertNotIn(19, [c["id"] for c in store.candidates("stock")])

    def test_inspection_options_source_identity_and_same_bytes(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ,{"RAG_ADMIN_PASSWORD":"fixture-admin",
                                                                  "RAG_COOKIE_SECURE":"0"}):
            with TestClient(create_app(path,seed=False),base_url="http://127.0.0.1") as client:
                client.post("/api/login",json={"password":"fixture-admin"}).raise_for_status()
                def upload(route, source, options=None):
                    body = {"source_key":source,"options":json.dumps(options or {})}
                    return client.post(route, data=body, files={"files":("stock.csv",b"heading\nname,quantity\nA,42")})
                preview = upload("/api/documents/inspect","east",{"header_rows":[2]}).json()["imported"][0]
                self.assertTrue(preview["sample"])
                self.assertEqual(client.get("/api/documents").json(), [])
                first = upload("/api/documents","east").json()["imported"][0]
                other = upload("/api/documents","west").json()["imported"][0]
                self.assertNotEqual(first["id"], other["id"])
                self.assertEqual(set(d["source_key"] for d in client.get("/api/documents").json()), {"east","west"})
                rebuilt = client.post(f"/api/documents/{first['id']}/reindex",json={"header_rows":[2]}).json()
                self.assertNotEqual(first["id"], rebuilt["id"])
                old = client.get(f"/api/documents/{first['id']}/preview").json()
                self.assertFalse(old["document"]["active"])
                self.assertEqual(client.post(f"/api/documents/{first['id']}/reindex").status_code,409)
                self.assertEqual(upload("/api/documents","east",{"header_rows":[2],"headerless":True}).status_code,422)

    def test_version_restore_preserves_both_indexes_and_requires_current_source(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {"RAG_ADMIN_PASSWORD":"fixture-admin",
                                                                           "RAG_COOKIE_SECURE":"0"}):
            with TestClient(create_app(path, seed=False), base_url="http://127.0.0.1") as client:
                client.post("/api/login",json={"password":"fixture-admin"}).raise_for_status()
                def save(content, source):
                    return client.post("/api/documents",files={"files":("log.txt", content)},
                                       data={"source_key":source}).json()["imported"][0]["id"]
                old = save(b"old","A")
                new = save(b"new","A")
                other = save(b"other","B")
                base = "/api/documents/"
                versions = client.get(f"{base}{new}/versions").raise_for_status().json()
                self.assertEqual([r["id"] for r in versions],[new,old])
                self.assertEqual(client.post(f"{base}{new}/restore/{other}").status_code,404)
                with patch("ragcore.storage.parse_upload_report", side_effect=AssertionError("unexpected reparse")):
                    result = client.post(f"{base}{new}/restore/{old}").raise_for_status().json()
                self.assertEqual(result["id"], old)
                self.assertEqual(client.post(f"{base}{new}/restore/{new}").status_code,409)
                self.assertEqual(client.get(f"{base}{new}/preview").json()["chunks"][0]["text"],"new")
                self.assertEqual(client.get(f"{base}{old}/preview").json()["chunks"][0]["text"],"old")
                self.assertEqual({r["id"] for r in client.get("/api/documents").json()},{old,other})

    def test_two_instances_share_one_read_and_json_replays_stream_result(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ,{
            "RAG_ADMIN_PASSWORD":"fixture-admin","RAG_COOKIE_SECURE":"0","RAG_LLM_BASE_URL":"","RAG_EMBED_BASE_URL":""}):
            with TestClient(create_app(path,True),base_url="http://127.0.0.1") as a, \
                    TestClient(create_app(path,True),base_url="http://127.0.0.1") as b:
                a.post("/api/login",json={"password":"fixture-admin"}).raise_for_status()
                b.cookies.update(a.cookies)
                parent = a.post("/api/query",json={"mode":"documents","question":"报销材料有哪些？"}).json()["id"]
                payload = {"question":"查询项目预算","mode":"data","request_id":str(uuid4()),"parent_id":parent}
                arrived = threading.Event()
                release = threading.Event()
                def read(_):
                    arrived.set()
                    self.assertTrue(release.wait(8))
                    return [{"项目名称":"甲","年度":2026,"预算金额（万元）":23}]
                with patch("ragcore.connectors._demo_budget",side_effect=read) as upstream, ThreadPoolExecutor(2) as pool:
                    one = pool.submit(a.post,"/api/query/stream",json=payload)
                    self.assertTrue(arrived.wait(8))
                    two = pool.submit(b.post,"/api/query",json=payload)
                    release.set()
                    sse = one.result().raise_for_status().text
                    result = two.result().raise_for_status().json()
                done = json.loads(sse.strip().splitlines()[-1].removeprefix("data: "))["result"]
                self.assertEqual((done["id"],done["answer"],done["parent_id"]), (result["id"],result["answer"],parent))
                self.assertEqual(upstream.call_count,1)
