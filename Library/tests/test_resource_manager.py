import json
from uuid import uuid4

import httpx
import pytest

from paper_library.resource_manager import MAX_RESPONSE_BYTES, ResourceManager, ResourceManagerError

JOB_ID = str(uuid4())


def job(**changes):
    return {"id": JOB_ID, "status": "running", **changes}


def manager(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return ResourceManager("https://manager.example", "private-manager-key", client=client), client


def test_submission_sends_only_compute_requirements_with_stable_idempotency_key():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202, json=job())

    remote, client = manager(handler)
    requested = {
        "resource_id": "cpu-small",
        "node_id": "node-1",
        "cpus": 0.25,
        "memory_mb": 256,
        "scratch_mb": 16,
        "timeout_seconds": 60,
    }
    plan = {
        **requested,
        "verification_requirements": [{"claim": "private paper"}],
        "rationale": "Private scientific reading",
        "decision": "launch",
    }
    with client, remote:
        assert remote.submit(plan, "stable-attempt-key")["id"] == JOB_ID
    assert str(calls[0].url) == "https://manager.example/jobs"
    assert calls[0].headers["Idempotency-Key"] == "stable-attempt-key"
    assert calls[0].headers["Authorization"] == "Bearer private-manager-key"
    assert json.loads(calls[0].content) == requested
    assert "private" not in calls[0].content.decode()


def test_status_check_uses_only_fixed_manager_job_route():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=job())

    remote, client = manager(handler)
    with client, remote:
        assert remote.get(JOB_ID) == job()
    assert calls[0].method == "GET"
    assert str(calls[0].url) == "https://manager.example/jobs/" + JOB_ID


@pytest.mark.parametrize("job_id", ["../nodes", "https://evil.example", "bad", None])
def test_arbitrary_job_routes_are_rejected_before_network(job_id):
    remote, client = manager(lambda request: pytest.fail("Unexpected network request"))
    with client, remote, pytest.raises(ResourceManagerError, match="invalid_job_id"):
        remote.get(job_id)


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "file:///tmp/jobs",
        "https://user:key@manager.example",
        "https://manager.example?key=secret",
        "https://manager.example/#fragment",
        "https://manager.example/../elsewhere",
    ],
)
def test_invalid_manager_configuration_is_rejected(base_url):
    with pytest.raises(ResourceManagerError, match="invalid_configuration"):
        ResourceManager(base_url)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad-id"},
        {"id": None},
        {"status": "unknown"},
        {"status": []},
        {"id": str(uuid4())},
    ],
)
def test_malformed_or_wrong_job_response_is_rejected(changes):
    remote, client = manager(lambda request: httpx.Response(200, json=job(**changes)))
    with client, remote, pytest.raises(ResourceManagerError):
        remote.get(JOB_ID)


@pytest.mark.parametrize("status", [307, 401, 404, 409, 500])
def test_failure_and_redirect_do_not_expose_remote_body_or_follow_location(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="private-manager-key", headers={"Location": "https://evil.example"}
        )

    remote, client = manager(handler)
    with client, remote, pytest.raises(ResourceManagerError) as error:
        remote.get(JOB_ID)
    assert len(calls) == 1
    assert "private-manager-key" not in str(error.value)


@pytest.mark.parametrize("content", [b"[]", b"null", b"not JSON", b'{"id":1,"id":2}', b'{"x":NaN}'])
def test_invalid_or_ambiguous_json_is_rejected(content):
    remote, client = manager(lambda request: httpx.Response(200, content=content))
    with client, remote, pytest.raises(ResourceManagerError, match="invalid_response"):
        remote.get(JOB_ID)


def test_streamed_response_size_is_bounded():
    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(MAX_RESPONSE_BYTES // 65536 + 1):
                yield b"x" * 65536

    remote, client = manager(lambda request: httpx.Response(200, stream=LargeStream()))
    with client, remote, pytest.raises(ResourceManagerError, match="response_too_large"):
        remote.get(JOB_ID)


def test_resource_listing_is_separate_from_generic_jobs():
    resources = [{"id": "cpu-small", "cpus": 2, "memory_mb": 1024}]
    remote, client = manager(lambda request: httpx.Response(200, json={"resources": resources}))
    with client, remote:
        assert remote.resources() == resources


def test_stdio_channel_transports_opaque_lines_without_reading_the_paper():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(
            200, json={"lines": [{"sequence": 1, "line": "opaque output"}], "next_cursor": 1}
        )

    remote, client = manager(handler)
    with client, remote:
        remote.write_stdin(JOB_ID, "message-1", "opaque input")
        assert remote.stdout(JOB_ID)["lines"][0]["line"] == "opaque output"
        remote.close_stdin(JOB_ID)
    assert json.loads(calls[0].content) == {"id": "message-1", "line": "opaque input"}
    assert calls[2].url.path.endswith("/stdin/close")


@pytest.mark.parametrize(
    "output",
    [
        {"lines": [{"sequence": 2, "line": "skipped"}], "next_cursor": 2},
        {"lines": [], "next_cursor": 1},
        {"lines": [{"sequence": 1, "line": None}], "next_cursor": 1},
    ],
)
def test_stdout_cannot_skip_or_invent_events(output):
    remote, client = manager(lambda request: httpx.Response(200, json=output))
    with client, remote, pytest.raises(ResourceManagerError, match="invalid_response"):
        remote.stdout(JOB_ID)


def test_default_client_ignores_environment_proxy_and_redirects(monkeypatch):
    real_client = httpx.Client
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return real_client(
            **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=job()))
        )

    monkeypatch.setattr("paper_library.resource_manager.httpx.Client", create)
    with ResourceManager("https://manager.example") as remote:
        remote.get(JOB_ID)
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
