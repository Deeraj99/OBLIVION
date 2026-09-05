"""Thread-safe job manager for long-running AI work.

Why this exists
---------------
A naive ``/api/ai/exam`` call blocks the Flask worker thread for as long as
Ollama takes to respond. On a normal Windows laptop, generating an exam paper
can take several minutes; during that time the whole UI appears frozen and
multiple browser tabs can each spawn their own blocking request. This module
fixes both problems:

* Every request creates a job, returns its ID immediately, and runs the work
  on a background worker pool.
* Duplicate requests return the existing job ID instead of starting a second
  copy of the same generation.
* Workers respect a small concurrency limit so Ollama is never asked to do
  everything at once.
* Each job has a clear lifecycle (queued / running / completed / failed /
  cancelled) that the frontend polls.

The job store is persisted in SQLite (``ai_jobs`` table) so the UI can poll
across worker restarts and stale rows can be cleaned up safely.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from database import Transaction, get_db

log = logging.getLogger("facultyhub.jobs")

MAX_CONCURRENT = 2
JOB_TTL_SECONDS = 30 * 60  # completed/failed jobs are kept for 30 minutes
WORKER_TICK_SECONDS = 0.2


class JobManager:
    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="ai-job")
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._active_keys: set[str] = set()

    # ----- public helpers ---------------------------------------------------
    def start(
        self,
        kind: str,
        work: Callable[["Job"], Any],
        *,
        dedupe_key: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> dict:
        """Queue a new job or return the existing duplicate's snapshot."""
        now = datetime.now().isoformat(timespec="seconds")
        if dedupe_key:
            with get_db() as db:
                rows = db.execute(
                    "SELECT * FROM ai_jobs WHERE job_kind=? AND status IN ('queued','running') "
                    "AND created_at > ? ORDER BY created_at DESC LIMIT 5",
                    (kind, (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")),
                ).fetchall()
                for row in rows:
                    try:
                        stored_payload = json.loads(row["payload"] or "{}")
                    except Exception:
                        continue
                    if stored_payload.get("_dedupe_key") == dedupe_key:
                        snapshot = self._row_to_job(row)
                        snapshot["duplicate"] = True
                        return snapshot
        job_id = uuid.uuid4().hex[:12]
        payload_to_store = dict(payload or {})
        if dedupe_key:
            payload_to_store["_dedupe_key"] = dedupe_key
        with Transaction() as db:
            db.execute(
                "INSERT INTO ai_jobs(id,job_kind,status,progress,payload,error,created_at,started_at,completed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    kind,
                    "queued",
                    "Queued",
                    json.dumps(payload_to_store),
                    "",
                    now,
                    None,
                    None,
                ),
            )
        snapshot = {"id": job_id, "status": "queued", "kind": kind, "progress": "Queued"}
        if dedupe_key:
            snapshot["dedupe_key"] = dedupe_key

        def runner() -> None:
            self._run(job_id, kind, work, dedupe_key)

        self._executor.submit(runner)
        return snapshot

    def get(self, job_id: str) -> Optional[dict]:
        with get_db() as db:
            row = db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def cancel(self, job_id: str) -> dict:
        # Cancellation is best-effort: the underlying call may already be
        # in flight to Ollama. We mark the row cancelled so the UI stops
        # polling.
        with Transaction() as db:
            db.execute(
                "UPDATE ai_jobs SET status='cancelled', completed_at=?, error=? "
                "WHERE id=? AND status IN ('queued','running')",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    "Cancelled by user.",
                    job_id,
                ),
            )
        return self.get(job_id) or {}

    def cleanup(self) -> int:
        """Remove finished jobs older than JOB_TTL_SECONDS. Returns rows removed."""
        cutoff = (datetime.now() - timedelta(seconds=JOB_TTL_SECONDS)).isoformat(timespec="seconds")
        with Transaction() as db:
            cur = db.execute(
                "DELETE FROM ai_jobs WHERE status IN ('completed','failed','cancelled') "
                "AND completed_at IS NOT NULL AND completed_at < ?",
                (cutoff,),
            )
        return cur.rowcount or 0

    # ----- internal helpers -------------------------------------------------
    def _run(self, job_id: str, kind: str, work: Callable[["Job"], Any], dedupe_key: Optional[str]) -> None:
        with self._counter_lock:
            self._active_keys.add(dedupe_key or job_id)

        try:
            now = datetime.now().isoformat(timespec="seconds")
            with Transaction() as db:
                db.execute(
                    "UPDATE ai_jobs SET status='running', started_at=?, progress=? WHERE id=?",
                    (now, "Running…", job_id),
                )

            job = Job(self, job_id, kind)
            try:
                result = work(job)
            except Exception as exc:  # noqa: BLE001
                log.exception("AI job %s (%s) failed", job_id, kind)
                finished_at = datetime.now().isoformat(timespec="seconds")
                with Transaction() as db:
                    db.execute(
                        "UPDATE ai_jobs SET status='failed', completed_at=?, error=? WHERE id=?",
                        (finished_at, str(exc)[:500], job_id),
                    )
                return

            finished_at = datetime.now().isoformat(timespec="seconds")
            with Transaction() as db:
                db.execute(
                    "UPDATE ai_jobs SET status='completed', completed_at=?, result=?, progress=? WHERE id=?",
                    (finished_at, json.dumps(result, default=str)[:200000], "Completed", job_id),
                )
        finally:
            with self._counter_lock:
                self._active_keys.discard(dedupe_key or job_id)

    def _row_to_job(self, row: Any) -> dict:
        d = dict(row)
        for key in ("payload", "result"):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except Exception:
                    pass
        return d


class Job:
    """Lightweight handle that the worker callable can use to update progress."""

    def __init__(self, manager: JobManager, job_id: str, kind: str) -> None:
        self.manager = manager
        self.id = job_id
        self.kind = kind

    def update_progress(self, message: str) -> None:
        with Transaction() as db:
            db.execute(
                "UPDATE ai_jobs SET progress=? WHERE id=? AND status='running'",
                (message[:200], self.id),
            )


# A single, lazily-initialised instance shared by the Flask app.
_instance: Optional[JobManager] = None
_instance_lock = threading.Lock()


def get_job_manager() -> JobManager:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = JobManager()
    return _instance