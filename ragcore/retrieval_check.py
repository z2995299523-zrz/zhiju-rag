"""Test a question against an administrator-selected version of a document."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import access, model_config, retrieval
from .query_trace import active_stage


router = APIRouter(prefix="/api/admin/retrieval", dependencies=[Depends(access.admin)])


class RetrievalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    document_id: int = Field(ge=1, le=2**63 - 1)
    chunk_id: int | None = Field(default=None, ge=1, le=2**63 - 1)
    top_k: int = Field(default=3, ge=1, le=16)

    @field_validator("question")
    @classmethod
    def valid_question(cls, question):
        question = question.strip()
        if not question:
            raise ValueError("请填写评测问题")
        question.encode("utf-8")
        return question


@router.post("/evaluate")
def evaluate(payload: RetrievalCheck, request: Request):
    store = request.app.state.store
    with store.db() as conn:
        doc = conn.execute("SELECT filename,sha256 FROM documents WHERE active=1 AND id=?",
                           (payload.document_id,)).fetchone()
        if doc is None:
            raise HTTPException(404, "所选文档已移除或换版，请重新选择")
        expected = dict(doc)
        if payload.chunk_id is not None:
            chunk = conn.execute("SELECT locator FROM chunks WHERE id=? AND document_id=?",
                                 (payload.chunk_id, payload.document_id)).fetchone()
            if chunk is None:
                raise HTTPException(422, "所选分块不属于当前文档版本")
            expected.update(chunk_id=payload.chunk_id, locator=chunk["locator"])
    stage = {"notes": []}
    token = active_stage.set(stage)
    try:
        with model_config.workspace(store):
            ranking = retrieval.rank_document_chunks(store, payload.question)
    finally:
        active_stage.reset(token)
    with store.db() as conn:
        if not conn.execute("SELECT 1 FROM documents WHERE id=? AND active=1", (payload.document_id,)).fetchone():
            raise HTTPException(409, "评测期间文档已移除或换版，请重新选择后再测")
    scores = retrieval.score_evidence(ranking, [expected], top_k=payload.top_k)
    top = [{"document_id": row["document_id"], "chunk_id": row["id"], "filename": row["filename"],
            "locator": row["locator"], "text": row["text"][:200], "semantic_score": row.get("semantic_score")}
           for row in ranking.ranked[:payload.top_k]]
    trace = stage.get("metrics", {})
    embedding = {"dimension": trace.get("embedding_dimension"), "outcome": trace.get("embedding_outcome")}
    return {"metrics": scores, "top_chunks": top, "embedding": embedding}
