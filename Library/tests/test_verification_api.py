import json
from uuid import uuid4

import httpx
import pytest

from paper_library.verification_api import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, VerificationAPI


def request(path="/health", method="GET", body=None, **changes):
    return {
        "type": "api_request",
        "id": "tool-1",
        "method": method,
        "path": path,
        "body": body,
        **changes,
    }


def broker(handler, **client_options):
    client = httpx.Client(transport=httpx.MockTransport(handler), **client_options)
    return VerificationAPI("https://verifier.example", "host-secret", client), client


def test_fixed_destination_and_host_authentication():
    calls = []

    def handler(outbound):
        calls.append(outbound)
        return httpx.Response(200, json={"status": "ok"})

    bridge, client = broker(handler)
    with client, bridge:
        result = bridge(request())
    assert result == {
        "type": "api_response",
        "id": "tool-1",
        "status_code": 200,
        "body": {"status": "ok"},
    }
    assert str(calls[0].url) == "https://verifier.example/health"
    assert calls[0].headers["Authorization"] == "Bearer host-secret"
    assert calls[0].headers["Accept-Encoding"] == "identity"
    assert calls[0].extensions["timeout"] == {"connect": 5, "read": 30, "write": 10, "pool": 5}
    assert "host-secret" not in json.dumps(result)


def test_default_client_ignores_environment_proxies(monkeypatch):
    original = httpx.Client
    options = []

    def create_client(**kwargs):
        options.append(kwargs)
        return original(
            **kwargs, transport=httpx.MockTransport(lambda outbound: httpx.Response(200, json={}))
        )

    monkeypatch.setenv("HTTPS_PROXY", "https://unwanted-proxy.example")
    monkeypatch.setattr("paper_library.verification_api.httpx.Client", create_client)
    with VerificationAPI("https://verifier.example") as bridge:
        assert bridge(request())["status_code"] == 200
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.org",
        "https://@example.org",
        "https://example.org/path",
        "https://example.org/?x=1",
        "https://example.org/#fragment",
        "https://example.org?",
        "https://example.org#",
        "https://example.org\\evil",
        " https://x",
        "http://",
        "https://example.org:bad",
    ],
)
def test_invalid_base_url_is_rejected(url):
    with pytest.raises(ValueError, match="HTTP"):
        VerificationAPI(url)


@pytest.mark.parametrize(
    "path,method",
    [
        ("https://evil.example/health", "GET"),
        ("//evil.example/health", "GET"),
        ("/worker/claim", "POST"),
        ("/nodes", "GET"),
        ("/nodes/register", "POST"),
        ("/jobs/../health", "GET"),
        ("/jobs?x=1", "POST"),
        ("/health#x", "GET"),
        ("/health%3Fx", "GET"),
        ("/health\\x", "GET"),
        ("/papers", "GET"),
        ("/health", "POST"),
        (f"/jobs/{uuid4()}", "GET"),
        (f"/jobs/{uuid4()}/cancel", "POST"),
    ],
)
def test_blocked_routes_never_reach_network(path, method):
    calls = []
    bridge, client = broker(lambda outbound: calls.append(outbound))
    with client, bridge:
        assert bridge(request(path, method))["status_code"] == 403
    assert calls == []


def test_only_created_jobs_can_be_read_and_canceled():
    job_id = str(uuid4())
    calls = []

    def handler(outbound):
        calls.append((outbound.method, outbound.url.path))
        return httpx.Response(202 if outbound.url.path == "/jobs" else 200, json={"id": job_id})

    bridge, client = broker(handler)
    other, other_client = broker(handler)
    with client, bridge:
        assert (
            bridge(request("/jobs", "POST", {"repository": "https://example.org/repo"}))[
                "status_code"
            ]
            == 202
        )
        assert bridge(request(f"/jobs/{job_id}"))["status_code"] == 200
        assert bridge(request(f"/jobs/{job_id}/cancel", "POST"))["status_code"] == 200
        assert other(request(f"/jobs/{job_id}"))["status_code"] == 403
        assert bridge(request(f"/jobs/{job_id.upper()}"))["status_code"] == 403
    other_client.close()
    assert len(calls) == 3


def test_redirect_is_not_followed_even_for_client_configured_to_follow():
    calls = []

    def handler(outbound):
        calls.append(str(outbound.url))
        return httpx.Response(
            307, headers={"Location": "https://evil.example/steal"}, json={"key": "host-secret"}
        )

    bridge, client = broker(handler, follow_redirects=True)
    with client, bridge:
        result = bridge(request())
    assert result["status_code"] == 307
    assert calls == ["https://verifier.example/health"]
    assert "host-secret" not in json.dumps(result)
    assert "headers" not in result


def test_transport_and_upstream_errors_never_expose_exception_or_response_secrets():
    def timeout(outbound):
        raise httpx.ReadTimeout("host-secret", request=outbound)

    for handler, expected in (
        (timeout, 502),
        (lambda outbound: httpx.Response(401, text="host-secret"), 401),
    ):
        bridge, client = broker(handler)
        with client, bridge:
            result = bridge(request())
        assert result["status_code"] == expected
        assert "host-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "data,status",
    [
        (b"not json", 200),
        (b"{}", 202),
        (b'{"id":"not-a-uuid"}', 202),
        (json.dumps({"id": str(uuid4())}).encode(), 200),
    ],
)
def test_invalid_job_response_fails_closed(data, status):
    bridge, client = broker(lambda outbound: httpx.Response(status, content=data))
    with client, bridge:
        assert bridge(request("/jobs", "POST", {}))["status_code"] == 502


def test_request_body_limit_prevents_network_call():
    calls = []
    bridge, client = broker(lambda outbound: calls.append(outbound))
    with client, bridge:
        result = bridge(request("/jobs", "POST", {"large": "x" * MAX_REQUEST_BYTES}))
    assert result["status_code"] == 413
    assert calls == []


def test_response_body_limit_is_enforced_during_streaming():
    class LargeStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(MAX_RESPONSE_BYTES // 65536 + 1):
                yield b"x" * 65536

    bridge, client = broker(lambda outbound: httpx.Response(200, stream=LargeStream()))
    with client, bridge:
        assert bridge(request())["status_code"] == 502


def test_compressed_responses_are_not_decompressed():
    bridge, client = broker(
        lambda outbound: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"untrusted bytes")
        )
    )
    with client, bridge:
        assert bridge(request())["status_code"] == 502


@pytest.mark.parametrize(
    "event",
    [
        {"type": "api_request"},
        request(headers={"Authorization": "agent-key"}),
        request(method="DELETE"),
        request(method=[]),
        request(body={}),
        request(body=[]),
        request(type="complete"),
        request("/jobs", "POST", {"bad": float("nan")}),
    ],
)
def test_invalid_events_are_rejected_without_network(event):
    calls = []
    bridge, client = broker(lambda outbound: calls.append(outbound))
    with client, bridge:
        assert bridge(event)["status_code"] == 400
    assert calls == []


def test_close_prevents_further_requests_and_does_not_close_injected_client():
    bridge, client = broker(lambda outbound: httpx.Response(200, json={"status": "ok"}))
    with client:
        bridge.close()
        assert not client.is_closed
        assert bridge(request())["status_code"] == 502


@pytest.mark.parametrize(
    "body",
    [
        {"data": "x" * (2 * 1024 * 1024 - 20)},
        [1000000000000000.0] * 150000,
    ],
)
def test_response_must_fit_the_complete_agent_protocol_envelope(body):
    import json

    from paper_library.agent_io import MAX_LINE_BYTES, encode
    from paper_library.verification_api import VerificationAPI

    # The second case expands short scientific-notation values when parsed.
    raw = (
        ("[" + ",".join(["1e15"] * 150000) + "]").encode()
        if isinstance(body, list)
        else json.dumps(body).encode()
    )
    assert len(raw) < 2 * 1024 * 1024
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
    )
    with client, VerificationAPI("https://verification.example", client=client) as broker:
        response = broker(
            {"type": "api_request", "id": "1", "method": "GET", "path": "/health", "body": None}
        )
    assert response["status_code"] == 502
    assert len(encode(response).encode()) < MAX_LINE_BYTES
