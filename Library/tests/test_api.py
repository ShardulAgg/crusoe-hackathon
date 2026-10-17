from datetime import datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from paper_library.main import app
from paper_library.models import VerificationReport


@pytest.fixture
def database(monkeypatch, tmp_path):
    path = tmp_path / "library.sqlite3"
    monkeypatch.setenv("LIBRARY_DB_PATH", str(path))
    monkeypatch.delenv("LIBRARY_API_KEY", raising=False)
    return path


@pytest.fixture
def client(database):
    with TestClient(app) as connection:
        yield connection


def paper(**changes):
    return {
        "title": "A paper awaiting verification",
        "paper_url": "https://example.org/papers/research.pdf",
        "submitted_by": "research-agent",
        **changes,
    }


def test_submit_then_retrieve_paper(client):
    payload = paper(authors=["Ada Researcher"], notes="Please check Table 2.")
    response = client.post("/papers", json=payload)

    assert response.status_code == 202
    queued = response.json()
    UUID(queued["id"])
    assert datetime.fromisoformat(queued["created_at"].replace("Z", "+00:00")).tzinfo
    assert queued["status"] == "pending_verification"
    assert queued["submission"] == {**payload, "paper_text": None}
    assert response.headers["location"] == f"/papers/{queued['id']}"
    assert client.get(response.headers["location"]).json() == queued


def test_text_submission_and_normalized_title(client):
    response = client.post(
        "/papers",
        json={
            "title": "  Text-only paper  ",
            "paper_text": "A complete paper in plain text.",
        },
    )

    assert response.status_code == 202
    submission = response.json()["submission"]
    assert submission["title"] == "Text-only paper"
    assert submission["paper_text"] == "A complete paper in plain text."
    assert submission["paper_url"] is None
    assert submission["authors"] == []
    assert "verification_requirements" not in submission
    assert response.json()["plan"] is None


def test_link_and_text_can_be_submitted_together(client):
    response = client.post("/papers", json=paper(paper_text="Snapshot of the submitted paper."))

    assert response.status_code == 202
    assert response.json()["submission"]["paper_text"] == "Snapshot of the submitted paper."


def test_queue_is_empty_initially(client):
    assert client.get("/papers").json() == {
        "papers": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


def test_papers_are_listed_in_submission_order_with_pagination(client):
    submissions = [
        client.post("/papers", json=paper(title=f"Paper {index}")).json() for index in range(4)
    ]

    response = client.get("/papers", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    page = response.json()
    assert page == {"papers": submissions[1:3], "total": 4, "limit": 2, "offset": 1}
    assert client.get("/papers").json()["papers"] == submissions
    assert client.get("/papers?offset=4").json()["papers"] == []


def test_submission_survives_api_restart(database):
    with TestClient(app) as first:
        queued = first.post("/papers", json=paper()).json()

    with TestClient(app) as restarted:
        assert restarted.get(f"/papers/{queued['id']}").json() == queued
        assert restarted.get("/papers").json()["papers"] == [queued]


@pytest.mark.parametrize(
    "payload",
    [
        {"paper_url": "https://example.org/paper"},
        {"title": "Missing source"},
        paper(title=" \n\t "),
        paper(title="x" * 501),
        {"title": "Empty text", "paper_text": " \n "},
        paper(paper_text="x" * 1_000_001),
        paper(paper_url="not a URL"),
        paper(paper_url="ftp://example.org/paper.pdf"),
        paper(paper_url="https://user:password@example.org/paper.pdf"),
        paper(authors=[" "]),
        paper(authors=["x" * 201]),
        paper(authors=["Author"] * 101),
        paper(submitted_by=" "),
        paper(notes=" "),
        paper(notes="x" * 10_001),
        paper(status="verified"),
    ],
)
def test_invalid_submission_is_rejected_without_enqueueing(client, payload):
    assert client.post("/papers", json=payload).status_code == 422
    assert client.get("/papers").json()["total"] == 0


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=101", "offset=-1", "offset=9223372036854775808", "limit=bad"]
)
def test_invalid_pagination_is_rejected(client, query):
    assert client.get(f"/papers?{query}").status_code == 422


def test_unknown_paper_returns_not_found(client):
    assert client.get(f"/papers/{uuid4()}").status_code == 404


def test_extracted_requirements_are_visible_in_plan_while_paper_remains_prose(client):
    source = "Run the fixed workload and require at least 1000 output tokens per second."
    created_response = client.post(
        "/papers", json={"title": "Throughput study", "paper_text": source}
    )
    assert created_response.status_code == 202
    created = created_response.json()
    requirement = {
        "id": "throughput-check",
        "description": "Check the throughput requirement extracted from the paper.",
        "procedure": ["Submit the fixed workload and compare its measured throughput."],
        "conditions": {"workload": "fixed"},
        "acceptance_criteria": [
            {
                "metric": "output_tokens_per_second",
                "operator": "gte",
                "expected": 1000,
                "unit": "tokens/s",
            }
        ],
        "source_quotes": [source],
    }
    selected = {
        "decision": "blocked",
        "rationale": "The paper does not pin its benchmark repository.",
        "verification_requirements": [requirement],
        "missing_information": ["Pinned benchmark repository"],
    }
    claimed = client.app.state.queue.claim_for_verification("verification-worker")
    client.app.state.queue.save_plan(created["id"], claimed["lease_token"], selected)
    client.app.state.queue.block_verification(
        created["id"], claimed["lease_token"], "missing_benchmark"
    )
    published = client.get(f"/papers/{created['id']}").json()
    assert published["plan"] == selected
    assert published["compute_job_id"] is None
    assert published["compute_job_status"] is None
    assert published["plan"]["verification_requirements"] == [requirement]
    assert published["submission"] == created["submission"]
    assert published["submission"]["paper_text"] == source
    assert "verification_requirements" not in published["submission"]

    retried = client.post(f"/papers/{created['id']}/retry")
    assert retried.status_code == 202
    assert retried.json()["plan"] is None
    assert retried.json()["compute_job_id"] is None
    assert retried.json()["compute_job_status"] is None
    assert retried.json()["submission"] == created["submission"]


def test_compute_progress_cannot_set_library_report_or_override_its_plan(client):
    created = client.post("/papers", json=paper()).json()
    claimed = client.app.state.queue.claim_for_verification("verification-worker")
    assert client.get("/papers?status=planning").json()["total"] == 1
    local_plan = {"decision": "launch", "rationale": "Library selected a small coordinator."}
    client.app.state.queue.save_plan(created["id"], claimed["lease_token"], local_plan)
    compute_job_id = str(uuid4())
    client.app.state.queue.save_compute_job(
        created["id"],
        claimed["lease_token"],
        {
            "id": compute_job_id,
            "status": "running",
            "node_id": "execution-node",
            "sandbox_id": "generic-compute-sandbox",
            "plan": {"decision": "blocked", "rationale": "A remote manager cannot judge papers."},
            "report": {"verdict": "verified", "summary": "A compute exit is not verification."},
        },
    )
    progress = client.get(f"/papers/{created['id']}").json()
    assert progress["status"] == "verifying"
    assert progress["compute_job_id"] == compute_job_id
    assert progress["compute_job_status"] == "running"
    assert progress["resource_manager_job_id"] is None
    assert progress["resource_manager_status"] is None
    assert progress["plan"] == local_plan
    assert progress["report"] is None
    assert "lease_token" not in progress
    assert "lease_until" not in progress
    assert "dispatch_key" not in progress
    assert "compute_key" not in progress
    client.app.state.queue.save_compute_job(
        created["id"],
        claimed["lease_token"],
        {"id": compute_job_id, "status": "completed", "exit_code": 0},
    )
    compute_finished = client.get(f"/papers/{created['id']}").json()
    assert compute_finished["compute_job_status"] == "completed"
    assert compute_finished["status"] == "verifying"
    assert compute_finished["report"] is None
    report = VerificationReport(verdict="inconclusive", summary="Missing comparison data.")
    client.app.state.queue.finish_verification(created["id"], claimed["lease_token"], report=report)
    completed = client.get(f"/papers/{created['id']}").json()
    assert completed["report"] == report.model_dump()
    assert completed["compute_job_status"] == "completed"
    assert completed["node_id"] == "execution-node"
    assert completed["sandbox_id"] == "generic-compute-sandbox"
    assert client.get("/papers?status=pending_verification").json()["total"] == 0
    assert client.get("/papers?status=completed").json()["total"] == 1
    assert client.get("/papers?status=unsupported").status_code == 422


def test_planning_block_and_retry_are_visible_through_api(client):
    created = client.post("/papers", json=paper()).json()
    claimed = client.app.state.queue.claim_for_verification("verification-worker")
    plan = {"decision": "blocked", "rationale": "The paper needs an unavailable capability."}
    client.app.state.queue.save_plan(created["id"], claimed["lease_token"], plan)
    client.app.state.queue.block_verification(
        created["id"], claimed["lease_token"], "unsupported_requirements"
    )
    blocked = client.get(f"/papers/{created['id']}").json()
    assert blocked["plan"] == plan
    assert blocked["blocked_reason"] == "unsupported_requirements"
    assert client.get("/papers?status=blocked").json()["total"] == 1
    retried = client.post(f"/papers/{created['id']}/retry")
    assert retried.status_code == 202
    assert retried.json()["status"] == "pending_verification"
    assert retried.json()["plan"] is None
    assert retried.json()["blocked_reason"] is None
    assert client.post(f"/papers/{created['id']}/retry").status_code == 409
    assert client.post(f"/papers/{uuid4()}/retry").status_code == 404


def test_configured_key_protects_submission_and_queue(database, monkeypatch):
    monkeypatch.setenv("LIBRARY_API_KEY", "test-library-key")
    correct = {"Authorization": "Bearer test-library-key"}

    with TestClient(app) as secured:
        for headers in ({}, {"Authorization": "Bearer wrong-key"}):
            assert secured.get("/papers", headers=headers).status_code == 401
            assert secured.post("/papers", headers=headers, json=paper()).status_code == 401

        created = secured.post("/papers", headers=correct, json=paper())
        assert created.status_code == 202
        location = created.headers["location"]
        assert secured.get(location).status_code == 401
        assert secured.post(location + "/retry").status_code == 401
        assert secured.get(location, headers=correct).json() == created.json()
        assert secured.get("/papers", headers=correct).json()["total"] == 1
        assert secured.get("/health").json() == {"status": "ok"}
        assert secured.get("/docs").status_code == 200
        assert secured.get("/openapi.json").status_code == 200
