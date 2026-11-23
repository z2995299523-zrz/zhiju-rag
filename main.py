"""HTTP entry point for the grounded enterprise knowledge workspace."""
import hashlib
import json
import logging
import math
import os
import sqlite3
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ragcore import __version__, access, model_config, models
from ragcore.connectors import ConnectorError, execute, validate_config
from ragcore.documents import MAX_FILE_BYTES, SUPPORTED_EXTENSIONS
from ragcore.import_schema import ImportOptions, PARSER_VERSION
from ragcore.ingest_worker import UploadBusyError, parse_upload_report
from ragcore.engine import display_fields, query_events, query_examples, workspace_answer
from ragcore.model_secrets import CredentialError
from ragcore.query_trace import recent_traces
from ragcore.ocr import IMAGE_EXTENSIONS, available as ocr_available
from ragcore.preview import router as preview_router
from ragcore.retrieval_check import router as retrieval_check_router
from ragcore.storage import DOC_FIELDS, ImportParseError, RequestConflict, StaleImport, Store, pack, validate_parent
from ragcore.workspace_id import workspace_id


ROOT = Path(__file__).resolve().parent


class QueryStreamResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        # ASGI 2.4 normally notices disconnects only on send. Listen while the
        # provider is silent as well, so closing the tab cancels its open stream.
        async with anyio.create_task_group() as group:
            async def watch_disconnect():
                await self.listen_for_disconnect(receive)
                group.cancel_scope.cancel()

            group.start_soon(watch_disconnect)
            try:
                async with aclosing(self.body_iterator):
                    await self.stream_response(send)
            except OSError:
                pass  # Client closed the connection during send.
            finally:
                group.cancel_scope.cancel()


class Password(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    username: str | None = Field(default=None, max_length=64)


class Parameters(BaseModel):
    params: dict = Field(default_factory=dict, max_length=24)

    @field_validator("params")
    @classmethod
    def business_values(cls, values):
        for key, value in values.items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > 64:
                raise ValueError("参数名无效")
            if value is not None and type(value) not in (str, int, float):
                raise ValueError("参数值须为文本、数字或空值")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("数字参数须为有限值")
            if isinstance(value, str):
                if len(value) > 256:
                    raise ValueError("单个参数值超过 256 个字符")
                value.encode("utf-8")
        return values


class Question(Parameters):
    request_id: UUID | None = None
    parent_id: int | None = Field(default=None, ge=1, le=2**63 - 1)
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["auto", "documents", "data", "mixed"] = "auto"
    connector_id: int | None = Field(default=None, gt=0)

    @field_validator("question")
    @classmethod
    def text_is_valid(cls, question):
        question.encode("utf-8")
        return question


def create_app(data_dir=None, seed=None):
    @asynccontextmanager
    async def lifespan(instance):
        store = Store(data_dir or os.getenv("RAG_DATA_DIR") or ROOT / "var")
        store.initialize(seed)
        instance.state.store = store
        instance.state.workspace_id = workspace_id(store.path.parent)
        yield

    app = FastAPI(title="企业知识检索工作台", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=lifespan)
    app.middleware("http")(access.guard)
    app.include_router(preview_router)
    app.include_router(retrieval_check_router)

    @app.exception_handler(ConnectorError)
    async def connector_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        fields = [str(e["loc"][-1]) for e in exc.errors() if e.get("loc")]
        return JSONResponse({"detail": "请求字段格式有误，请检查 " + "、".join(dict.fromkeys(fields))[:220]}, status_code=422)

    @app.get("/healthz")
    def health(request: Request):
        with request.app.state.store.db() as conn:
            conn.execute("SELECT 1 FROM settings").fetchone()
        return {"status": "ok", "workspace_id": request.app.state.workspace_id}

    @app.get("/api/session")
    def session(request: Request):
        actor = access.account(request)
        return {"authenticated": bool(actor["role"]), "role": actor["role"], "auth_required": actor["auth_required"],
                "username": actor.get("username")}

    @app.post("/api/login")
    def login(payload: Password, request: Request):
        return access.login(request, payload.password, payload.username)

    @app.post("/api/logout")
    def logout(request: Request):
        with request.app.state.store.db() as conn:
            digest = hashlib.sha256(request.cookies.get("rag_session", "").encode()).hexdigest()
            conn.execute("DELETE FROM sessions WHERE digest=?", (digest,))
        response = JSONResponse({"ok": True})
        response.delete_cookie("rag_session", path="/")
        return response

    @app.get("/api/overview")
    def overview(request: Request, actor=Depends(access.admin)):
        with request.app.state.store.db() as conn:
            docs, chunks = conn.execute("SELECT count(*),coalesce(sum(chunk_count),0) FROM documents WHERE active=1").fetchone()
            return {"documents": docs, "chunks": chunks,
                    "connectors": conn.execute("SELECT count(*) FROM connectors").fetchone()[0],
                    "queries": conn.execute("SELECT count(*) FROM history").fetchone()[0]}

    @app.get("/api/admin/query-traces")
    def query_traces(request: Request, actor=Depends(access.admin), limit: int = Query(10, ge=1, le=50),
                     before: int | None = Query(None, ge=1, le=2**63 - 1)):
        return recent_traces(request.app.state.store, before, limit)

    @app.get("/api/admin/models")
    def model_settings(request: Request, actor=Depends(access.admin)):
        return model_config.overview(request.app.state.store)

    @app.put("/api/admin/models/{kind}")
    def configure_model(kind: model_config.Kind, payload: model_config.ModelDraft, request: Request,
                        actor=Depends(access.admin)):
        return model_config.save(request.app.state.store, kind, payload, access.audit_actor(actor))

    @app.post("/api/admin/models/{kind}/test")
    def probe_model(kind: model_config.Kind, request: Request, payload: model_config.ModelDraft | None = None,
                    concurrent_batches: bool = False, actor=Depends(access.admin)):
        if concurrent_batches and kind != "embedding":
            raise model_config.ConfigError("并发分批测试仅适用于向量模型。")
        settings = model_config.probe_settings(request.app.state.store, kind, payload)
        return models.probe(kind, settings, concurrent_batches=concurrent_batches)

    @app.exception_handler(model_config.ConfigError)
    @app.exception_handler(CredentialError)
    async def model_configuration_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/api/documents")
    def documents(request: Request, actor=Depends(access.reader), limit: int | None = Query(None, ge=1, le=1000)):
        with request.app.state.store.db() as conn:
            return [dict(r) for r in conn.execute(f"SELECT {DOC_FIELDS} FROM documents WHERE active=1 ORDER BY id DESC LIMIT ?",
                                                   (limit if limit is not None else -1,))]

    @app.get("/api/formats")
    def formats(actor=Depends(access.reader)):
        ocr_enabled = ocr_available()
        available_formats = SUPPORTED_EXTENSIONS if ocr_enabled else SUPPORTED_EXTENSIONS - IMAGE_EXTENSIONS
        return {"extensions": sorted(available_formats), "max_file_bytes": MAX_FILE_BYTES,
                "max_files": 8, "ocr_enabled": ocr_enabled, "parser_version": PARSER_VERSION}

    @app.post("/api/documents/inspect")
    @app.post("/api/documents")
    async def upload(request: Request, actor=Depends(access.admin)):
        imported, errors = [], []
        inspect = request.url.path.endswith("/inspect")
        async with request.form(max_files=8, max_fields=2) as form:
            try:
                options = ImportOptions.model_validate_json(form.get("options", "{}")).model_dump(exclude_defaults=True)
            except (ValueError, TypeError):
                raise HTTPException(422, "解析设置无效，请检查表头行和隐藏内容选项") from None
            source_key = form.get("source_key", "")
            if not isinstance(source_key, str) or len(source_key) > 128 or any(ord(c) < 32 for c in source_key):
                raise HTTPException(422, "资料来源名称无效")
            files = form.getlist("files")
            if not files or any(not isinstance(f, UploadFile) for f in files):
                raise HTTPException(422, "请用 files 字段上传至少一个文件")
            for file in files:
                filename = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
                try:
                    if not filename or len(filename) > 200 or any(ord(c) < 32 for c in filename):
                        raise ValueError("文件名无效或过长")
                    if file.size is not None and file.size > MAX_FILE_BYTES:
                        raise ValueError("单文件超过 20 MiB")
                    content = await file.read(MAX_FILE_BYTES + 1)
                    if len(content) > MAX_FILE_BYTES:
                        raise ValueError("单文件超过 20 MiB")
                    if inspect:
                        result = await run_in_threadpool(parse_upload_report, filename, content, options)
                        chunks = result.pop("chunks")
                        result.update(filename=filename, chunk_count=len(chunks), sample=chunks[:6])
                    else:
                        result = await run_in_threadpool(request.app.state.store.ingest, filename, content,
                                                         access.audit_actor(actor), source_key=source_key, options=options)
                    imported.append(result)
                except ValueError as exc:
                    error = {"filename": filename, "error": str(exc), "message": str(exc)}
                    if isinstance(exc, ImportParseError):
                        error["parse_ms"] = exc.elapsed_ms
                    code = getattr(exc, "code", "busy" if isinstance(exc, UploadBusyError) else "invalid_document")
                    error.update(code=code, retryable=code == "busy")
                    errors.append(error)
        return {"imported": imported, "errors": errors}

    @app.post("/api/documents/{doc_id}/reindex")
    def reindex(doc_id: int, request: Request, options: ImportOptions | None = None, actor=Depends(access.admin)):
        try:
            return request.app.state.store.reindex(doc_id, access.audit_actor(actor),
                                                  options.model_dump(exclude_defaults=True) if options else None)
        except StaleImport as exc:
            raise HTTPException(409, str(exc)) from exc
        except ImportParseError as exc:
            raise HTTPException(503 if exc.code == "busy" else 422, str(exc)) from exc

    @app.get("/api/documents/{doc_id}/versions")
    def document_versions(doc_id: int, request: Request, actor=Depends(access.admin)):
        with request.app.state.store.db() as conn:
            source = conn.execute("SELECT source_key,filename FROM documents WHERE id=? AND active=1",
                                  (doc_id,)).fetchone()
            if source is None:
                raise HTTPException(409, "资料已更新或停用，请刷新后选择当前版本")
            rows = conn.execute(f"SELECT {DOC_FIELDS},active FROM documents WHERE source_key=? AND filename=? ORDER BY id DESC",
                                tuple(source)).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/documents/{doc_id}/restore/{version_id}")
    def restore_document(doc_id: int, version_id: int, request: Request, actor=Depends(access.admin)):
        with request.app.state.store.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute("SELECT source_key,filename FROM documents WHERE id=? AND active=1",
                                  (doc_id,)).fetchone()
            if source is None:
                raise HTTPException(409, "资料已更新或停用，请刷新后选择当前版本")
            version = conn.execute(f"SELECT {DOC_FIELDS} FROM documents WHERE id=? AND source_key=? AND filename=?",
                                   (version_id, *source)).fetchone()
            if version is None:
                raise HTTPException(404, "未找到该资料的指定版本")
            if version_id != doc_id:
                conn.execute("UPDATE documents SET active=0 WHERE id=?", (doc_id,))
                conn.execute("UPDATE documents SET active=1 WHERE id=?", (version_id,))
                Store.audit(conn, access.audit_actor(actor), "document.restore", f"{doc_id}/{version_id}")
            return dict(version)

    @app.delete("/api/documents/{doc_id}")
    def remove_document(doc_id: int, request: Request, actor=Depends(access.admin)):
        with request.app.state.store.db() as conn:
            found = conn.execute("UPDATE documents SET active=0 WHERE id=? AND active=1", (doc_id,))
            if not found.rowcount:
                raise HTTPException(404, "文件不存在或已移除")
            Store.audit(conn, access.audit_actor(actor), "document.remove", str(doc_id))
        return {"ok": True}

    @app.get("/api/documents/{doc_id}/download")
    def download(doc_id: int, request: Request, actor=Depends(access.reader)):
        with request.app.state.store.db() as conn:
            row = conn.execute("SELECT filename,content,sha256 FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "原始文件不存在")
        return Response(bytes(row["content"]), media_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["filename"], safe=""),
                                 "ETag": '"' + row["sha256"] + '"'})

    @app.get("/api/connectors")
    def connectors(request: Request, actor=Depends(access.reader)):
        configs = request.app.state.store.connectors()
        if actor["role"] != "admin":
            return [display_fields({k: c[k] for k in ("id", "name", "description", "parameters")}) for c in configs]
        return configs

    def save_config(request, config, ident, actor):
        normalized = validate_config(config)
        try:
            with request.app.state.store.db() as conn:
                values = normalized["name"], pack(normalized)
                if ident is None:
                    ident = conn.execute("INSERT INTO connectors(name,config) VALUES (?,?)", values).lastrowid
                elif not conn.execute("UPDATE connectors SET name=?,config=? WHERE id=?", (*values, ident)).rowcount:
                    raise HTTPException(404, "数据接口不存在")
                Store.audit(conn, access.audit_actor(actor), "connector.save", str(ident))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "接口名称已存在") from None
        return {**normalized, "id": ident}

    @app.post("/api/connectors")
    def add_connector(config: dict, request: Request, actor=Depends(access.admin)):
        return save_config(request, config, None, actor)

    @app.put("/api/connectors/{ident}")
    def edit_connector(ident: int, config: dict, request: Request, actor=Depends(access.admin)):
        return save_config(request, config, ident, actor)

    @app.delete("/api/connectors/{ident}")
    def delete_connector(ident: int, request: Request, actor=Depends(access.admin)):
        with request.app.state.store.db() as conn:
            if not conn.execute("DELETE FROM connectors WHERE id=?", (ident,)).rowcount:
                raise HTTPException(404, "数据接口不存在")
            Store.audit(conn, access.audit_actor(actor), "connector.remove", str(ident))
        return {"ok": True}

    @app.post("/api/connectors/{ident}/test")
    def test_connector(ident: int, payload: Parameters, request: Request, actor=Depends(access.admin)):
        config = next((c for c in request.app.state.store.connectors() if c["id"] == ident), None)
        if not config:
            raise HTTPException(404, "数据接口不存在")
        return execute(config, payload.params)

    @app.get("/api/query/examples")
    def examples(request: Request, actor=Depends(access.reader)):
        return query_examples(request.app.state.store)

    def prepare_turn(payload, request, actor, *, stream_model):
        question = payload.question.strip()
        if not question:
            raise HTTPException(422, "请输入问题")
        store = request.app.state.store
        owner = actor["owner"]
        request_id = str(payload.request_id) if payload.request_id else None
        try:
            validate_parent(store, owner, payload.parent_id)
            if request_id:
                identity = [question, payload.mode, payload.connector_id, sorted(payload.params.items()), payload.parent_id]
                store.reserve_query(owner, request_id, hashlib.sha256(pack(identity).encode()).hexdigest())
        except KeyError:
            raise HTTPException(404, "历史会话不存在") from None
        except RequestConflict as exc:
            raise HTTPException(409, str(exc)) from None

        async def execute_turn():
            async with store.query_turn(owner, request_id):
                if request_id:
                    with store.db() as conn:
                        archived = store.completed_query(conn, owner, request_id)
                    if archived:
                        yield "done", {"result": workspace_answer(archived, show_sql=actor["role"] == "admin")}
                        return
                events = query_events(store, owner, question, payload.mode, payload.params, payload.connector_id,
                                      parent_id=payload.parent_id, show_sql=actor["role"] == "admin",
                                      stream_model=stream_model, request_id=request_id)
                async with aclosing(events):
                    async for event in events:
                        yield event
        return execute_turn()

    @app.post("/api/query")
    async def query(payload: Question, request: Request, actor=Depends(access.reader)):
        events = await run_in_threadpool(prepare_turn, payload, request, actor, stream_model=False)
        try:
            async with aclosing(events):
                async for name, data in events:
                    if name == "done":
                        return data["result"]
        except models.ModelAnswerError as exc:
            raise HTTPException(502, "回答生成中断，请重试。") from exc
        except KeyError as exc:
            if exc.args == ("数据接口不存在",):
                raise HTTPException(404, exc.args[0]) from None
            raise

    @app.post("/api/query/stream")
    def stream_query(payload: Question, request: Request, actor=Depends(access.reader)):
        events = prepare_turn(payload, request, actor, stream_model=True)
        async def body():
            try:
                async with aclosing(events):
                    async for name, data in events:
                        yield f"event: {name}\ndata: {pack(data)}\n\n"
            except Exception as exc:
                message = "回答生成中断，请重试。"
                if isinstance(exc, KeyError) and exc.args == ("数据接口不存在",):
                    message = exc.args[0]
                else:
                    logging.getLogger(__name__).exception("Streaming query failed")
                yield f"event: error\ndata: {pack({'message': message})}\n\n"

        return QueryStreamResponse(body(), media_type="text/event-stream",
                                   headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/history")
    def history(request: Request, actor=Depends(access.reader), limit: int = 100, offset: int = 0,
                scope: Literal["mine", "workspace"] | None = None, q: str = Query("", max_length=2000),
                threads: bool = False):
        if scope == "workspace" and actor["role"] != "admin":
            raise HTTPException(403, "查看工作区全部查询需要管理员权限")
        whole_workspace = actor["role"] == "admin" and scope != "mine"
        clauses, params = [], []
        if not whole_workspace:
            clauses.append("owner=?")
            params.append(actor["owner"])
        if q.strip():
            clauses.append("instr(lower(question), lower(?))>0")
            params.append(q.strip())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        prefix = ""
        if threads:
            # A second tab can branch from an older turn. Each leaf remains accessible.
            if q.strip():
                prefix = ("WITH RECURSIVE matches(id,owner) AS (SELECT id,owner FROM history" + where +
                          " UNION SELECT h.id,h.owner FROM history h JOIN matches m"
                          " ON h.parent_id=m.id AND h.owner=m.owner) ")
                where = " WHERE id IN (SELECT id FROM matches)"
            where += (" AND " if where else " WHERE ") + (
                "NOT EXISTS (SELECT 1 FROM history child WHERE child.parent_id=history.id AND child.owner=history.owner)")
        with request.app.state.store.db() as conn:
            return [dict(r) for r in conn.execute(prefix + "SELECT id,question,intent,status,created_at FROM history" + where
                + " ORDER BY id DESC LIMIT ? OFFSET ?", (*params, max(1, min(200, limit)), max(0, min(offset, 2**31 - 1))))]

    def stored_answer(ident, request, actor):
        with request.app.state.store.db() as conn:
            row = conn.execute("SELECT result FROM history WHERE id=? AND (?='admin' OR owner=?)",
                               (ident, actor["role"], actor["owner"])).fetchone()
        if not row:
            raise HTTPException(404, "查询记录不存在")
        return {**json.loads(row["result"]), "id": ident}

    @app.get("/api/history/{ident}")
    def replay(ident: int, request: Request, actor=Depends(access.reader)):
        with request.app.state.store.db() as conn:
            rows = conn.execute("""WITH RECURSIVE turns(id,parent_id,owner) AS (
                SELECT id,parent_id,owner FROM history WHERE id=? AND (?='admin' OR owner=?)
                UNION ALL
                SELECT h.id,h.parent_id,h.owner FROM history h JOIN turns t
                  ON h.id=t.parent_id AND h.id<t.id AND h.owner=t.owner
            ) SELECT h.id,h.result,h.owner FROM history h JOIN turns t ON t.id=h.id ORDER BY h.id""",
                                (ident, actor["role"], actor["owner"])).fetchall()
        if not rows:
            raise HTTPException(404, "查询记录不存在")
        turns = [workspace_answer({**json.loads(row["result"]), "id": row["id"]},
                                  show_sql=actor["role"] == "admin") for row in rows]
        latest = turns.pop()
        if rows[-1]["owner"] != actor["owner"]:
            latest["can_continue"] = False
        if turns:
            latest["predecessors"] = turns
        return latest

    @app.get("/api/history/{ident}/sources/{source_id}/{representation}")
    def snapshot(ident: int, source_id: str, representation: Literal["raw", "rows"], request: Request,
                 actor=Depends(access.reader)):
        result = stored_answer(ident, request, actor)
        source = next((s for s in result.get("citations") or [] if isinstance(s, dict) and
                       s.get("id") == source_id and s.get("kind") == "api"), None)
        if not source:
            raise HTTPException(404, "记录中没有这份接口证据")
        if representation == "rows":
            if not isinstance(source.get("rows"), list):
                raise HTTPException(404, "这条历史记录未保存提取记录")
            data = pack(source["rows"]).encode()
            digest = source.get("rows_sha256") or hashlib.sha256(data).hexdigest()
        else:
            with request.app.state.store.db() as conn:
                row = conn.execute("SELECT body,digest FROM api_snapshots WHERE history_id=? AND source_id=?",
                                   (ident, source_id)).fetchone()
            if row is None:
                raise HTTPException(404, "这条历史记录未保存原始响应字节")
            data, digest = row
        if actor["role"] != "admin":
            # Inspect the actual requested representation, including raw fields
            # outside records_path. Never modify bytes covered by the archive hash.
            try:
                content = source["rows"] if representation == "rows" else json.loads(data)
                restricted = display_fields(content) != content
            except (ValueError, UnicodeError, RecursionError):
                restricted = True
            if restricted:
                raise HTTPException(403, "此快照包含受限字段，普通成员可在来源面板查看可展示的业务记录。")
        return Response(data, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="query-{ident}-{source_id}-{representation}.json"',
            "ETag": '"' + digest + '"'})

    @app.get("/api/audit")
    def audit(request: Request, actor=Depends(access.admin)):
        with request.app.state.store.db() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")]

    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    return app


app = create_app()
