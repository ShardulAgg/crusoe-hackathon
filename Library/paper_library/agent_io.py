"""Persist agent exchanges so resuming compute does not repeat verification API effects."""

import hashlib
import json

from .verification_api import canonical_uuid

MAX_LINE_BYTES = 2 * 1024 * 1024


class AgentProtocolError(Exception):
    pass


def response_message_id(request_id):
    return "api-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def encode(value):
    line = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise AgentProtocolError("agent_message_too_large")
    return line


class AgentIO:
    def __init__(self, queue):
        self.queue = queue
        with queue.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS agent_tasks (
                compute_job_id TEXT PRIMARY KEY, line TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_requests (
                compute_job_id TEXT NOT NULL, request_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, event TEXT NOT NULL, response TEXT,
                PRIMARY KEY(compute_job_id,request_id), UNIQUE(compute_job_id,sequence))""")

    def task(self, job_id, paper):
        from .agent_protocol import VERIFICATION_API_ACCESS

        with self.queue.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT line FROM agent_tasks WHERE compute_job_id=?", (job_id,)
            ).fetchone()
            if row:
                return row["line"]
            line = encode(
                {
                    "type": "task",
                    "protocol_version": 1,
                    "paper": paper,
                    "verification_api": VERIFICATION_API_ACCESS,
                }
            )
            db.execute("INSERT INTO agent_tasks VALUES (?,?)", (job_id, line))
            return line

    def response(self, job_id, sequence, event, allow_new):
        request_id = event.get("id")
        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 100
            or event.get("method") not in {"GET", "POST"}
            or not isinstance(event.get("path"), str)
            or not isinstance(event.get("body"), (dict, type(None)))
            or set(event) - {"type", "id", "method", "path", "body"}
        ):
            raise AgentProtocolError("invalid_agent_request")
        encoded = encode(event)
        with self.queue.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM agent_requests WHERE compute_job_id=? AND request_id=?",
                (job_id, request_id),
            ).fetchone()
            if row:
                if row["sequence"] != sequence or row["event"] != encoded:
                    raise AgentProtocolError("duplicate_agent_request")
                if row["response"] is None:
                    raise AgentProtocolError("verification_api_outcome_unknown")
                return row["response"]
            count = db.execute(
                "SELECT COUNT(*) FROM agent_requests WHERE compute_job_id=?", (job_id,)
            ).fetchone()[0]
            if not allow_new or count >= 1000:
                raise AgentProtocolError("unfinished_agent_request")
            db.execute(
                "INSERT INTO agent_requests VALUES (?,?,?,?,NULL)",
                (job_id, request_id, sequence, encoded),
            )
        return None

    def save_response(self, job_id, request_id, response):
        line = encode(response)
        with self.queue.connection() as db:
            db.execute(
                "UPDATE agent_requests SET response=? WHERE compute_job_id=? AND request_id=?",
                (line, job_id, request_id),
            )
        return line

    def owned_jobs(self, job_id):
        with self.queue.connection() as db:
            rows = db.execute(
                "SELECT event,response FROM agent_requests WHERE compute_job_id=?", (job_id,)
            ).fetchall()
        owned = set()
        for row in rows:
            event = json.loads(row["event"])
            response = json.loads(row["response"]) if row["response"] else None
            if (
                event.get("method") == "POST"
                and event.get("path") == "/jobs"
                and response
                and response.get("status_code") == 202
                and isinstance(response.get("body"), dict)
                and canonical_uuid(response["body"].get("id"))
            ):
                owned.add(response["body"]["id"])
        return owned
