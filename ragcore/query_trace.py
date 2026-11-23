"""Server-observed query timings. Store only stage metadata, never provider payloads."""
import json
import logging
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime, timezone

import anyio


active_stage = ContextVar("active_query_stage", default=None)


def stage_note(code):
    stage = active_stage.get()
    if stage is not None and code not in stage["notes"]:
        stage["notes"].append(code)


class QueryTrace:
    def __init__(self, mode, request_id):
        self.mode = mode
        self.request_id = request_id
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.started = time.perf_counter_ns()
        self.stage_started = self.started
        self.stages = []
        self.finished = False

    def _close_stage(self, timestamp):
        if self.stages:
            self.stages[-1]["duration_ms"] = round((timestamp - self.stage_started) / 1_000_000, 3)

    def start(self, name):
        stamp = time.perf_counter_ns() if self.stages else self.started
        self._close_stage(stamp)
        self.stage_started = stamp
        stage = {"name": name, "status": "ok", "notes": []}
        self.stages.append(stage)
        active_stage.set(stage)

    def mark(self, status):
        self.stages[-1]["status"] = status

    async def finish(self, store, outcome, history_id=None):
        if self.finished:
            return
        self.finished = True
        stamp = time.perf_counter_ns()
        self._close_stage(stamp)
        values = (self.started_at, self.mode, self.request_id, history_id, outcome,
                  round((stamp - self.started) / 1_000_000, 3), json.dumps(self.stages, separators=(",", ":")))

        def persist():
            try:
                with store.db() as conn:
                    conn.execute("""INSERT INTO query_traces(started_at,mode,request_id,history_id,outcome,duration_ms,stages)
                                    VALUES (?,?,?,?,?,?,?)""", values)
            except sqlite3.Error:
                logging.getLogger(__name__).exception("Could not persist query diagnostics")

        # A disconnect cancels the response scope. Its short local write must still finish.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(persist)


def recent_traces(store, before, limit):
    with store.db() as conn:
        rows = conn.execute("""SELECT id,started_at,mode,request_id,history_id,outcome,duration_ms,stages
                               FROM query_traces WHERE id < ? ORDER BY id DESC LIMIT ?""",
                            (before if before is not None else 2**63 - 1, limit + 1)).fetchall()
    items = [{**dict(row), "stages": json.loads(row["stages"])} for row in rows[:limit]]
    return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}
