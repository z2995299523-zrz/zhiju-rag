"""SQLite persistence. The original bytes, located extracts and index commit together."""
import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .accounts import ensure_schema
from .import_schema import DOCUMENT_SCHEMA, PARSER_VERSION, ImportOptions, migrate_imports
from .ingest_worker import UploadBusyError, parse_upload_report
from .ocr import available as ocr_available
from .retrieval import index_terms


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


DOC_FIELDS = "id,filename,size,sha256,chunk_count,created_at,source_key,parser_version,warning_count"
CHUNK_FIELDS = "c.id,c.document_id,c.locator,c.text,c.context,c.metadata,d.filename,d.sha256,d.source_key"


class ImportParseError(ValueError):
    def __init__(self, message, elapsed_ms, code="invalid_document"):
        super().__init__(message)
        self.elapsed_ms = elapsed_ms
        self.code = code


class StaleImport(ValueError):
    """Another importer replaced or deactivated the selected revision."""


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
            conn.executescript("CREATE TABLE IF NOT EXISTS documents " + DOCUMENT_SCHEMA + ";")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT);
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
                CREATE TABLE IF NOT EXISTS query_traces (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, mode TEXT NOT NULL,
                    request_id TEXT, history_id INTEGER REFERENCES history(id), outcome TEXT NOT NULL,
                    duration_ms REAL NOT NULL, stages TEXT NOT NULL
                );
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
            if "source_key" not in {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}:
                conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            migrate_imports(conn)
            ensure_schema(conn)
            if "parent_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(history)")}:
                conn.execute("ALTER TABLE history ADD COLUMN parent_id INTEGER REFERENCES history(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_parent ON history(parent_id,owner)")
            if conn.execute("SELECT 1 FROM settings WHERE name='initialized'").fetchone():
                return
            from .seed import initial_profile, seed_workspace
            seed_workspace(self, conn, initial_profile(seed))
            conn.execute("INSERT INTO settings VALUES ('initialized','1')")

    def insert_document(self, conn, filename, content, chunks, *, actor="system",
                        source_key="", recipe='{"options":{}}', report=None, expected_id=None):
        digest = hashlib.sha256(content).hexdigest()
        if expected_id is not None and not conn.execute(
            "SELECT 1 FROM documents WHERE id=? AND active=1 AND filename=? AND source_key=?",
            (expected_id, filename, source_key)
        ).fetchone():
            raise StaleImport("资料已更新或停用，请刷新后选择当前版本")
        existing = conn.execute("""SELECT id,active FROM documents WHERE source_key=? AND filename=?
                                AND sha256=? AND parser_version=? AND recipe=?""",
                                (source_key, filename, digest, PARSER_VERSION, recipe)).fetchone()
        duplicate = existing is not None and bool(existing["active"])
        if not duplicate:
            conn.execute("UPDATE documents SET active=0 WHERE source_key=? AND filename=? AND active=1", (source_key, filename))
        if existing:
            doc_id = existing["id"]
            conn.execute("UPDATE documents SET active=1 WHERE id=?", (doc_id,))
        else:
            report = report or {}
            cursor = conn.execute("""INSERT INTO documents(filename,size,sha256,chunk_count,created_at,content,
                                  source_key,parser_version,recipe,report,warning_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                  (filename, len(content), digest, len(chunks), now(), content, source_key, PARSER_VERSION,
                                   recipe, pack(report), len(report.get("warnings", []))))
            doc_id = cursor.lastrowid
            for chunk in chunks:
                context = chunk.get("context", "")
                key = conn.execute("INSERT INTO chunks(document_id,locator,text,context,metadata) VALUES (?,?,?,?,?)",
                                   (doc_id, chunk["locator"], chunk["text"], context, pack(chunk.get("metadata", {})))).lastrowid
                tokens = " ".join(index_terms(" ".join((source_key, filename, chunk["locator"], context, chunk["text"]))))
                conn.execute("INSERT INTO chunk_index(rowid,tokens) VALUES (?,?)", (key, tokens))
        if not duplicate:
            self.audit(conn, actor, "document.import", str(doc_id))
        result = dict(conn.execute(f"SELECT {DOC_FIELDS},report FROM documents WHERE id=?", (doc_id,)).fetchone())
        result.update(json.loads(result.pop("report")))
        return {**result, "duplicate": duplicate}

    def ingest(self, filename, content, actor, *, source_key="", options=None, expected_id=None):
        source_key = source_key.strip()
        if len(source_key) > 128 or any(ord(ch) < 32 for ch in source_key):
            raise ValueError("资料来源名称过长或包含控制字符")
        settings = ImportOptions.model_validate(options or {}).model_dump(exclude_defaults=True)
        recipe = dict(options=settings)
        if Path(filename).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            recipe["ocr"] = ocr_available()
        started = time.perf_counter()
        try:
            report = parse_upload_report(filename, content, settings)
        except ValueError as exc:
            raise ImportParseError(str(exc), round((time.perf_counter() - started) * 1000, 1),
                                   "busy" if isinstance(exc, UploadBusyError) else "invalid_document") from exc
        parse_ms = round((time.perf_counter() - started) * 1000, 1)
        chunks = report.pop("chunks")
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = self.insert_document(conn, filename, content, chunks, actor=actor, source_key=source_key,
                                         recipe=pack(recipe), report=report, expected_id=expected_id)
        return {**saved, "parse_ms": parse_ms}

    def reindex(self, doc_id, actor, options=None):
        with self.db() as conn:
            row = conn.execute("SELECT filename,source_key,content,recipe FROM documents WHERE id=? AND active=1",
                               (doc_id,)).fetchone()
        if row is None:
            raise StaleImport("资料不存在或已停用")
        return self.ingest(row["filename"], row["content"], actor, source_key=row["source_key"],
                           options=json.loads(row["recipe"]).get("options", {}) if options is None else options,
                           expected_id=doc_id)

    def document_terms(self):
        with self.db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE name='document_terms'").fetchone()
        return json.loads(row[0]) if row else {}

    def candidates(self, question, cap=180, *, with_method=False):
        with self.db() as conn:
            count = conn.execute("SELECT coalesce(sum(chunk_count),0) FROM documents WHERE active=1").fetchone()[0]
            if count <= cap:
                method = "all"
                result = conn.execute(f"""SELECT {CHUNK_FIELDS} FROM chunks c JOIN documents d
                                      ON d.id=c.document_id WHERE d.active=1 ORDER BY c.id""")
            else:
                method = "fts5"
                query = " OR ".join('"' + t.replace('"', '""') + '"' for t in list(dict.fromkeys(index_terms(question)))[:160])
                if not query:
                    return ([], method) if with_method else []
                result = conn.execute(f"""SELECT {CHUNK_FIELDS} FROM chunk_index
                    JOIN chunks c ON c.id=chunk_index.rowid JOIN documents d ON d.id=c.document_id
                    WHERE chunk_index MATCH ? AND d.active=1 ORDER BY rank LIMIT ?""", (query, cap))
            chunks = [dict(row) for row in result]
            for chunk in chunks:
                chunk["metadata"] = json.loads(chunk["metadata"])
            return (chunks, method) if with_method else chunks

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
        """Serialize the same request across processes that share this SQLite workspace."""
        if request_id is None:
            yield
            return
        # A bounded set of files avoids accumulating one lock per turn. The OS
        # releases the lock on disconnect, orderly shutdown or process death.
        import errno
        import os
        lock_dir = self.path.parent / ".query-locks"
        lock_dir.mkdir(exist_ok=True)
        slot = hashlib.sha256(pack([owner, request_id]).encode()).hexdigest()[:3]
        with (lock_dir / slot).open("a+b") as handle:
            if os.name == "nt":
                import msvcrt
                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    await asyncio.sleep(0.08)
            yield

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
