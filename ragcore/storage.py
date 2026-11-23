"""SQLite persistence. The original bytes, located extracts and index commit together."""
import asyncio
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import Future
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .accounts import ensure_schema
from .ingest_worker import parse_upload
from .retrieval import index_terms


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


DOC_FIELDS = "id,filename,size,sha256,chunk_count,created_at"
CHUNK_FIELDS = "c.id,c.document_id,c.locator,c.text,d.filename,d.sha256"


class RequestConflict(ValueError):
    pass


def validate_parent(store, owner, parent_id):
    if parent_id is not None:
        with store.db() as conn:
            if not conn.execute("SELECT 1 FROM history WHERE id=? AND owner=?", (parent_id, owner)).fetchone():
                raise KeyError("历史会话不存在")


class Store:
    def __init__(self, directory):
        self.path = Path(directory).resolve() / "workspace.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._running = {}
        self._running_lock = threading.Lock()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self, seed=None):
        with self.db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, chunk_count INTEGER NOT NULL, created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, content BLOB NOT NULL, UNIQUE(filename,sha256)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
                    locator TEXT NOT NULL, text TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_index USING fts5(tokens);
                CREATE TABLE IF NOT EXISTS connectors (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
                    created_at TEXT NOT NULL, question TEXT NOT NULL, intent TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_history_owner ON history(owner,id);
                CREATE TABLE IF NOT EXISTS query_requests (
                    owner TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    history_id INTEGER UNIQUE REFERENCES history(id), PRIMARY KEY(owner,request_id)
                );
                CREATE TABLE IF NOT EXISTS api_snapshots (
                    history_id INTEGER NOT NULL REFERENCES history(id), source_id TEXT NOT NULL,
                    digest TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY(history_id,source_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, role TEXT NOT NULL,
                    expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (peer TEXT PRIMARY KEY, failures INTEGER, since INTEGER);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, created_at TEXT, actor TEXT,
                    action TEXT, resource TEXT);
            """)
            conn.execute("BEGIN IMMEDIATE")
            ensure_schema(conn)
            if "parent_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(history)")}:
                conn.execute("ALTER TABLE history ADD COLUMN parent_id INTEGER REFERENCES history(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_parent ON history(parent_id,owner)")
            if conn.execute("SELECT 1 FROM settings WHERE name='initialized'").fetchone():
                return
            from .seed import initial_profile, seed_workspace
            seed_workspace(self, conn, initial_profile(seed))
            conn.execute("INSERT INTO settings VALUES ('initialized','1')")

    def insert_document(self, conn, filename, content, chunks, *, actor="system"):
        digest = hashlib.sha256(content).hexdigest()
        existing = conn.execute("SELECT id,active FROM documents WHERE filename=? AND sha256=?",
                                (filename, digest)).fetchone()
        duplicate = existing is not None and bool(existing["active"])
        if not duplicate:
            conn.execute("UPDATE documents SET active=0 WHERE filename=? AND active=1", (filename,))
        if existing:
            doc_id = existing["id"]
            conn.execute("UPDATE documents SET active=1 WHERE id=?", (doc_id,))
        else:
            cursor = conn.execute("""INSERT INTO documents(filename,size,sha256,chunk_count,created_at,content)
                                  VALUES (?,?,?,?,?,?)""", (filename, len(content), digest, len(chunks), now(), content))
            doc_id = cursor.lastrowid
            for chunk in chunks:
                key = conn.execute("INSERT INTO chunks(document_id,locator,text) VALUES (?,?,?)",
                                   (doc_id, chunk["locator"], chunk["text"])).lastrowid
                tokens = " ".join(index_terms(filename + " " + chunk["locator"] + " " + chunk["text"]))
                conn.execute("INSERT INTO chunk_index(rowid,tokens) VALUES (?,?)", (key, tokens))
        if not duplicate:
            self.audit(conn, actor, "document.import", str(doc_id))
        result = dict(conn.execute(f"SELECT {DOC_FIELDS} FROM documents WHERE id=?", (doc_id,)).fetchone())
        return {**result, "duplicate": duplicate}

    def ingest(self, filename, content, actor):
        chunks = parse_upload(filename, content)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.insert_document(conn, filename, content, chunks, actor=actor)

    def candidates(self, question, cap=180):
        with self.db() as conn:
            count = conn.execute("SELECT coalesce(sum(chunk_count),0) FROM documents WHERE active=1").fetchone()[0]
            if count <= cap:
                result = conn.execute(f"""SELECT {CHUNK_FIELDS} FROM chunks c JOIN documents d
                                      ON d.id=c.document_id WHERE d.active=1 ORDER BY c.id""")
            else:
                query = " OR ".join('"' + t.replace('"', '""') + '"' for t in list(dict.fromkeys(index_terms(question)))[:160])
                if not query:
                    return []
                result = conn.execute(f"""SELECT {CHUNK_FIELDS} FROM chunk_index
                    JOIN chunks c ON c.id=chunk_index.rowid JOIN documents d ON d.id=c.document_id
                    WHERE chunk_index MATCH ? AND d.active=1 ORDER BY rank LIMIT ?""", (query, cap))
            return [dict(row) for row in result]

    def connectors(self):
        with self.db() as conn:
            return [{**json.loads(row["config"]), "id": row["id"]} for row in
                    conn.execute("SELECT id, config FROM connectors ORDER BY id")]

    def reserve_query(self, owner, request_id, fingerprint):
        with self.db() as conn:
            conn.execute("""INSERT INTO query_requests(owner,request_id,fingerprint) VALUES (?,?,?)
                            ON CONFLICT(owner,request_id) DO NOTHING""", (owner, request_id, fingerprint))
            previous = conn.execute("SELECT fingerprint FROM query_requests WHERE owner=? AND request_id=?",
                                    (owner, request_id)).fetchone()[0]
            if previous != fingerprint:
                raise RequestConflict("该请求编号已用于不同的问题或条件，请新建问题。")

    @asynccontextmanager
    async def query_turn(self, owner, request_id):
        """Let simultaneous retries on this server share a single retrieval."""
        if request_id is None:
            yield
            return
        key = (owner, request_id)
        while True:
            with self._running_lock:
                running = self._running.get(key)
                if running is None:
                    self._running[key] = Future()
                    break
            await asyncio.shield(asyncio.wrap_future(running))
        try:
            yield
        finally:
            with self._running_lock:
                self._running.pop(key).set_result(None)

    @staticmethod
    def completed_query(conn, owner, request_id):
        row = conn.execute("""SELECT h.id,h.result FROM query_requests r JOIN history h ON h.id=r.history_id
                              WHERE r.owner=? AND r.request_id=?""", (owner, request_id)).fetchone()
        return {**json.loads(row["result"]), "id": row["id"]} if row else None

    def record(self, owner, result, snapshots=None, *, cancelled=None, request_id=None):
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("查询已取消")
            if request_id is not None:
                existing = self.completed_query(conn, owner, request_id)
                if existing:
                    return existing
            result["created_at"] = now()
            result["id"] = conn.execute("""INSERT INTO history(owner,created_at,question,intent,status,result,parent_id)
                VALUES (?,?,?,?,?,?,?)""", (owner, result["created_at"], result["question"], result["intent"],
                                           result["status"], pack(result), result.get("parent_id"))).lastrowid
            for sid, digest, body in snapshots or []:
                conn.execute("INSERT INTO api_snapshots VALUES (?,?,?,?)", (result["id"], sid, digest, body))
            if request_id is not None:
                linked = conn.execute("UPDATE query_requests SET history_id=? WHERE owner=? AND request_id=?",
                                      (result["id"], owner, request_id))
                if linked.rowcount != 1:
                    raise ValueError("未登记此回合的请求编号")
            if cancelled is not None and cancelled.is_set():
                raise InterruptedError("查询已取消")
        return result

    @staticmethod
    def audit(conn, actor, action, resource):
        conn.execute("INSERT INTO audit(created_at,actor,action,resource) VALUES (?,?,?,?)",
                     (now(), actor, action, resource))
