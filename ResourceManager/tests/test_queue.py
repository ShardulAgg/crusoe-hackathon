import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from resource_manager.queue import ComputeQueue, LeaseLost, OutputLimit, QueueError
from resource_manager.resource_catalog import ResourceCatalog


def submit(queue, body, catalog, key="key"):
    return queue.submit(body, key, catalog)


def test_durable_idempotency_and_conflict(queue, body, catalog):
    job = submit(queue, body, catalog)
    restarted = ComputeQueue(queue.path)
    assert restarted.submit(body, "key", catalog) == job
    changed = body.model_copy(update={"cpus": 0.5})
    with pytest.raises(QueueError, match="different compute"):
        restarted.submit(changed, "key", catalog)
    assert queue.list_jobs()["total"] == 1


def test_concurrent_idempotency_is_one_job(queue, body, catalog):
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(
            pool.map(lambda _: ComputeQueue(queue.path).submit(body, "same", catalog), range(16))
        )
    assert len({job["id"] for job in jobs}) == 1


def test_reservation_atomic_never_oversubscribes(queue, body, catalog):
    for i in range(12):
        submit(queue, body, catalog, str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(lambda i: ComputeQueue(queue.path).claim(str(i), catalog), range(12))
        )
    allocated = [claim for claim in claims if claim]
    assert len(allocated) == 2
    assert sum(claim[0]["cpus"] for claim in allocated) == 2
    assert queue.resources(catalog) == []
    assert queue.list_jobs(status="queued")["total"] == 10


def test_memory_and_slot_limits(queue, body, resource):
    catalog = ResourceCatalog([resource.model_copy(update={"cpus": 8, "memory_mb": 512})])
    for i in range(3):
        submit(queue, body, catalog, str(i))
    assert queue.claim("a", catalog)
    assert queue.claim("b", catalog) is None


def test_offline_and_stale_nodes_wait(queue, body, catalog):
    submit(queue, body, catalog)
    with queue.connection() as db:
        db.execute("UPDATE compute_nodes SET last_seen=0")
    assert queue.resources(catalog) == []
    assert queue.claim("a", catalog) is None
    queue.heartbeat_node("node-a")
    assert queue.claim("a", catalog)


def test_nodes_are_approved_by_inventory(queue, body, catalog):
    queue.register_node({"id": "unapproved", "cpus": 100, "memory_mb": 10000})
    assert {item["node_id"] for item in queue.resources(catalog)} == {"node-a"}
    with pytest.raises(QueueError, match="approved"):
        submit(queue, body.model_copy(update={"node_id": "unapproved"}), catalog)


def test_expired_owner_holds_capacity_until_confirmed_cleanup(queue, body, catalog):
    job = submit(queue, body.model_copy(update={"cpus": 2}), catalog)
    _, token, _ = queue.claim("owner", catalog)
    with queue.connection() as db:
        db.execute("UPDATE compute_jobs SET lease_until=0")
    assert queue.get(job["id"])["cleanup_required"] is True
    assert queue.resources(catalog) == []
    assert not queue.release_capacity(job["id"], "wrong-token")
    with pytest.raises(LeaseLost):
        queue.heartbeat(job["id"], token)
    assert queue.release_capacity(job["id"], token)
    assert queue.resources(catalog)[0]["cpus"] == 2
    assert queue.get(job["id"])["status"] == "failed"


def test_finish_cleanup_failure_holds_capacity(queue, body, catalog):
    job = submit(queue, body, catalog)
    _, token, _ = queue.claim("owner", catalog)
    queue.finish(
        job["id"], token, exit_code=0, error="cleanup_unconfirmed", cleanup_confirmed=False
    )
    stored = queue.get(job["id"])
    assert stored["status"] == "failed" and stored["cleanup_required"]
    assert queue.resources(catalog)[0]["cpus"] == 1
    assert queue.release_capacity(job["id"], token)
    assert queue.resources(catalog)[0]["cpus"] == 2


def test_cancel_queued_and_active_hold_until_cleanup(queue, body, catalog):
    first = submit(queue, body, catalog, "first")
    second = submit(queue, body, catalog, "second")
    queue.cancel(second["id"])
    assert not queue.get(second["id"])["cleanup_required"]
    _, token, _ = queue.claim("owner", catalog)
    cancelled = queue.cancel(first["id"])
    assert cancelled["status"] == "cancelled" and cancelled["cleanup_required"]
    assert queue.heartbeat(first["id"], token) is False
    queue.finish(first["id"], token, error="cancelled", cleanup_confirmed=True)
    assert queue.get(first["id"])["status"] == "cancelled"
    assert not queue.get(first["id"])["cleanup_required"]


def test_input_opaque_idempotent_after_close_and_finish(queue, body, catalog):
    job = submit(queue, body, catalog)
    message = {"id": "task", "line": '{"opaque":"not interpreted"}'}
    assert queue.put_stdin(job["id"], message)["accepted"]
    assert queue.close_stdin(job["id"])["closed"]
    with pytest.raises(QueueError):
        queue.put_stdin(job["id"], {"id": "new", "line": "new"})
    _, token, _ = queue.claim("owner", catalog)
    queue.finish(job["id"], token, exit_code=0)
    assert queue.put_stdin(job["id"], message)["accepted"]
    with pytest.raises(QueueError, match="different line"):
        queue.put_stdin(job["id"], {"id": "task", "line": "different"})


def test_input_total_bounded_and_duplicate_still_replays(queue, body, catalog):
    job = submit(queue, body, catalog)
    message = {"id": "first", "line": "x" * (2 * 1024 * 1024 - 1)}
    queue.put_stdin(job["id"], message)
    for i in range(7):
        queue.put_stdin(job["id"], {"id": str(i), "line": message["line"]})
    with pytest.raises(QueueError) as error:
        queue.put_stdin(job["id"], {"id": "overflow", "line": "x"})
    assert error.value.status_code == 413
    assert queue.put_stdin(job["id"], message)["accepted"]


def test_stdout_pagination_contiguous_and_bounded(queue, body, catalog):
    job = submit(queue, body, catalog)
    _, token, _ = queue.claim("owner", catalog)
    for i in range(70):
        queue.append_stdout(job["id"], token, f"arbitrary {i}")
    page = queue.stdout(job["id"])
    assert [line["sequence"] for line in page["lines"]] == list(range(1, 65))
    assert page["next_cursor"] == 64
    assert queue.stdout(job["id"], 64)["next_cursor"] == 70
    assert queue.stdout(job["id"], 70) == {"lines": [], "next_cursor": 70}
    with pytest.raises(OutputLimit):
        queue.append_stdout(job["id"], token, "\x00" * (2 * 1024 * 1024))
    assert len(json.dumps(page).encode()) < 8 * 1024 * 1024


def test_private_runtime_and_tokens_never_public(queue, body, catalog):
    job = submit(queue, body, catalog)
    _, token, _ = queue.claim("owner", catalog)
    public = json.dumps(queue.get(job["id"]))
    assert token not in public and "trusted-runtime" not in public
    for name in ("lease_token", "allocation_token", "image", "docker_context"):
        assert name not in queue.get(job["id"])


def test_old_papers_tables_are_not_reinterpreted(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE papers (payload TEXT)")
    db.execute("INSERT INTO papers VALUES ('legacy-domain-row')")
    db.commit()
    db.close()
    queue = ComputeQueue(path)
    assert queue.list_jobs()["total"] == 0
    with queue.connection() as db:
        assert db.execute("SELECT payload FROM papers").fetchone()[0] == "legacy-domain-row"


def test_changed_pool_id_still_counts_same_node(queue, body, resource):
    old = ResourceCatalog([resource])
    submit(queue, body.model_copy(update={"cpus": 2}), old)
    queue.claim("a", old)
    new = ResourceCatalog([resource.model_copy(update={"id": "renamed"})])
    assert queue.resources(new) == []
    submit(queue, body.model_copy(update={"resource_id": "renamed"}), new, "new")
    assert queue.claim("b", new) is None


def test_independent_nodes_can_run_in_parallel(queue, body, resource):
    other = resource.model_copy(update={"id": "other", "node_id": "node-b"})
    catalog = ResourceCatalog([resource, other])
    queue.register_node({"id": "node-b", "cpus": 2, "memory_mb": 8192})
    for i in range(2):
        submit(queue, body.model_copy(update={"cpus": 2}), catalog, str(i))
    third = submit(
        queue,
        body.model_copy(update={"resource_id": "other", "node_id": "node-b", "cpus": 2}),
        catalog,
        "third",
    )
    assert queue.claim("a", catalog)
    assert queue.claim("b", catalog)[0]["id"] == third["id"]


def test_node_capacity_cannot_change_under_running_job(queue, body, catalog):
    submit(queue, body, catalog)
    queue.claim("a", catalog)
    with pytest.raises(QueueError, match="capacity"):
        queue.register_node({"id": "node-a", "cpus": 1, "memory_mb": 4096})
