import threading
import time

import pytest

from resource_manager.queue import ComputeQueue
from resource_manager.worker import run_once


def execute(queue, body, catalog, fake_docker, mode="echo", closed=True):
    job = queue.submit(body, mode, catalog)
    queue.put_stdin(job["id"], {"id": "opaque", "line": "this is opaque text"})
    if closed:
        queue.close_stdin(job["id"])
    runtime = fake_docker(catalog.resources[0], mode)
    assert run_once(queue, catalog, "worker", runtime_factory=lambda _: runtime)
    return queue.get(job["id"]), runtime


def test_hardened_command_and_opaque_echo(queue, body, catalog, fake_docker):
    job, runtime = execute(queue, body, catalog, fake_docker)
    assert job["status"] == "completed" and job["exit_code"] == 0
    assert runtime.cleanup_confirmed
    assert queue.stdout(job["id"])["lines"] == [{"sequence": 1, "line": "this is opaque text"}]
    calls = fake_docker.calls()
    create = next(args for args in calls if args[0] == "create")
    for expected in (
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=128",
        "--memory=512m",
        "--memory-swap=512m",
        "--cpus=1.0",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
    ):
        assert expected in create
    assert create[-1] == "sha256:" + "a" * 64
    for forbidden in ("--volume", "--mount", "-v", "--env", "-e", "--privileged"):
        assert forbidden not in create
    assert calls[-1] == ["rm", "--force", job["sandbox_id"]]
    assert not (fake_docker.root / "created").exists()


def test_trailing_nonnewline_output_is_retained(queue, body, catalog, fake_docker):
    job, _ = execute(queue, body, catalog, fake_docker, "trailing")
    assert job["status"] == "completed"
    assert queue.stdout(job["id"])["lines"][0]["line"] == "opaque trailing output"


@pytest.mark.parametrize(
    "mode,error",
    [
        ("nonzero", "process_exited_nonzero"),
        ("stdout-limit", "stdout_output_limit"),
        ("stderr-limit", "stderr_output_limit"),
        ("bad-utf8", "stdout_not_utf8"),
        ("control", "stdout_output_limit"),
        ("volumes", "image_declares_disallowed_volumes"),
        ("missing-image", "configured_image_not_installed"),
        ("daemon-fail", "docker_daemon_unavailable"),
    ],
)
def test_generic_failures_are_bounded_and_clean(queue, body, catalog, fake_docker, mode, error):
    job, runtime = execute(queue, body, catalog, fake_docker, mode)
    assert job["status"] == "failed" and job["error"] == error
    assert runtime.cleanup_confirmed and not job["cleanup_required"]
    if mode == "nonzero":
        assert job["exit_code"] == 7
    if mode in {"volumes", "missing-image", "daemon-fail"}:
        assert not any(call[0] == "create" for call in fake_docker.calls())


def test_timeout_even_when_silent(queue, body, catalog, fake_docker):
    start = time.monotonic()
    job, runtime = execute(
        queue, body.model_copy(update={"timeout_seconds": 1}), catalog, fake_docker, "silent"
    )
    assert time.monotonic() - start < 5
    assert job["error"] == "compute_timeout" and runtime.cleanup_confirmed
    assert queue.resources(catalog)[0]["cpus"] == 2


def test_failed_cleanup_retains_capacity(queue, body, catalog, fake_docker):
    job, runtime = execute(queue, body, catalog, fake_docker, "cleanup-fail")
    assert job["status"] == "failed"
    assert job["cleanup_required"] and not runtime.cleanup_confirmed
    assert job["error"] == "container_cleanup_unconfirmed"
    assert queue.resources(catalog)[0]["cpus"] == 1
    assert "sensitive" not in str(job)


def test_uncertain_create_never_starts_and_holds_capacity(queue, body, catalog, fake_docker):
    job, runtime = execute(
        queue, body.model_copy(update={"timeout_seconds": 1}), catalog, fake_docker, "slow-create"
    )
    assert job["cleanup_required"] and not runtime.cleanup_confirmed
    assert not any(call[0] == "start" for call in fake_docker.calls())
    assert any(call[0] == "rm" for call in fake_docker.calls())


def test_cancel_during_silent_execution_cleans_and_releases(queue, body, catalog, fake_docker):
    job = queue.submit(body, "cancel", catalog)
    runtime = fake_docker(catalog.resources[0], "silent")
    thread = threading.Thread(target=lambda: run_once(queue, catalog, "worker", lambda _: runtime))
    thread.start()
    deadline = time.monotonic() + 5
    while queue.get(job["id"])["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert queue.cancel(job["id"])["cleanup_required"]
    thread.join(timeout=5)
    assert not thread.is_alive()
    result = queue.get(job["id"])
    assert result["status"] == "cancelled" and not result["cleanup_required"]
    assert runtime.cleanup_confirmed


def test_stdin_eof_waits_until_queued_messages_are_delivered(queue, body, catalog, fake_docker):
    job = queue.submit(body, "eof", catalog)
    for i in range(20):
        queue.put_stdin(job["id"], {"id": str(i), "line": f"line {i}"})
    queue.close_stdin(job["id"])
    runtime = fake_docker(catalog.resources[0])
    run_once(queue, catalog, "worker", lambda _: runtime)
    assert queue.get(job["id"])["status"] == "completed"
    assert [line["line"] for line in queue.stdout(job["id"])["lines"]] == [
        f"line {i}" for i in range(20)
    ]


def test_old_owner_cleanup_can_release_expired_allocation(queue, body, catalog):
    job = queue.submit(body, "expired", catalog)

    class Runtime:
        cleanup_confirmed = True

        def run(self, job, token, store):
            with store.connection() as db:
                db.execute("UPDATE compute_jobs SET lease_until=0")
            store.get(job["id"])
            return 0

    run_once(queue, catalog, "worker", lambda _: Runtime())
    assert queue.get(job["id"])["status"] == "failed"
    assert not queue.get(job["id"])["cleanup_required"]
    assert queue.resources(catalog)[0]["cpus"] == 2
    assert ComputeQueue(queue.path).get(job["id"])["error"] == "compute_lease_expired"


def test_preflight_pins_tag_and_rejects_image_volumes(resource, fake_docker):
    runtime = fake_docker(resource)
    runtime.check_ready()
    assert runtime.image_id == "sha256:" + "a" * 64
    assert fake_docker.calls()[-1] == ["image", "inspect", "trusted-runtime:local"]


def test_factory_failure_releases_reserved_capacity_without_execution(queue, body, catalog):
    job = queue.submit(body, "factory", catalog)

    def fail(resource):
        raise ValueError("private operator diagnostics")

    assert run_once(queue, catalog, "worker", fail)
    result = queue.get(job["id"])
    assert result["error"] == "compute_execution_failed" and not result["cleanup_required"]
    assert queue.resources(catalog)[0]["cpus"] == 2


def test_sigterm_cleans_container_and_restores_handler(
    queue, body, catalog, fake_docker, monkeypatch
):
    import os
    import signal

    from resource_manager import worker

    job = queue.submit(body, "signal", catalog)
    runtime = fake_docker(catalog.resources[0], "silent")
    previous = signal.getsignal(signal.SIGTERM)
    real_run_once = worker.run_once
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda store, inventory, worker_id: real_run_once(
            store, inventory, worker_id, lambda _: runtime
        ),
    )
    monkeypatch.setattr(worker.ResourceCatalog, "load", lambda _: catalog)

    def terminate_when_running():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if queue.get(job["id"])["status"] == "running":
                os.kill(os.getpid(), signal.SIGTERM)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=terminate_when_running)
    thread.start()
    assert worker.main(["--once", "--db-path", queue.path]) == 130
    thread.join(timeout=5)
    assert signal.getsignal(signal.SIGTERM) == previous
    assert runtime.cleanup_confirmed
    result = queue.get(job["id"])
    assert result["status"] == "failed" and result["error"] == "worker_interrupted"
    assert not result["cleanup_required"]


def test_short_line_flood_does_not_postpone_deadline(queue, body, catalog, fake_docker):
    start = time.monotonic()
    job, runtime = execute(
        queue, body.model_copy(update={"timeout_seconds": 1}), catalog, fake_docker, "short-lines"
    )
    assert job["error"] == "compute_timeout"
    assert time.monotonic() - start < 5 and runtime.cleanup_confirmed
