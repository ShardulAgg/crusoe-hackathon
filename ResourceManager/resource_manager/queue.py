"""Generic durable compute jobs, capacity reservations, and opaque line transport."""

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .models import MAX_LINE_BYTES, JobSubmission, NodeRegistration, StdinMessage

LEASE_SECONDS = 120
MAX_STDOUT_BYTES = 16 * 1024 * 1024
MAX_STDIN_BYTES = 16 * 1024 * 1024
MAX_STDOUT_RESPONSE_BYTES = 8 * 1024 * 1024
PUBLIC_JOB_FIELDS = (
    "id",
    "status",
    "resource_id",
    "node_id",
    "cpus",
    "memory_mb",
    "scratch_mb",
    "timeout_seconds",
    "sandbox_id",
    "exit_code",
    "error",
    "cleanup_required",
    "created_at",
    "started_at",
    "completed_at",
)


class QueueError(Exception):
    def __init__(self, status_code, code):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class LeaseLost(Exception):
    pass


class JobCancelled(Exception):
    pass


class OutputLimit(Exception):
    pass


def timestamp(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat()


def idempotency_key(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(ord(c) < 32 or ord(c) > 126 for c in value)
    ):
        raise QueueError(422, "Invalid Idempotency-Key")
    return value


class ComputeQueue:
    def __init__(self, path):
        self.path = str(Path(path).expanduser())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            deadline = time.monotonic() + 10
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS compute_nodes (
                    id TEXT PRIMARY KEY, cpus REAL NOT NULL, memory_mb INTEGER NOT NULL,
                    configured_status TEXT NOT NULL, last_seen REAL NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS compute_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'queued', resource_id TEXT NOT NULL,
                    node_id TEXT NOT NULL, cpus REAL NOT NULL, memory_mb INTEGER NOT NULL,
                    scratch_mb INTEGER NOT NULL, timeout_seconds INTEGER NOT NULL,
                    sandbox_id TEXT NOT NULL, exit_code INTEGER, error TEXT,
                    cleanup_required INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, allocation_held INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT, lease_token TEXT, lease_until REAL, allocation_token TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    stdin_closed INTEGER NOT NULL DEFAULT 0,
                    stdout_bytes INTEGER NOT NULL DEFAULT 0,
                    image TEXT, docker_context TEXT
                );
                CREATE INDEX IF NOT EXISTS compute_jobs_node
                    ON compute_jobs(node_id,allocation_held);
                CREATE TABLE IF NOT EXISTS compute_requests (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS compute_stdin (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    id TEXT NOT NULL, line TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(job_id,id)
                );
                CREATE TABLE IF NOT EXISTS compute_stdout (
                    job_id TEXT NOT NULL, sequence INTEGER NOT NULL, line TEXT NOT NULL,
                    PRIMARY KEY(job_id,sequence)
                );
            """)

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
    def public(row):
        if row is None:
            return None
        result = {field: row[field] for field in PUBLIC_JOB_FIELDS}
        result["cleanup_required"] = bool(result["cleanup_required"])
        return result

    @staticmethod
    def expire(db, now):
        # A missing owner cannot prove a remote process stopped. Keep its reservation.
        db.execute(
            """UPDATE compute_jobs SET
               status=CASE WHEN status='cancelled' THEN status ELSE 'failed' END,
               error='compute_lease_expired', cleanup_required=1,
               lease_token=NULL, lease_until=NULL,
               completed_at=? WHERE allocation_held=1 AND lease_token IS NOT NULL AND lease_until<=?
               AND status IN ('provisioning','running','cancelled')""",
            (timestamp(now), now),
        )

    @staticmethod
    def usage(db, node_id):
        held = db.execute(
            "SELECT cpus,memory_mb FROM compute_jobs WHERE node_id=? AND allocation_held=1",
            (node_id,),
        ).fetchall()
        return (
            len(held),
            sum((Decimal(str(row["cpus"])) for row in held), Decimal(0)),
            sum(row["memory_mb"] for row in held),
        )

    @staticmethod
    def node_public(row, now):
        return {
            "id": row["id"],
            "cpus": row["cpus"],
            "memory_mb": row["memory_mb"],
            "status": "online"
            if row["configured_status"] == "online" and row["last_seen"] > now - LEASE_SECONDS
            else "offline",
            "last_seen": row["last_seen"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def register_node(self, registration):
        body = NodeRegistration.model_validate(
            registration.model_dump()
            if isinstance(registration, NodeRegistration)
            else registration
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM compute_nodes WHERE id=?", (body.id,)).fetchone()
            count, _, _ = self.usage(db, body.id)
            if (
                previous is not None
                and count
                and (previous["cpus"] != body.cpus or previous["memory_mb"] != body.memory_mb)
            ):
                raise QueueError(
                    409, "Cannot change node capacity while compute allocations are held"
                )
            now = time.time()
            db.execute(
                """INSERT INTO compute_nodes VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   cpus=excluded.cpus,memory_mb=excluded.memory_mb,
                   configured_status=excluded.configured_status,last_seen=excluded.last_seen,
                   updated_at=excluded.updated_at""",
                (
                    body.id,
                    body.cpus,
                    body.memory_mb,
                    body.status,
                    now,
                    timestamp(now),
                    timestamp(now),
                ),
            )
            return self.node_public(
                db.execute("SELECT * FROM compute_nodes WHERE id=?", (body.id,)).fetchone(), now
            )

    def heartbeat_node(self, node_id):
        with self.connection() as db:
            now = time.time()
            updated = db.execute(
                "UPDATE compute_nodes SET last_seen=?,updated_at=? WHERE id=?",
                (now, timestamp(now), node_id),
            ).rowcount
            if not updated:
                raise QueueError(404, "Node not found")
            return self.node_public(
                db.execute("SELECT * FROM compute_nodes WHERE id=?", (node_id,)).fetchone(), now
            )

    def nodes(self):
        with self.connection() as db:
            return [
                self.node_public(row, time.time())
                for row in db.execute("SELECT * FROM compute_nodes ORDER BY id")
            ]

    def resources(self, catalog):
        result = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            for resource in catalog.resources:
                if not resource.enabled or resource.node_id is None:
                    continue
                node = db.execute(
                    "SELECT * FROM compute_nodes WHERE id=?", (resource.node_id,)
                ).fetchone()
                if node is None or self.node_public(node, now)["status"] != "online":
                    continue
                count, cpus, memory = self.usage(db, resource.node_id)
                remaining_cpu = Decimal(str(min(resource.cpus, node["cpus"]))) - cpus
                remaining_memory = min(resource.memory_mb, node["memory_mb"]) - memory
                slots = resource.max_concurrent_jobs - count
                if remaining_cpu <= 0 or remaining_memory < 64 or slots <= 0:
                    continue
                result.append(
                    {
                        "id": resource.id,
                        "node_id": resource.node_id,
                        "description": resource.description,
                        "enabled": True,
                        "cpus": float(remaining_cpu),
                        "memory_mb": remaining_memory,
                        "scratch_mb": resource.scratch_mb,
                        "max_timeout_seconds": resource.max_timeout_seconds,
                        "max_concurrent_jobs": slots,
                        "cost_per_hour_usd": resource.cost_per_hour_usd,
                        "allocation_mode": resource.allocation_mode,
                    }
                )
        return result

    def submit(self, submission, key, catalog):
        key = idempotency_key(key)
        body = JobSubmission.model_validate(
            submission.model_dump() if isinstance(submission, JobSubmission) else submission
        )
        payload = json.dumps(
            body.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM compute_requests WHERE key=?", (key,)).fetchone()
            if previous is not None:
                if previous["payload"] != payload:
                    raise QueueError(
                        409, "Idempotency-Key was used for a different compute request"
                    )
                return self.public(
                    db.execute(
                        "SELECT * FROM compute_jobs WHERE id=?", (previous["job_id"],)
                    ).fetchone()
                )
            try:
                resource = catalog.get(body.resource_id)
                resource.check_request(body)
            except ValueError as exc:
                raise QueueError(
                    422, "Compute request does not match an approved resource"
                ) from exc
            node = db.execute("SELECT * FROM compute_nodes WHERE id=?", (body.node_id,)).fetchone()
            if node is None:
                raise QueueError(409, "The requested node has not registered")
            if body.cpus > node["cpus"] or body.memory_mb > node["memory_mb"]:
                raise QueueError(422, "Compute request exceeds registered node capacity")
            job_id = str(uuid4())
            db.execute(
                """INSERT INTO compute_jobs
                   (id,resource_id,node_id,cpus,memory_mb,scratch_mb,timeout_seconds,sandbox_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    body.resource_id,
                    body.node_id,
                    body.cpus,
                    body.memory_mb,
                    body.scratch_mb,
                    body.timeout_seconds,
                    f"compute-job-{job_id}",
                    timestamp(),
                ),
            )
            db.execute("INSERT INTO compute_requests VALUES(?,?,?)", (key, payload, job_id))
            return self.public(
                db.execute("SELECT * FROM compute_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def get(self, job_id):
        with self.connection() as db:
            self.expire(db, time.time())
            row = db.execute("SELECT * FROM compute_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise QueueError(404, "Compute job not found")
            return self.public(row)

    def list_jobs(self, status=None, resource_id=None, node_id=None, limit=50, offset=0):
        filters = []
        values = []
        for column, value in (
            ("status", status),
            ("resource_id", resource_id),
            ("node_id", node_id),
        ):
            if value is not None:
                filters.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.connection() as db:
            self.expire(db, time.time())
            total = db.execute("SELECT COUNT(*) FROM compute_jobs" + where, values).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM compute_jobs" + where + " ORDER BY sequence LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return {
            "jobs": [self.public(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def claim(self, worker_id, catalog):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            for row in db.execute(
                "SELECT * FROM compute_jobs WHERE status='queued' ORDER BY sequence"
            ):
                try:
                    resource = catalog.get(row["resource_id"])
                    resource.check_request(
                        JobSubmission.model_validate(
                            {key: row[key] for key in JobSubmission.model_fields}
                        )
                    )
                except (ValueError, TypeError):
                    continue
                node = db.execute(
                    "SELECT * FROM compute_nodes WHERE id=?", (row["node_id"],)
                ).fetchone()
                if node is None or self.node_public(node, now)["status"] != "online":
                    continue
                count, cpus, memory = self.usage(db, row["node_id"])
                if (
                    count >= resource.max_concurrent_jobs
                    or cpus + Decimal(str(row["cpus"]))
                    > Decimal(str(min(resource.cpus, node["cpus"])))
                    or memory + row["memory_mb"] > min(resource.memory_mb, node["memory_mb"])
                ):
                    continue
                token = secrets.token_urlsafe(32)
                db.execute(
                    """UPDATE compute_jobs SET status='provisioning',allocation_held=1,
                       worker_id=?,lease_token=?,lease_until=?,allocation_token=?,started_at=?,
                       image=?,docker_context=? WHERE id=?""",
                    (
                        worker_id,
                        token,
                        now + LEASE_SECONDS,
                        token,
                        timestamp(now),
                        resource.image,
                        resource.docker_context,
                        row["id"],
                    ),
                )
                job = self.public(
                    db.execute("SELECT * FROM compute_jobs WHERE id=?", (row["id"],)).fetchone()
                )
                return job, token, resource
        return None

    def heartbeat(self, job_id, token):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.expire(db, now)
            row = db.execute(
                "SELECT * FROM compute_jobs WHERE id=? AND lease_token=?", (job_id, token)
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE compute_jobs SET lease_until=? WHERE id=?",
                    (now + LEASE_SECONDS, job_id),
                )
                active = not bool(row["cancel_requested"])
        if row is None:
            raise LeaseLost
        return active

    def mark_running(self, job_id, token):
        if not self.heartbeat(job_id, token):
            raise JobCancelled
        with self.connection() as db:
            updated = db.execute(
                """UPDATE compute_jobs SET status='running' WHERE id=? AND lease_token=?
                   AND status='provisioning' AND cancel_requested=0""",
                (job_id, token),
            ).rowcount
        if not updated:
            raise JobCancelled

    def put_stdin(self, job_id, message):
        body = StdinMessage.model_validate(message)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT line FROM compute_stdin WHERE job_id=? AND id=?", (job_id, body.id)
            ).fetchone()
            if existing is not None:
                if existing["line"] != body.line:
                    raise QueueError(409, "Input message ID was used for a different line")
                return {"id": body.id, "accepted": True}
            job = db.execute(
                "SELECT status,stdin_closed FROM compute_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise QueueError(404, "Compute job not found")
            if job["status"] not in {"queued", "provisioning", "running"} or job["stdin_closed"]:
                raise QueueError(409, "Compute job input is closed")
            stored_bytes = db.execute(
                "SELECT COALESCE(SUM(length(CAST(line AS BLOB))+1),0) "
                "FROM compute_stdin WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            if stored_bytes + len(body.line.encode("utf-8")) + 1 > MAX_STDIN_BYTES:
                raise QueueError(413, "Compute job input exceeds 16 MiB")
            db.execute(
                "INSERT INTO compute_stdin(job_id,id,line) VALUES(?,?,?)",
                (job_id, body.id, body.line),
            )
            return {"id": body.id, "accepted": True}

    def close_stdin(self, job_id):
        with self.connection() as db:
            if not db.execute(
                "UPDATE compute_jobs SET stdin_closed=1 WHERE id=?", (job_id,)
            ).rowcount:
                raise QueueError(404, "Compute job not found")
        return {"closed": True}

    def pending_stdin(self, job_id):
        with self.connection() as db:
            job = db.execute(
                "SELECT stdin_closed FROM compute_jobs WHERE id=?", (job_id,)
            ).fetchone()
            row = db.execute(
                "SELECT sequence,line FROM compute_stdin WHERE job_id=? AND delivered=0 "
                "ORDER BY sequence LIMIT 1",
                (job_id,),
            ).fetchone()
            return (dict(row) if row is not None else None), bool(job["stdin_closed"])

    def ack_stdin(self, job_id, sequence):
        with self.connection() as db:
            db.execute(
                "UPDATE compute_stdin SET delivered=1 WHERE job_id=? AND sequence=?",
                (job_id, sequence),
            )

    def append_stdout(self, job_id, token, line):
        encoded = line.encode("utf-8")
        if (
            len(encoded) > MAX_LINE_BYTES
            or "\n" in line
            or len(
                json.dumps({"sequence": 2**63 - 1, "line": line}, ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            > MAX_STDOUT_RESPONSE_BYTES - 128
        ):
            raise OutputLimit
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT * FROM compute_jobs WHERE id=? AND lease_token=?", (job_id, token)
            ).fetchone()
            if job is None:
                raise LeaseLost
            if job["stdout_bytes"] + len(encoded) + 1 > MAX_STDOUT_BYTES:
                raise OutputLimit
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM compute_stdout WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            db.execute("INSERT INTO compute_stdout VALUES(?,?,?)", (job_id, sequence, line))
            db.execute(
                "UPDATE compute_jobs SET stdout_bytes=stdout_bytes+? WHERE id=?",
                (len(encoded) + 1, job_id),
            )

    def stdout(self, job_id, after=0):
        self.get(job_id)
        with self.connection() as db:
            rows = db.execute(
                "SELECT sequence,line FROM compute_stdout WHERE job_id=? AND sequence>? "
                "ORDER BY sequence LIMIT 64",
                (job_id, after),
            ).fetchall()
        lines = []
        size = 128
        for row in rows:
            item = dict(row)
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if size + len(encoded) + 1 > MAX_STDOUT_RESPONSE_BYTES:
                break
            lines.append(item)
            size += len(encoded) + 1
        return {"lines": lines, "next_cursor": lines[-1]["sequence"] if lines else after}

    def cancel(self, job_id):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM compute_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise QueueError(404, "Compute job not found")
            if row["status"] in {"queued", "provisioning", "running"}:
                db.execute(
                    """UPDATE compute_jobs SET status='cancelled',cancel_requested=1,completed_at=?,
                       cleanup_required=allocation_held WHERE id=?""",
                    (timestamp(), job_id),
                )
            return self.public(
                db.execute("SELECT * FROM compute_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def finish(self, job_id, token, exit_code=None, error=None, cleanup_confirmed=True):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db, time.time())
            row = db.execute(
                "SELECT * FROM compute_jobs WHERE id=? AND lease_token=?", (job_id, token)
            ).fetchone()
            if row is not None:
                status = (
                    "cancelled"
                    if row["cancel_requested"]
                    else ("completed" if error is None and exit_code == 0 else "failed")
                )
                db.execute(
                    """UPDATE compute_jobs SET status=?,exit_code=?,error=?,cleanup_required=?,
                       completed_at=?,lease_token=NULL,lease_until=NULL,allocation_held=?,
                       allocation_token=CASE WHEN ? THEN NULL ELSE allocation_token END
                       WHERE id=?""",
                    (
                        status,
                        exit_code,
                        error,
                        not cleanup_confirmed,
                        timestamp(),
                        not cleanup_confirmed,
                        cleanup_confirmed,
                        job_id,
                    ),
                )
        if row is None:
            raise LeaseLost

    def release_capacity(self, job_id, allocation_token):
        with self.connection() as db:
            updated = db.execute(
                """UPDATE compute_jobs SET allocation_held=0,cleanup_required=0,
                   allocation_token=NULL
                   WHERE id=? AND allocation_token=?""",
                (job_id, allocation_token),
            ).rowcount
        return bool(updated)
