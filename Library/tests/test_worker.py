import json
import time
from uuid import uuid4

import pytest

from paper_library.agent_io import AgentIO, response_message_id
from paper_library.models import PaperSubmission
from paper_library.queue import PaperQueue
from paper_library.resource_catalog import SandboxPlan
from paper_library.resource_manager import ResourceManagerError
from paper_library.worker import main, run_once

TEXT = "The fixed workload uses 1024 input tokens. Throughput is at least 1000 tokens/s."


def plan(**changes):
    return SandboxPlan.model_validate(
        {
            "decision": "launch",
            "rationale": "Minimum API coordination resources.",
            "verification_steps": ["Measure throughput under the stated conditions."],
            "verification_requirements": [
                {
                    "id": "throughput",
                    "description": "Check throughput",
                    "procedure": ["Run the pinned benchmark"],
                    "conditions": {"input_tokens": 1024},
                    "acceptance_criteria": [
                        {"metric": "tokens/s", "operator": "gte", "expected": 1000}
                    ],
                    "source_quotes": [TEXT],
                }
            ],
            "resource_id": "cpu-small",
            "node_id": "node-1",
            "cpus": 0.25,
            "memory_mb": 256,
            "scratch_mb": 16,
            "timeout_seconds": 60,
            **changes,
        }
    )


class Planner:
    def __init__(self):
        self.calls = 0

    def plan(self, paper, resources):
        self.calls += 1
        return plan()


class Manager:
    def __init__(self):
        self.job = {
            "id": str(uuid4()),
            "status": "running",
            "node_id": "node-1",
            "sandbox_id": "container",
            "cleanup_required": False,
            "exit_code": None,
        }
        self.keys = []
        self.input = {}
        self.lines = []
        self.closed = False
        self.cancellations = 0

    def resources(self):
        return [
            {
                "id": "cpu-small",
                "node_id": "node-1",
                "enabled": True,
                "cpus": 2,
                "memory_mb": 8192,
                "scratch_mb": 64,
            }
        ]

    def submit(self, plan, key):
        self.keys.append(key)
        return dict(self.job)

    def get(self, job_id):
        assert job_id == self.job["id"]
        return dict(self.job)

    def write_stdin(self, job_id, message_id, line):
        if message_id in self.input:
            assert self.input[message_id] == line
        self.input[message_id] = line
        return {"accepted": True}

    def stdout(self, job_id, after=0):
        lines = [
            {"sequence": index + 1, "line": json.dumps(event)}
            for index, event in enumerate(self.lines)
            if index >= after
        ]
        return {"lines": lines, "next_cursor": after + len(lines)}

    def close_stdin(self, job_id):
        self.closed = True

    def cancel(self, job_id):
        self.cancellations += 1
        self.job.update(status="cancelled", cleanup_required=False)
        return dict(self.job)


class API:
    def __init__(self):
        self.calls = []
        self.restored = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def restore_owned_jobs(self, ids):
        self.restored.update(ids)

    def __call__(self, event):
        self.calls.append(event)
        return {
            "type": "api_response",
            "id": event["id"],
            "status_code": 200,
            "body": {"status": "ok"},
        }


@pytest.fixture
def queue(tmp_path):
    return PaperQueue(tmp_path / "papers.sqlite3")


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("paper_library.queue.time.time", lambda: now[0])
    return now


def submit(queue):
    return queue.submit(PaperSubmission(title="Research paper", paper_text=TEXT))


def test_empty_queue_does_not_contact_manager(queue):
    assert run_once(queue, object(), object(), lambda: object(), "worker") is False


def test_library_judges_paper_and_alone_interprets_agent_result(queue, clock):
    paper = submit(queue)
    manager, planner, api = Manager(), Planner(), API()
    assert run_once(queue, manager, planner, lambda: api, "worker")
    verifying = queue.get(paper["id"])
    assert verifying["status"] == "verifying"
    assert verifying["compute_job_id"] == manager.job["id"]
    task = json.loads(manager.input["task"])
    assert task["paper"]["submission"]["paper_text"] == TEXT
    assert "lease_token" not in task["paper"]
    assert "compute_key" not in task["paper"]
    assert verifying["report"] is None
    report = {"verdict": "inconclusive", "summary": "Evidence was not sufficient.", "evidence": []}
    manager.lines = [{"type": "result", "report": report}]
    clock[0] += 3
    run_once(queue, manager, planner, lambda: api, "worker")
    assert manager.closed
    assert queue.get(paper["id"])["status"] == "verifying"
    manager.job.update(status="completed", exit_code=0)
    clock[0] += 3
    run_once(queue, manager, planner, lambda: api, "worker")
    completed = queue.get(paper["id"])
    assert completed["status"] == "completed"
    assert completed["report"] == report
    assert planner.calls == 1
    assert len(manager.keys) == 1


def test_compute_completion_and_zero_exit_alone_do_not_verify_a_paper(queue):
    paper = submit(queue)
    manager = Manager()
    manager.job.update(status="completed", exit_code=0)
    run_once(queue, manager, Planner(), API, "worker")
    assert queue.get(paper["id"])["status"] == "failed"
    assert queue.get(paper["id"])["report"] is None


def test_ambiguous_compute_submission_reuses_saved_plan_and_key(queue, clock):
    paper = submit(queue)
    manager, planner = Manager(), Planner()
    original = manager.submit

    def uncertain(plan, key):
        value = original(plan, key)
        if len(manager.keys) == 1:
            raise ResourceManagerError("timeout")
        return value

    manager.submit = uncertain
    run_once(queue, manager, planner, API, "worker")
    clock[0] += 3
    run_once(PaperQueue(queue.path), manager, planner, API, "restarted-worker")
    assert manager.keys[0] == manager.keys[1]
    assert planner.calls == 1
    assert queue.get(paper["id"])["compute_job_id"] == manager.job["id"]


def test_api_broker_response_and_initial_task_replay_without_duplicate_effects(queue, clock):
    submit(queue)
    manager, planner, api = Manager(), Planner(), API()
    manager.lines = [
        {"type": "api_request", "id": "1", "method": "GET", "path": "/health", "body": None}
    ]
    run_once(queue, manager, planner, lambda: api, "worker")
    first_task = manager.input["task"]
    clock[0] += 3
    run_once(PaperQueue(queue.path), manager, planner, lambda: api, "new-worker")
    assert len(api.calls) == 1
    assert manager.input["task"] == first_task
    assert json.loads(manager.input[response_message_id("1")])["status_code"] == 200


def test_unknown_api_effect_after_crash_fails_safely_without_reissuing(queue, clock):
    paper = submit(queue)
    manager, planner, api = Manager(), Planner(), API()
    run_once(queue, manager, planner, lambda: api, "worker")
    event = {"type": "api_request", "id": "1", "method": "POST", "path": "/jobs", "body": {}}
    AgentIO(queue).response(manager.job["id"], 1, event, allow_new=True)
    manager.lines = [event]
    clock[0] += 3
    run_once(queue, manager, planner, lambda: api, "restarted-worker")
    failed = queue.get(paper["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "verification_api_outcome_unknown"
    assert api.calls == []
    assert manager.cancellations == 1


def test_missing_compute_defers_without_asking_llm(queue):
    paper = submit(queue)
    manager, planner = Manager(), Planner()
    manager.resources = lambda: []
    run_once(queue, manager, planner, API, "worker")
    assert planner.calls == 0
    assert queue.get(paper["id"])["status"] == "planning"


def test_worker_starts_without_docker_access(queue):
    assert (
        main(
            [
                "--resource-manager-url",
                "http://127.0.0.1:8002",
                "--planner-base-url",
                "https://model.example/v1",
                "--planner-model",
                "later",
                "--db-path",
                queue.path,
                "--once",
            ]
        )
        == 0
    )


def test_cached_uncertain_api_submission_cannot_resume_as_a_fresh_request(queue, clock):
    paper = submit(queue)
    manager, planner, api = Manager(), Planner(), API()
    run_once(queue, manager, planner, lambda: api, "worker")
    event = {"type": "api_request", "id": "1", "method": "POST", "path": "/jobs", "body": {}}
    io = AgentIO(queue)
    io.response(manager.job["id"], 1, event, allow_new=True)
    io.save_response(
        manager.job["id"], "1", {"type": "api_response", "id": "1", "status_code": 502, "body": {}}
    )
    manager.lines = [event]
    clock[0] += 3
    run_once(queue, manager, planner, lambda: api, "restarted-worker")
    assert queue.get(paper["id"])["error"] == "verification_api_outcome_unknown"
    assert queue.get(paper["id"])["status"] == "failed"
    assert api.calls == []
    assert manager.cancellations == 1
    assert response_message_id("1") not in manager.input


def test_cleanup_reconciliation_retries_a_cancel_that_was_unavailable(queue, clock):
    paper = submit(queue)
    manager, planner = Manager(), Planner()
    manager.lines = [{"type": "bad-agent-message"}]
    original, attempts = manager.cancel, []

    def cancel(job_id):
        attempts.append(job_id)
        if len(attempts) == 1:
            raise ResourceManagerError("unavailable")
        return original(job_id)

    manager.cancel = cancel
    run_once(queue, manager, planner, API, "worker")
    assert queue.get(paper["id"])["status"] == "failed"
    assert queue.get(paper["id"])["cleanup_required"] is True
    clock[0] += 3
    run_once(queue, manager, planner, API, "cleanup-worker")
    assert queue.get(paper["id"])["status"] == "failed"
    assert queue.get(paper["id"])["cleanup_required"] is False
    assert len(attempts) == 2
    assert queue.retry(paper["id"])["status"] == "pending_verification"
