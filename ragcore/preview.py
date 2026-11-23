"""Authenticated online reading of the exact document version stored at import."""
import json
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import Response

from . import access
from .ingest_worker import UploadBusyError, inspect_json, inspect_sheet
from .source_window import TEXT_FORMATS, text_window
from .storage import DOC_FIELDS


router = APIRouter(prefix="/api/documents", dependencies=[Depends(access.reader)])
DocumentID = Annotated[int, PathParam(ge=1, le=2**63 - 1)]
SEARCH = " AND (instr(lower(text),lower(?))>0 OR instr(lower(locator),lower(?))>0 OR instr(lower(context),lower(?))>0)"


def original_mime(filename, signature):
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" and signature.startswith(b"%PDF-"):
        return "application/pdf"
    if suffix == ".png" and signature.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


@router.get("/{doc_id}/preview")
def preview(doc_id: DocumentID, request: Request, q: str = Query("", max_length=256),
            offset: int = Query(0, ge=0, le=2**31 - 1), limit: int = Query(30, ge=1, le=100),
            chunk_id: int | None = Query(None, ge=1, le=2**63 - 1)):
    term = q.strip()
    condition = " WHERE document_id=?" + (SEARCH if term else "")
    params = (doc_id, term, term, term) if term else (doc_id,)
    with request.app.state.store.db() as conn:
        doc = conn.execute(f"SELECT {DOC_FIELDS}, report,recipe,active, substr(content,1,12) AS sig FROM documents WHERE id=?",
                           (doc_id,)).fetchone()
        if doc is None:
            raise HTTPException(404, "文档不存在")
        total = (conn.execute("SELECT count(*) FROM chunks" + condition, params).fetchone()[0]
                 if term else doc["chunk_count"])
        focus = chunk_id
        if chunk_id is not None:
            if not conn.execute("SELECT 1 FROM chunks WHERE id=? AND document_id=?", (chunk_id, doc_id)).fetchone():
                raise HTTPException(404, "此引用段落不存在于该文档版本")
            if term and not conn.execute("SELECT 1 FROM chunks" + condition + " AND id=?",
                                         (*params, chunk_id)).fetchone():
                focus = None
            else:
                before = conn.execute("SELECT count(*) FROM chunks" + condition + " AND id<?",
                                      (*params, chunk_id)).fetchone()[0]
                offset = (before // limit) * limit
        offset = min(offset, (max(total - 1, 0) // limit) * limit)
        chunks = conn.execute("SELECT id,locator,text,context FROM chunks" + condition + " ORDER BY id LIMIT ? OFFSET ?",
                              (*params, limit, offset)).fetchall()
    meta = dict(doc)
    meta.update(json.loads(meta.pop("report")))
    meta["options"] = json.loads(meta.pop("recipe")).get("options", {})
    mime = original_mime(meta["filename"], meta.pop("sig"))
    meta["active"] = bool(meta["active"])
    kind = ("pdf" if mime == "application/pdf" else "image") if mime else None
    return {"document": meta, "chunks": [dict(chunk) for chunk in chunks], "total": total, "offset": offset,
            "limit": limit, "focus_chunk_id": focus, "original_kind": kind,
            "original_url": f"/api/documents/{doc_id}/original" if mime else None}


@router.get("/{doc_id}/source-window", dependencies=[Depends(access.admin)])
def source_window(doc_id: DocumentID, request: Request, chunk_id: int = Query(ge=1, le=2**63 - 1),
                  column: int | None = Query(None, ge=1, le=512)):
    with request.app.state.store.db() as conn:
        row = conn.execute("""SELECT d.filename,c.locator,c.metadata FROM documents d JOIN chunks c ON c.document_id=d.id
                              WHERE d.id=? AND c.id=?""", (doc_id, chunk_id)).fetchone()
        if row is None:
            raise HTTPException(404, "此分块不属于该文档版本")
        suffix = Path(row["filename"]).suffix.lower()
        if suffix not in TEXT_FORMATS and suffix != ".xlsx":
            raise HTTPException(415, "请使用原版预览或下载原件核对")
        data = conn.execute("SELECT content FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
        header = None
        if suffix in {".csv", ".tsv"}:
            header = conn.execute("SELECT locator FROM chunks WHERE document_id=? ORDER BY id LIMIT 1",
                                  (doc_id,)).fetchone()[0]
    try:
        metadata = json.loads(row["metadata"])
        if suffix == ".xlsx":
            view = inspect_sheet(data, row["locator"], start_column=column, header_rows=metadata.get("header_rows", []))
            view["fields"] = [{"column":cell["column"], "address":cell["address"], "label":cell["label"][:160]}
                              for cell in metadata.get("cells", [])]
            return view
        if suffix in {".json", ".jsonl", ".ndjson"}:
            locator = row["locator"]
            pointer = metadata.get("pointer")
            if pointer is None:
                pointer = re.sub(r" · 文本字符 \d+–\d+$", "", locator.partition(" · JSON Pointer ")[2])
            line = re.match(r"第 (\d+) 行", locator)
            return inspect_json(data, suffix, pointer, int(line[1]) if line else None)
        if metadata.get("header_rows") and suffix in {".csv", ".tsv"}:
            return text_window(data, row["locator"], header_rows=metadata["header_rows"])
        return text_window(data, row["locator"], header, plain=suffix in {".md", ".txt"})
    except UploadBusyError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def byte_range(header, size):
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d{0,19})-(\d{0,19})", header.strip(), re.ASCII)
    if not match or not any(match.groups()):
        return None  # Unsupported/multiple ranges are served as a complete representation.
    left, right = match.groups()
    first = int(left) if left else max(0, size - int(right))
    last = min(int(right), size - 1) if left and right else size - 1
    if first > last:
        raise HTTPException(416, "请求的文件范围无效", headers={"Content-Range": f"bytes */{size}"})
    return first, last


@router.get("/{doc_id}/original")
@router.head("/{doc_id}/original", include_in_schema=False)
def original(doc_id: DocumentID, request: Request, range_header: str | None = Header(None, alias="Range")):
    with request.app.state.store.db() as conn:
        row = conn.execute("SELECT filename,size,sha256,substr(content,1,12) AS sig FROM documents WHERE id=?",
                           (doc_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "文档不存在")
        mime = original_mime(row["filename"], row["sig"])
        if not mime:
            raise HTTPException(415, "该格式支持在线阅读提取正文，也可下载原件")
        size, etag = row["size"], '"' + row["sha256"] + '"'
        span = byte_range(range_header, size) if request.headers.get("if-range", etag) == etag else None
        first, last = span or (0, size - 1)
        length = last - first + 1
        body = b""
        if request.method == "GET":
            body = conn.execute("SELECT substr(content,?,?) FROM documents WHERE id=?",
                                (first + 1, length, doc_id)).fetchone()[0]
    headers = {"Content-Disposition": "inline; filename*=UTF-8''" + quote(row["filename"], safe=""),
               "Content-Length": str(length), "Accept-Ranges": "bytes", "ETag": etag,
               "Cross-Origin-Resource-Policy": "same-origin"}
    if span:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    request.state.pdf_preview = mime == "application/pdf"
    return Response(body, status_code=206 if span else 200, media_type=mime, headers=headers)
