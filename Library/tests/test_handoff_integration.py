"""Real Library and generic ResourceManager APIs with independent durable state."""

import importlib
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from paper_library.main import app as library_app
from paper_library.resource_catalog import SandboxPlan
from paper_library.resource_manager import ResourceManager
from paper_library.verification_api import VerificationAPI
from paper_library.worker import run_once

TEXT = "The fixed workload uses 1024 input tokens. Throughput is at least 1000 tokens/s."
ALLOCATION_FIELDS = {"resource_id", "node_id", "cpus", "memory_mb", "scratch_mb", "timeout_seconds"}


class Planner:
    def __init__(self):
        self.calls = 0

    def plan(self, paper, catalog):
        self.calls += 1
        assert paper["submission"]["paper_text"] == TEXT
        available = catalog.get("cpu-small").planner_view()
        assert available["node_id"] == "node-1"
        assert "image" not in available
        assert "docker_context" not in available
        return SandboxPlan.model_validate(
            {
                "decision": "launch",
                "rationale": "Minimum API coordination resources.",
                "verification_steps": ["Measure throughput under the paper's stated conditions."],
                "verification_requirements": [
                    {
                        "id": "throughput",
                        "description": "Check the throughput claim.",
                        "procedure": ["Run the fixed benchmark through the verification API."],
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
            }
        )


@pytest.fixture
def services(tmp_path, monkeypatch):
    manager_source = Path(__file__).resolve().parents[2] / "ResourceManager"
    if not manager_source.is_dir():
        pytest.skip("Sibling ResourceManager checkout required for cross-service integration")
    monkeypatch.syspath_prepend(str(manager_source))
    manager_app = importlib.import_module("resource_manager.main").app
    manager_catalog = importlib.import_module("resource_manager.resource_catalog").ResourceCatalog
    library_db, manager_db = tmp_path / "library.sqlite3", tmp_path / "compute.sqlite3"
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "cpu.json").write_text(
        json.dumps(
            {
                "id": "cpu-small",
                "node_id": "node-1",
                "enabled": True,
                "image": "operator-image:local",
                "docker_context": "operator-host",
                "allocation_mode": "on_demand",
                "cpus": 2,
                "memory_mb": 8192,
                "scratch_mb": 64,
                "max_timeout_seconds": 600,
            }
        )
    )
    monkeypatch.setenv("LIBRARY_DB_PATH", str(library_db))
    monkeypatch.delenv("LIBRARY_API_KEY", raising=False)
    monkeypatch.setenv("RESOURCE_MANAGER_DB_PATH", str(manager_db))
    monkeypatch.setenv("RESOURCE_MANAGER_RESOURCES_DIR", str(resources_dir))
    monkeypatch.setenv("RESOURCE_MANAGER_API_KEY", "compute-service-key")
    clock = [time.time()]
    monkeypatch.setattr("paper_library.queue.time.time", lambda: clock[0])

    with TestClient(library_app) as library, TestClient(manager_app) as remote_api:
        response = remote_api.post(
            "/nodes/register",
            headers={"Authorization": "Bearer compute-service-key"},
            json={"id": "node-1", "cpus": 2, "memory_mb": 8192},
        )
        assert response.status_code == 200
        assert library_app.state.queue.path != manager_app.state.queue.path
        yield {
            "library": library,
            "remote": remote_api,
            "store": manager_app.state.queue,
            "catalog": manager_catalog.load(resources_dir),
            "clock": clock,
        }


def connect(remote_api, calls, lose_first_acknowledgement=False):
    def transport(request):
        response = remote_api.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers=dict(request.headers),
        )
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content) if request.content else None,
                "key": request.headers.get("Idempotency-Key"),
                "response": response.json(),
            }
        )
        allocations = [
            call for call in calls if call["method"] == "POST" and call["path"] == "/jobs"
        ]
        if (
            lose_first_acknowledgement
            and request.method == "POST"
            and request.url.path == "/jobs"
            and len(allocations) == 1
        ):
            raise httpx.ReadTimeout("Acknowledgement lost after durable compute acceptance")
        return httpx.Response(response.status_code, content=response.content)

    client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
    return client, ResourceManager("http://compute.test", "compute-service-key", client=client)


def claim_runtime(services, job_id):
    claimed = services["store"].claim("compute-runtime", services["catalog"])
    assert claimed is not None
    job, token, _ = claimed
    assert job["id"] == job_id
    services["store"].mark_running(job_id, token)
    return token


@pytest.mark.parametrize("lose_first_acknowledgement", [False, True])
def test_library_uses_resource_only_compute_and_alone_interprets_verification_result(
    services, lose_first_acknowledgement
):
    calls, verification_calls = [], []
    planner = Planner()
    library, remote_api, store = services["library"], services["remote"], services["store"]
    client, manager = connect(remote_api, calls, lose_first_acknowledgement)

    def verification_endpoint(request):
        verification_calls.append(request)
        return httpx.Response(200, json={"status": "ok"})

    with (
        client,
        manager,
        httpx.Client(transport=httpx.MockTransport(verification_endpoint)) as verification_client,
    ):

        def api_factory():
            return VerificationAPI(
                "http://verification.test", "verification-service-key", verification_client
            )

        paper = library.post(
            "/papers", json={"title": "Throughput study", "paper_text": TEXT}
        ).json()
        assert run_once(library_app.state.queue, manager, planner, api_factory, "library-worker")
        if lose_first_acknowledgement:
            services["clock"][0] += 3
            assert run_once(
                library_app.state.queue, manager, planner, api_factory, "restarted-library-worker"
            )
        progress = library.get("/papers/" + paper["id"]).json()
        job_id = progress["compute_job_id"]
        assert progress["status"] == "verifying"
        assert progress["report"] is None
        assert planner.calls == 1
        allocations = [
            call for call in calls if call["method"] == "POST" and call["path"] == "/jobs"
        ]
        assert len(allocations) == (2 if lose_first_acknowledgement else 1)
        assert len({call["key"] for call in allocations}) == 1
        assert {call["response"]["id"] for call in allocations} == {job_id}
        for call in allocations:
            assert set(call["body"]) == ALLOCATION_FIELDS
            assert TEXT not in json.dumps(call["body"])
            assert paper["id"] not in json.dumps(call["body"])
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM compute_jobs").fetchone()[0] == 1
        assert "compute-service-key" not in json.dumps(progress)

        token = claim_runtime(services, job_id)
        task_row, closed = store.pending_stdin(job_id)
        assert closed is False
        task = json.loads(task_row["line"])
        assert task["type"] == "task"
        assert task["paper"]["id"] == paper["id"]
        assert task["paper"]["submission"]["paper_text"] == TEXT
        assert task["paper"]["plan"]["verification_requirements"][0]["source_quotes"] == [TEXT]
        assert "lease_token" not in task["paper"]
        assert "compute_key" not in task["paper"]
        assert "compute-service-key" not in task_row["line"]
        assert "verification-service-key" not in task_row["line"]
        store.ack_stdin(job_id, task_row["sequence"])

        store.append_stdout(
            job_id,
            token,
            json.dumps(
                {
                    "type": "api_request",
                    "id": "health-check",
                    "method": "GET",
                    "path": "/health",
                    "body": None,
                }
            ),
        )
        services["clock"][0] += 3
        run_once(library_app.state.queue, manager, planner, api_factory, "library-api-broker")
        response_row, _ = store.pending_stdin(job_id)
        assert json.loads(response_row["line"])["status_code"] == 200
        assert len(verification_calls) == 1
        assert str(verification_calls[0].url) == "http://verification.test/health"
        assert verification_calls[0].headers["Authorization"] == "Bearer verification-service-key"
        assert "verification-service-key" not in response_row["line"]
        store.ack_stdin(job_id, response_row["sequence"])

        report = {
            "verdict": "inconclusive",
            "summary": "The claim needs more evidence.",
            "evidence": [],
        }
        store.append_stdout(job_id, token, json.dumps({"type": "result", "report": report}))
        store.finish(job_id, token, exit_code=0)
        generic = manager.get(job_id)
        assert generic["status"] == "completed"
        assert generic["exit_code"] == 0
        assert "report" not in generic
        assert "plan" not in generic
        assert "paper_id" not in generic
        services["clock"][0] += 3
        run_once(library_app.state.queue, manager, planner, api_factory, "library-result-reader")
        completed = library.get("/papers/" + paper["id"]).json()
        assert completed["status"] == "completed"
        assert completed["report"] == report
        assert completed["submission"]["paper_text"] == TEXT
        assert planner.calls == 1
        assert len(verification_calls) == 1  # Replayed stdout reused Library's durable API reply.


def test_successful_generic_compute_without_an_agent_result_does_not_verify_paper(services):
    calls, planner = [], Planner()
    client, manager = connect(services["remote"], calls)
    with client, manager, VerificationAPI("http://unused-verification.test") as api:
        paper = (
            services["library"]
            .post("/papers", json={"title": "Throughput study", "paper_text": TEXT})
            .json()
        )
        run_once(library_app.state.queue, manager, planner, lambda: api, "library-worker")
        progress = services["library"].get("/papers/" + paper["id"]).json()
        job_id = progress["compute_job_id"]
        token = claim_runtime(services, job_id)
        services["store"].finish(job_id, token, exit_code=0)
        services["clock"][0] += 3
        run_once(library_app.state.queue, manager, planner, lambda: api, "library-result-reader")
        generic = manager.get(job_id)
        assert generic["status"] == "completed" and generic["exit_code"] == 0
        rejected = services["library"].get("/papers/" + paper["id"]).json()
        assert rejected["status"] == "failed"
        assert rejected["report"] is None
        assert rejected["error"] == "agent_finished_without_valid_report"
