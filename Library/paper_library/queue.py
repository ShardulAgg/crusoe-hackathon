"""Durable Library-owned verification with idempotent generic compute requests."""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from .models import ComputeJobUpdate, PaperSubmission, VerificationReport, validate_opaque_json

POLL_SECONDS = 2
ACTIVE_STATUSES = {"planning", "verifying"}
TERMINAL_STATUSES = {"completed", "blocked", "failed"}
COMPUTE_ACTIVE_STATUSES = {"queued", "provisioning", "running"}


class QueueConflict(Exception):
    """Verification ownership is missing, expired, or incompatible with the operation."""


def utc_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def safe_error_code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value):
        raise ValueError("Errors must use a bounded safe error code")
    return value


def plan_json(plan):
    if isinstance(plan, BaseModel):
        plan = plan.model_dump(mode="json")
    if type(plan) is not dict:
        raise ValueError("Verification plans must be JSON objects")
    validate_opaque_json(plan)
    return json.dumps(
        plan, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class PaperQueue:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            deadline = time.monotonic() + 10
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """CREATE TABLE IF NOT EXISTS papers (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    submission TEXT NOT NULL
                )"""
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(papers)")}
            additions = {
                "status": "TEXT NOT NULL DEFAULT 'pending_verification'",
                "started_at": "TEXT",
                "completed_at": "TEXT",
                "worker_id": "TEXT",
                "lease_token": "TEXT",
                "lease_until": "REAL",
                "report": "TEXT",
                "error": "TEXT",
                "plan": "TEXT",
                "blocked_reason": "TEXT",
                # Old allocation/handoff fields remain audit data, never new compute identity.
                "resource_id": "TEXT",
                "allocated_cpus": "REAL",
                "allocated_memory_mb": "INTEGER",
                "allocation_token": "TEXT",
                "cleanup_pending": "INTEGER NOT NULL DEFAULT 0",
                "node_id": "TEXT",
                "sandbox_id": "TEXT",
                "resource_manager_job_id": "TEXT",
                "resource_manager_status": "TEXT",
                "dispatch_key": "TEXT",
                "next_poll_at": "REAL",
                "compute_job_id": "TEXT",
                "compute_job_status": "TEXT",
                "compute_key": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE papers ADD COLUMN {name} {definition}")
            db.execute(
                """UPDATE papers SET allocation_token=lease_token
                   WHERE compute_key IS NULL AND dispatch_key IS NULL
                   AND (resource_id IS NOT NULL OR status='verifying')
                   AND allocation_token IS NULL AND lease_token IS NOT NULL"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS papers_status ON papers(status, sequence)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def paper(row):
        if row is None:
            return None
        report = (
            VerificationReport.model_validate_json(row["report"]).model_dump()
            if row["report"] is not None
            else None
        )
        plan = validate_opaque_json(json.loads(row["plan"])) if row["plan"] else None
        if plan is not None and type(plan) is not dict:
            raise ValueError("Stored verification plans must be JSON objects")
        return {
            "id": row["id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "submission": json.loads(row["submission"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "worker_id": row["worker_id"],
            "report": report,
            "error": row["error"],
            "plan": plan,
            "blocked_reason": row["blocked_reason"],
            "cleanup_required": bool(row["cleanup_pending"])
            or (row["resource_id"] is not None and row["status"] in {"failed", "blocked"}),
            "node_id": row["node_id"],
            "sandbox_id": row["sandbox_id"],
            "resource_manager_job_id": row["resource_manager_job_id"],
            "resource_manager_status": row["resource_manager_status"],
            "compute_job_id": row["compute_job_id"],
            "compute_job_status": row["compute_job_status"],
        }

    @staticmethod
    def expire(db, now: float):
        # A worker lease cannot attest that compute stopped, or that agent output was processed.
        db.execute(
            """UPDATE papers SET lease_token=NULL, lease_until=NULL
               WHERE (compute_key IS NOT NULL OR dispatch_key IS NOT NULL) AND lease_until <= ?""",
            (now,),
        )
        # Historical direct-execution rows retain conservative cleanup requirements.
        db.execute(
            """UPDATE papers SET status='failed', completed_at=?,
               error='verification_lease_expired', lease_token=NULL, lease_until=NULL,
               cleanup_pending=CASE WHEN allocation_token IS NOT NULL OR resource_id IS NOT NULL
                                    THEN 1 ELSE 0 END
               WHERE compute_key IS NULL AND dispatch_key IS NULL
               AND status IN ('planning','verifying') AND lease_until <= ?""",
            (utc_time(now), now),
        )

    def submit(self, submission: PaperSubmission):
        if isinstance(submission, BaseModel):
            submission = submission.model_dump(mode="python")
        submission = PaperSubmission.model_validate(submission)
        paper_id = str(uuid4())
        now = time.time()
        with self.connection() as db:
            db.execute(
                "INSERT INTO papers(id,created_at,submission,next_poll_at) VALUES(?,?,?,?)",
                (paper_id, utc_time(now), submission.model_dump_json(), now),
            )
            return self.paper(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())

    def get(self, paper_id: str):
        with self.connection() as db:
            self.expire(db, time.time())
            return self.paper(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())

    def list(self, limit: int, offset: int, status: str | None = None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, time.time())
            where = " WHERE status=?" if status is not None else ""
            parameters = (status,) if status is not None else ()
            total = db.execute("SELECT COUNT(*) FROM papers" + where, parameters).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM papers" + where + " ORDER BY sequence LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "papers": [self.paper(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def claim_for_verification(self, worker_id: str, lease_seconds: int = 120):
        self.validate_lease_seconds(lease_seconds)
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id.strip()) > 200:
            raise ValueError("worker_id must be nonblank and at most 200 characters")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            row = db.execute(
                """SELECT * FROM papers WHERE status='pending_verification'
                   OR (compute_key IS NOT NULL
                       AND (status IN ('planning','verifying') OR
                            (status IN ('completed','blocked','failed') AND cleanup_pending=1
                             AND compute_job_id IS NOT NULL))
                       AND (lease_until IS NULL OR lease_until <= ?)
                       AND (next_poll_at IS NULL OR next_poll_at <= ?))
                   ORDER BY COALESCE(next_poll_at,
                            CAST(strftime('%s',created_at) AS REAL),0), sequence LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            compute_key = row["compute_key"] or str(uuid4())
            status = "planning" if row["status"] == "pending_verification" else row["status"]
            db.execute(
                """UPDATE papers SET status=?, started_at=COALESCE(started_at,?),
                   worker_id=?, lease_token=?, lease_until=?, compute_key=? WHERE id=?""",
                (
                    status,
                    utc_time(now),
                    worker_id.strip(),
                    token,
                    now + lease_seconds,
                    compute_key,
                    row["id"],
                ),
            )
            paper = self.paper(
                db.execute("SELECT * FROM papers WHERE id=?", (row["id"],)).fetchone()
            )
            return {**paper, "lease_token": token, "compute_key": compute_key}

    @staticmethod
    def validate_lease_seconds(value):
        if type(value) is not int or not 1 <= value <= 86400:
            raise ValueError("lease_seconds must be an integer from 1 to 86400")

    @staticmethod
    def owned(db, paper_id, lease_token, now):
        return db.execute(
            """SELECT * FROM papers WHERE id=? AND lease_token=? AND lease_until>?
               AND compute_key IS NOT NULL""",
            (paper_id, lease_token, now),
        ).fetchone()

    def heartbeat(self, paper_id: str, lease_token: str, lease_seconds: int = 120):
        self.validate_lease_seconds(lease_seconds)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            updated = db.execute(
                """UPDATE papers SET lease_until=? WHERE id=? AND lease_token=?
                   AND compute_key IS NOT NULL AND lease_until>?""",
                (now + lease_seconds, paper_id, lease_token, now),
            ).rowcount
        if not updated:
            raise QueueConflict("Verification ownership is missing, expired, or no longer active")

    def save_plan(self, paper_id: str, lease_token: str, plan: dict):
        encoded = plan_json(plan)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            row = self.owned(db, paper_id, lease_token, now)
            if row is not None and row["status"] in ACTIVE_STATUSES:
                if row["plan"] is not None and plan_json(json.loads(row["plan"])) != encoded:
                    raise QueueConflict(
                        "The saved verification plan is immutable within one attempt"
                    )
                if row["plan"] is None:
                    if row["compute_job_id"] is not None:
                        raise QueueConflict("A plan must be saved before creating compute")
                    db.execute(
                        "UPDATE papers SET plan=?,status='planning' WHERE id=?", (encoded, paper_id)
                    )
                paper = self.paper(
                    db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                )
            else:
                paper = None
        if paper is None:
            raise QueueConflict("Verification ownership is missing, expired, or no longer active")
        return paper

    def save_compute_job(self, paper_id: str, lease_token: str, job: dict):
        update = ComputeJobUpdate.model_validate(job)
        job_id = str(update.id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            row = self.owned(db, paper_id, lease_token, now)
            if row is not None:
                if row["plan"] is None:
                    raise QueueConflict("Save the verification plan before creating compute")
                if row["compute_job_id"] not in (None, job_id):
                    raise ValueError("Compute job identity cannot change within one attempt")
                cleanup = update.cleanup_required or update.status in COMPUTE_ACTIVE_STATUSES
                db.execute(
                    """UPDATE papers SET compute_job_id=?,compute_job_status=?,cleanup_pending=?,
                       node_id=COALESCE(?,node_id),sandbox_id=COALESCE(?,sandbox_id),
                       status=CASE WHEN status IN ('planning','verifying') THEN 'verifying'
                                   ELSE status END,
                       error=CASE WHEN status IN ('planning','verifying') THEN NULL ELSE error END
                       WHERE id=?""",
                    (
                        job_id,
                        update.status,
                        int(cleanup),
                        update.node_id,
                        update.sandbox_id,
                        paper_id,
                    ),
                )
                paper = self.paper(
                    db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                )
            else:
                paper = None
        if paper is None:
            raise QueueConflict("Verification ownership is missing, expired, or no longer active")
        return paper

    def finish_verification(
        self,
        paper_id: str,
        lease_token: str,
        report: VerificationReport | None = None,
        error: str | None = None,
        cleanup_required: bool = False,
    ):
        if (report is None) == (error is None):
            raise ValueError("Provide exactly one of report or error")
        if type(cleanup_required) is not bool:
            raise ValueError("cleanup_required must be a boolean")
        if report is not None:
            if isinstance(report, BaseModel):
                report = report.model_dump(mode="python")
            report = VerificationReport.model_validate(report)
        if error is not None:
            error = safe_error_code(error)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            row = self.owned(db, paper_id, lease_token, now)
            paper = None
            if row is not None and row["status"] in ACTIVE_STATUSES:
                # An active compute job must be reconciled even if a caller omitted the flag.
                cleanup = cleanup_required or row["compute_job_status"] in COMPUTE_ACTIVE_STATUSES
                db.execute(
                    """UPDATE papers SET status=?,completed_at=?,report=?,error=?,
                       cleanup_pending=?,lease_token=NULL,lease_until=NULL,next_poll_at=?
                       WHERE id=?""",
                    (
                        "completed" if report is not None else "failed",
                        utc_time(now),
                        report.model_dump_json() if report is not None else None,
                        error,
                        int(cleanup),
                        now + POLL_SECONDS if cleanup else None,
                        paper_id,
                    ),
                )
                paper = self.paper(
                    db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                )
        if paper is None:
            raise QueueConflict("Verification ownership is missing, expired, or no longer active")
        return paper

    def block_verification(
        self, paper_id: str, lease_token: str, reason: str, plan: dict | None = None
    ):
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 10000:
            raise ValueError("A blocked reason must be nonblank and at most 10000 characters")
        if plan is not None:
            self.save_plan(paper_id, lease_token, plan)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            row = self.owned(db, paper_id, lease_token, now)
            paper = None
            if row is not None and row["status"] in ACTIVE_STATUSES:
                cleanup = (
                    bool(row["cleanup_pending"])
                    or row["compute_job_status"] in COMPUTE_ACTIVE_STATUSES
                )
                db.execute(
                    """UPDATE papers SET status='blocked',completed_at=?,blocked_reason=?,
                       report=NULL,error=NULL,cleanup_pending=?,lease_token=NULL,lease_until=NULL,
                       next_poll_at=? WHERE id=?""",
                    (
                        utc_time(now),
                        reason.strip(),
                        int(cleanup),
                        now + POLL_SECONDS if cleanup else None,
                        paper_id,
                    ),
                )
                paper = self.paper(
                    db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                )
        if paper is None:
            raise QueueConflict("Verification ownership is missing, expired, or no longer active")
        return paper

    def defer_verification(self, paper_id: str, lease_token: str, error_code: str):
        error_code = safe_error_code(error_code)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            updated = db.execute(
                """UPDATE papers SET error=CASE WHEN status IN ('planning','verifying') THEN ?
                                              ELSE error END,
                   lease_token=NULL,lease_until=NULL,next_poll_at=?
                   WHERE id=? AND lease_token=? AND lease_until>? AND compute_key IS NOT NULL""",
                (error_code, now + POLL_SECONDS, paper_id, lease_token, now),
            ).rowcount
            paper = self.paper(
                db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            )
        if not updated:
            raise QueueConflict("Verification ownership is missing, expired, or no longer active")
        return paper

    def retry(self, paper_id: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, time.time())
            row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if row is None:
                return None
            legacy_confirmed = (
                row["compute_key"] is not None
                or row["dispatch_key"] is None
                or (
                    row["resource_manager_job_id"] is not None
                    and row["resource_manager_status"] in {"blocked", "failed"}
                )
            )
            eligible = (
                row["status"] in {"blocked", "failed"}
                and legacy_confirmed
                and not row["cleanup_pending"]
                and row["resource_id"] is None
                and (row["compute_job_status"] not in COMPUTE_ACTIVE_STATUSES)
            )
            paper = None
            if eligible:
                db.execute(
                    """UPDATE papers SET status='pending_verification',started_at=NULL,
                       completed_at=NULL,worker_id=NULL,lease_token=NULL,lease_until=NULL,
                       report=NULL,error=NULL,plan=NULL,blocked_reason=NULL,resource_id=NULL,
                       allocated_cpus=NULL,allocated_memory_mb=NULL,allocation_token=NULL,
                       cleanup_pending=0,node_id=NULL,sandbox_id=NULL,compute_job_id=NULL,
                       compute_job_status=NULL,compute_key=NULL,next_poll_at=? WHERE id=?""",
                    (time.time(), paper_id),
                )
                paper = self.paper(
                    db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
                )
        if not eligible:
            raise QueueConflict(
                "Only blocked or failed verifications with completed cleanup can retry"
            )
        return paper
