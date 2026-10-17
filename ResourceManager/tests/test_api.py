from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from resource_manager.main import app
from resource_manager.resource_catalog import ResourceCatalog


def post(client, body, key="request"):
    return client.post("/jobs", headers={"Idempotency-Key": key}, json=body.model_dump())


def test_generic_submission_idempotency_and_restart(client, body):
    first = post(client, body)
    assert first.status_code == 202
    assert first.headers["Location"] == f"/jobs/{first.json()['id']}"
    assert post(client, body).json() == first.json()
    changed = body.model_copy(update={"cpus": 0.5})
    assert post(client, changed).status_code == 409
    assert client.get("/jobs").json()["total"] == 1
    assert set(first.json()) == {
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
    }
    assert first.json()["status"] == "queued"
    with TestClient(app) as restarted:
        assert post(restarted, body).json() == first.json()


def test_concurrent_post_same_key_one_row(client, body):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: post(client, body).json(), range(12)))
    assert len({item["id"] for item in results}) == 1
    assert client.get("/jobs").json()["total"] == 1


@pytest.mark.parametrize(
    "extra",
    ["paper_id", "submission", "report", "plan", "image", "docker_context", "command", "env"],
)
def test_domain_or_execution_fields_are_not_accepted(client, body, extra):
    response = client.post(
        "/jobs",
        headers={"Idempotency-Key": "key"},
        json={**body.model_dump(), extra: "secret-value"},
    )
    assert response.status_code == 422
    assert "secret-value" not in response.text


@pytest.mark.parametrize("key", [None, "", "   ", "x" * 201])
def test_idempotency_key_required_and_bounded(client, body, key):
    headers = {} if key is None else {"Idempotency-Key": key}
    assert client.post("/jobs", headers=headers, json=body.model_dump()).status_code == 422


def test_resources_public_quotas_and_liveness(client):
    resources = client.get("/resources").json()["resources"]
    assert resources[0]["node_id"] == "node-a"
    assert resources[0]["max_concurrent_jobs"] == 2
    assert "image" not in resources[0] and "docker_context" not in resources[0]
    queue = app.state.queue
    with queue.connection() as db:
        db.execute("UPDATE compute_nodes SET last_seen=0")
    assert client.get("/resources").json() == {"resources": []}
    assert client.get("/nodes").json()["nodes"][0]["status"] == "offline"
    assert client.post("/nodes/node-a/heartbeat").json()["status"] == "online"
    assert client.post("/nodes/missing/heartbeat").status_code == 404


def test_opaque_io_close_replay_and_completed_exit_code(client, body):
    job = post(client, body).json()
    path = f"/jobs/{job['id']}"
    message = {"id": "input", "line": "any opaque string: {'not':'agent protocol'}"}
    assert client.post(path + "/stdin", json=message).status_code == 202
    assert client.post(path + "/stdin/close").json() == {"closed": True}
    assert client.post(path + "/stdin/close").status_code == 202
    queue = app.state.queue
    _, token, _ = queue.claim("test", ResourceCatalog.load(app.state.resources_dir))
    queue.mark_running(job["id"], token)
    queue.append_stdout(job["id"], token, "opaque result")
    queue.finish(job["id"], token, exit_code=0)
    assert client.get(path).json()["status"] == "completed"
    assert client.get(path).json()["exit_code"] == 0
    assert client.post(path + "/stdin", json=message).status_code == 202
    assert client.post(path + "/stdin", json={**message, "line": "conflict"}).status_code == 409
    assert client.post(path + "/stdin", json={"id": "new", "line": "later"}).status_code == 409
    assert client.get(path + "/stdout").json() == {
        "lines": [{"sequence": 1, "line": "opaque result"}],
        "next_cursor": 1,
    }
    assert client.get(path + "/stdout?after=1").json() == {"lines": [], "next_cursor": 1}
    assert client.get(path + "/stdout?after=-1").status_code == 422


@pytest.mark.parametrize(
    "message",
    [
        {"id": "", "line": "x"},
        {"id": "input", "line": "two\nlines"},
        {"id": "input", "line": "é" * (1024 * 1024 + 1)},
    ],
)
def test_stdin_validation(client, body, message):
    job = post(client, body).json()
    assert client.post(f"/jobs/{job['id']}/stdin", json=message).status_code == 422


def test_list_queue_filters_and_pagination(client, body):
    jobs = [post(client, body, str(i)).json() for i in range(3)]
    client.post(f"/jobs/{jobs[0]['id']}/cancel")
    listed = client.get(
        "/jobs?status=queued&resource_id=cpu&node_id=node-a&limit=1&offset=1"
    ).json()
    assert listed["total"] == 2 and listed["limit"] == 1 and listed["offset"] == 1
    assert listed["jobs"][0]["id"] == jobs[2]["id"]
    assert client.get("/jobs?status=not-a-status").status_code == 422
    assert client.get("/jobs?limit=201").status_code == 422


def test_api_auth_covers_all_compute_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("RESOURCE_MANAGER_DB_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("RESOURCE_MANAGER_API_KEY", "operator-key")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for method, path in [
            ("GET", "/resources"),
            ("GET", "/nodes"),
            ("GET", "/jobs"),
            ("GET", f"/jobs/{uuid4()}"),
            ("POST", "/nodes/register"),
        ]:
            assert client.request(method, path).status_code == 401
        assert (
            client.get("/nodes", headers={"Authorization": "Bearer operator-key"}).status_code
            == 200
        )


def test_no_verification_routes(client):
    assert client.get("/verification-jobs").status_code == 404
    assert client.get("/papers").status_code == 404
