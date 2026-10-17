import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from paper_library.models import Paper, PaperSubmission, StoredPaperSubmission
from paper_library.queue import POLL_SECONDS, PaperQueue, QueueConflict


@pytest.fixture
def queue(tmp_path):
    return PaperQueue(tmp_path / "papers.sqlite3")


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("paper_library.queue.time.time", lambda: now[0])
    return now


def submit(queue, title="A paper"):
    return queue.submit(PaperSubmission(title=title, paper_text="Unchanged paper contents."))


def plan(**changes):
    return {
        "decision": "launch",
        "verification_requirements": [{"from_paper": "quoted"}],
        **changes,
    }


def compute_job(**changes):
    return {"id": str(uuid4()), "status": "running", **changes}


def report():
    return {"verdict": "inconclusive", "summary": "More evidence is needed.", "evidence": []}


def owned_plan(queue, **claim_options):
    paper = submit(queue)
    claim = queue.claim_for_verification("worker", **claim_options)
    queue.save_plan(paper["id"], claim["lease_token"], plan())
    return claim


def raw_row(queue, paper_id):
    with sqlite3.connect(queue.path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())


def test_plain_submission_persists_and_lists_oldest_first(queue):
    papers = [submit(queue, f"Paper {index}") for index in range(4)]
    assert queue.list(2, 1) == {"papers": papers[1:3], "total": 4, "limit": 2, "offset": 1}
    assert queue.get(papers[0]["id"]) == papers[0]
    assert PaperQueue(queue.path).get(papers[0]["id"]) == papers[0]
    assert "verification_requirements" not in papers[0]["submission"]
    assert queue.get(str(uuid4())) is None
    assert queue.list(10, 10)["papers"] == []


def test_mutated_source_models_are_revalidated_before_storage(queue):
    invalid = PaperSubmission(title="A paper", paper_text="Text").model_copy(update={"title": " "})
    with pytest.raises(ValidationError):
        queue.submit(invalid)
    assert queue.list(10, 0)["total"] == 0


def test_verification_claim_is_fifo_and_private_keys_never_appear_publicly(queue):
    papers = [submit(queue, f"Paper {index}") for index in range(3)]
    claims = [queue.claim_for_verification(f"worker-{index}") for index in range(3)]
    assert [claim["id"] for claim in claims] == [paper["id"] for paper in papers]
    assert all(claim["status"] == "planning" for claim in claims)
    assert len({claim["lease_token"] for claim in claims}) == 3
    assert len({claim["compute_key"] for claim in claims}) == 3
    for claim in claims:
        UUID(claim["compute_key"])
    assert queue.claim_for_verification("another-worker") is None
    for paper in queue.list(10, 0)["papers"]:
        assert (
            not {"lease_token", "lease_until", "compute_key", "dispatch_key", "next_poll_at"}
            & paper.keys()
        )
        assert paper["compute_job_id"] is None
        Paper.model_validate(paper)


def test_concurrent_workers_cannot_own_the_same_verification(queue):
    paper = submit(queue)
    barrier = Barrier(8)

    def claim(number):
        barrier.wait()
        return queue.claim_for_verification(f"worker-{number}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim, range(8)))
    owned = [result for result in results if result is not None]
    assert len(owned) == 1
    assert owned[0]["id"] == paper["id"]


def test_plan_is_saved_once_before_compute_and_survives_ambiguous_transport(queue, clock):
    paper = submit(queue)
    first = queue.claim_for_verification("worker-a")
    with pytest.raises(QueueConflict, match="plan before"):
        queue.save_compute_job(paper["id"], first["lease_token"], compute_job())
    saved = queue.save_plan(paper["id"], first["lease_token"], plan())
    assert saved["status"] == "planning"
    assert saved["plan"] == plan()
    assert queue.save_plan(paper["id"], first["lease_token"], plan()) == saved
    queue.defer_verification(paper["id"], first["lease_token"], "compute_transport_error")
    assert queue.claim_for_verification("too-soon") is None
    clock[0] += POLL_SECONDS
    recovered = PaperQueue(queue.path).claim_for_verification("replacement")
    assert recovered["compute_key"] == first["compute_key"]
    assert recovered["plan"] == plan()
    assert recovered["compute_job_id"] is None
    with pytest.raises(QueueConflict, match="immutable"):
        queue.save_plan(paper["id"], recovered["lease_token"], plan(changed=True))
    assert queue.get(paper["id"])["plan"] == plan()


def test_compute_updates_cannot_supply_a_plan_report_or_scientific_verdict(queue):
    claim = owned_plan(queue)
    remote = compute_job(
        status="completed",
        plan={"injected": "remote plan"},
        report=report(),
        verdict="verified",
        blocked_reason="A remote scientific decision",
        node_id="node-a",
        sandbox_id="sandbox-a",
    )
    saved = queue.save_compute_job(claim["id"], claim["lease_token"], remote)
    assert saved["status"] == "verifying"
    assert saved["completed_at"] is None
    assert saved["report"] is None
    assert saved["blocked_reason"] is None
    assert saved["plan"] == plan()
    assert saved["compute_job_id"] == remote["id"]
    assert saved["compute_job_status"] == "completed"
    assert saved["node_id"] == "node-a"
    assert saved["sandbox_id"] == "sandbox-a"
    assert raw_row(queue, claim["id"])["lease_token"] == claim["lease_token"]


@pytest.mark.parametrize(
    "status", ["queued", "provisioning", "running", "completed", "failed", "cancelled"]
)
def test_every_generic_compute_status_remains_separate_from_library_outcome(queue, status):
    claim = owned_plan(queue)
    saved = queue.save_compute_job(claim["id"], claim["lease_token"], compute_job(status=status))
    assert saved["status"] == "verifying"
    assert saved["report"] is None
    assert saved["compute_job_status"] == status
    assert saved["cleanup_required"] is (status in {"queued", "provisioning", "running"})


def test_completed_compute_with_unprocessed_output_is_reclaimed_without_new_create(queue, clock):
    first = owned_plan(queue, lease_seconds=10)
    remote = compute_job(status="completed")
    queue.save_compute_job(first["id"], first["lease_token"], remote)
    clock[0] += 10
    assert queue.get(first["id"])["status"] == "verifying"
    with pytest.raises(QueueConflict):
        queue.heartbeat(first["id"], first["lease_token"])
    resumed = queue.claim_for_verification("output-parser")
    assert resumed["compute_key"] == first["compute_key"]
    assert resumed["compute_job_id"] == remote["id"]
    assert resumed["compute_job_status"] == "completed"
    assert resumed["plan"] == plan()
    assert resumed["report"] is None


def test_expiry_before_compute_response_keeps_same_key_and_saved_plan(queue, clock):
    first = owned_plan(queue, lease_seconds=10)
    clock[0] += 10
    resumed = queue.claim_for_verification("replacement")
    assert resumed["compute_key"] == first["compute_key"]
    assert resumed["compute_job_id"] is None
    assert resumed["plan"] == plan()
    assert resumed["lease_token"] != first["lease_token"]
    assert queue.get(first["id"])["status"] == "planning"


def test_heartbeat_renews_exclusive_verification_ownership(queue, clock):
    first = owned_plan(queue, lease_seconds=10)
    clock[0] += 5
    with pytest.raises(QueueConflict):
        queue.heartbeat(first["id"], "wrong-token")
    queue.heartbeat(first["id"], first["lease_token"], lease_seconds=20)
    clock[0] += 10
    assert queue.claim_for_verification("other-worker") is None
    clock[0] += 10
    assert queue.claim_for_verification("other-worker")["compute_key"] == first["compute_key"]


def test_compute_identity_cannot_change_within_an_attempt(queue):
    claim = owned_plan(queue)
    original = compute_job()
    queue.save_compute_job(claim["id"], claim["lease_token"], original)
    with pytest.raises(ValueError, match="identity cannot change"):
        queue.save_compute_job(claim["id"], claim["lease_token"], compute_job())
    assert queue.get(claim["id"])["compute_job_id"] == original["id"]


@pytest.mark.parametrize(
    "changes", [{"id": "invalid"}, {"status": "verifying"}, {"cleanup_required": "false"}]
)
def test_invalid_compute_metadata_does_not_mutate_library_record(queue, changes):
    claim = owned_plan(queue)
    before = queue.get(claim["id"])
    with pytest.raises(ValidationError):
        queue.save_compute_job(claim["id"], claim["lease_token"], compute_job(**changes))
    assert queue.get(claim["id"]) == before


def test_only_library_finish_writes_verdict_and_keeps_source_and_plan(queue):
    claim = owned_plan(queue)
    remote = compute_job(status="completed")
    queue.save_compute_job(claim["id"], claim["lease_token"], remote)
    finished = queue.finish_verification(claim["id"], claim["lease_token"], report=report())
    assert finished["status"] == "completed"
    assert finished["report"] == report()
    assert finished["plan"] == plan()
    assert finished["submission"] == claim["submission"]
    assert finished["completed_at"] is not None
    assert finished["compute_job_id"] == remote["id"]
    assert finished["cleanup_required"] is False
    assert PaperQueue(queue.path).get(claim["id"]) == finished
    assert queue.claim_for_verification("worker") is None
    with pytest.raises(QueueConflict):
        queue.finish_verification(claim["id"], claim["lease_token"], report=report())


@pytest.mark.parametrize("outcome", ["report", "error"])
def test_terminal_verification_result_is_preserved_while_compute_cleanup_reconciles(
    queue, clock, outcome
):
    claim = owned_plan(queue)
    remote = compute_job(status="running", node_id="node-a", sandbox_id="sandbox-a")
    queue.save_compute_job(claim["id"], claim["lease_token"], remote)
    arguments = (
        {"report": report()} if outcome == "report" else {"error": "verification_agent_failed"}
    )
    finished = queue.finish_verification(claim["id"], claim["lease_token"], **arguments)
    assert finished["cleanup_required"] is True
    assert queue.claim_for_verification("too-soon") is None
    with pytest.raises(QueueConflict):
        queue.retry(claim["id"])
    clock[0] += POLL_SECONDS
    cleanup = queue.claim_for_verification("cleanup-worker")
    assert cleanup["status"] == finished["status"]
    assert cleanup["compute_key"] == claim["compute_key"]
    assert cleanup["report"] == finished["report"]
    reconciled = queue.save_compute_job(
        claim["id"],
        cleanup["lease_token"],
        {**remote, "status": "cancelled", "cleanup_required": False},
    )
    assert reconciled["status"] == finished["status"]
    assert reconciled["report"] == finished["report"]
    assert reconciled["error"] == finished["error"]
    assert reconciled["completed_at"] == finished["completed_at"]
    assert reconciled["plan"] == plan()
    assert reconciled["cleanup_required"] is False
    assert queue.claim_for_verification("another-worker") is None
    if outcome == "error":
        assert queue.retry(claim["id"])["status"] == "pending_verification"
    else:
        with pytest.raises(QueueConflict):
            queue.retry(claim["id"])


def test_judge_can_block_locally_with_saved_requirements_without_any_compute(queue):
    paper = submit(queue)
    claim = queue.claim_for_verification("judge-worker")
    blocked_plan = {"decision": "blocked", "missing_information": ["Pinned dataset revision"]}
    blocked = queue.block_verification(
        paper["id"], claim["lease_token"], "Missing conditions", plan=blocked_plan
    )
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "Missing conditions"
    assert blocked["plan"] == blocked_plan
    assert blocked["compute_job_id"] is None
    assert blocked["report"] is None
    assert blocked["cleanup_required"] is False
    assert queue.retry(paper["id"])["status"] == "pending_verification"


def test_retry_creates_fresh_compute_identity_after_cleanup_and_preserves_historical_ids(queue):
    paper = submit(queue)
    old_manager_id = str(uuid4())
    with sqlite3.connect(queue.path) as db:
        db.execute(
            "UPDATE papers SET resource_manager_job_id=?,resource_manager_status='failed' "
            "WHERE id=?",
            (old_manager_id, paper["id"]),
        )
    claim = queue.claim_for_verification("worker")
    queue.save_plan(paper["id"], claim["lease_token"], plan())
    remote = compute_job(status="failed")
    queue.save_compute_job(paper["id"], claim["lease_token"], remote)
    queue.finish_verification(paper["id"], claim["lease_token"], error="compute_failed")
    retried = queue.retry(paper["id"])
    assert retried["submission"] == paper["submission"]
    assert retried["resource_manager_job_id"] == old_manager_id
    assert retried["resource_manager_status"] == "failed"
    assert retried["compute_job_id"] is None
    assert retried["compute_job_status"] is None
    assert retried["plan"] is None
    assert queue.claim_for_verification("retry-worker")["compute_key"] != claim["compute_key"]


def test_transport_deferral_preserves_plan_job_and_key_but_never_sets_terminal_verdict(
    queue, clock
):
    claim = owned_plan(queue)
    remote = compute_job()
    queue.save_compute_job(claim["id"], claim["lease_token"], remote)
    deferred = queue.defer_verification(
        claim["id"], claim["lease_token"], "compute_transport_error"
    )
    assert deferred["status"] == "verifying"
    assert deferred["report"] is None
    assert deferred["compute_job_id"] == remote["id"]
    assert deferred["plan"] == plan()
    assert deferred["error"] == "compute_transport_error"
    assert raw_row(queue, claim["id"])["compute_key"] == claim["compute_key"]
    clock[0] += POLL_SECONDS
    resumed = queue.claim_for_verification("resumed-worker")
    assert resumed["compute_job_id"] == remote["id"]
    assert resumed["compute_key"] == claim["compute_key"]


def test_pending_ingress_and_due_resumptions_are_fair(queue, clock):
    first = owned_plan(queue)
    queue.defer_verification(first["id"], first["lease_token"], "transport_error")
    earlier_pending = submit(queue, "Already waiting")
    clock[0] += POLL_SECONDS + 1
    for index in range(4):
        submit(queue, f"New paper {index}")
    assert queue.claim_for_verification("first-worker")["id"] == earlier_pending["id"]
    assert queue.claim_for_verification("second-worker")["id"] == first["id"]


@pytest.mark.parametrize("operation", ["plan", "compute", "finish", "block", "defer", "heartbeat"])
def test_expired_owner_cannot_change_verification(queue, clock, operation):
    claim = owned_plan(queue, lease_seconds=10)
    clock[0] += 10
    calls = {
        "plan": lambda: queue.save_plan(claim["id"], claim["lease_token"], plan()),
        "compute": lambda: queue.save_compute_job(claim["id"], claim["lease_token"], compute_job()),
        "finish": lambda: queue.finish_verification(
            claim["id"], claim["lease_token"], report=report()
        ),
        "block": lambda: queue.block_verification(claim["id"], claim["lease_token"], "Unavailable"),
        "defer": lambda: queue.defer_verification(
            claim["id"], claim["lease_token"], "transport_error"
        ),
        "heartbeat": lambda: queue.heartbeat(claim["id"], claim["lease_token"]),
    }
    with pytest.raises(QueueConflict):
        calls[operation]()
    assert raw_row(queue, claim["id"])["lease_token"] is None
    assert queue.get(claim["id"])["status"] == "planning"
    assert queue.get(claim["id"])["plan"] == plan()


def test_invalid_outcomes_and_unsafe_error_text_are_rejected(queue):
    claim = owned_plan(queue)
    with pytest.raises(ValueError):
        queue.finish_verification(claim["id"], claim["lease_token"])
    with pytest.raises(ValueError):
        queue.finish_verification(
            claim["id"], claim["lease_token"], report=report(), error="failure"
        )
    with pytest.raises(ValueError, match="safe error code"):
        queue.finish_verification(claim["id"], claim["lease_token"], error="Bearer private-key")
    with pytest.raises(ValidationError):
        queue.finish_verification(claim["id"], claim["lease_token"], report={"verdict": "probably"})
    assert queue.get(claim["id"])["report"] is None


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_startups_migrate_original_schema_without_losing_source(tmp_path, attempt):
    path = tmp_path / f"legacy-{attempt}.sqlite3"
    paper_id = str(uuid4())
    source = {"title": "Old paper", "paper_text": "Source content."}
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE papers(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
               id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,submission TEXT NOT NULL)"""
        )
        db.execute(
            "INSERT INTO papers(id,created_at,submission) VALUES(?,?,?)",
            (paper_id, "2026-10-17T00:00:00+00:00", json.dumps(source)),
        )
    barrier = Barrier(4)

    def start(_):
        barrier.wait()
        return PaperQueue(path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        queues = list(executor.map(start, range(4)))
    stored = queues[0].get(paper_id)
    assert stored["submission"] == source
    assert stored["compute_job_id"] is None
    assert stored["status"] == "pending_verification"
    assert queues[0].claim_for_verification("worker")["id"] == paper_id
    assert queues[1].claim_for_verification("other-worker") is None


def test_legacy_manifest_plan_and_cleanup_data_remain_readable_without_relaunch(queue):
    paper = submit(queue)
    legacy = {
        "title": "Historical paper",
        "paper_text": "Source",
        "verification_requirements": [{"historical_custom_shape": "preserved"}],
    }
    old_plan = {"historical_plan": {"custom_condition": [1, True, None]}}
    old_manager_id = str(uuid4())
    with sqlite3.connect(queue.path) as db:
        db.execute(
            """UPDATE papers SET submission=?,plan=?,status='verifying',node_id='old-node',
               sandbox_id='old-sandbox',resource_manager_job_id=?,resource_manager_status='verifying',
               dispatch_key=?,lease_until=NULL WHERE id=?""",
            (json.dumps(legacy), json.dumps(old_plan), old_manager_id, str(uuid4()), paper["id"]),
        )
    stored = PaperQueue(queue.path).get(paper["id"])
    assert stored["submission"] == legacy
    assert stored["plan"] == old_plan
    assert stored["node_id"] == "old-node"
    assert stored["sandbox_id"] == "old-sandbox"
    assert stored["resource_manager_job_id"] == old_manager_id
    assert stored["compute_job_id"] is None
    assert queue.claim_for_verification("new-worker") is None
    assert StoredPaperSubmission.model_validate(legacy).verification_requirements == [
        {"historical_custom_shape": "preserved"}
    ]
    Paper.model_validate(stored)


def test_legacy_direct_execution_expiry_retains_cleanup_guard(queue, clock):
    paper = submit(queue)
    with sqlite3.connect(queue.path) as db:
        db.execute(
            """UPDATE papers SET status='verifying',lease_token='old-token',lease_until=?,
               allocation_token='old-token',sandbox_id='old-sandbox' WHERE id=?""",
            (clock[0] + 10, paper["id"]),
        )
    assert queue.claim_for_verification("new-worker") is None
    clock[0] += 10
    stored = queue.get(paper["id"])
    assert stored["status"] == "failed"
    assert stored["cleanup_required"] is True
    with pytest.raises(QueueConflict):
        queue.retry(paper["id"])
