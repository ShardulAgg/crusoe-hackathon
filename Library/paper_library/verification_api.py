"""A narrow, host-side bridge to the inference verification API.

The sandbox supplies relative API requests; it never receives a network client
or the host's API key. Each bridge may read/cancel only the jobs it created.
"""

import json
import re
from urllib.parse import urlsplit
from uuid import UUID

import httpx

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROTOCOL_LINE_BYTES = 2 * 1024 * 1024
TIMEOUT = httpx.Timeout(connect=5, read=30, write=10, pool=5)
JOB_PATH = re.compile(r"/jobs/([a-f0-9-]+)(/cancel)?\Z")


def canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


class VerificationAPI:
    """Callable broker returning api_response events, including local errors.

    Invalid events return 400, denied routes return 403, oversized bodies return
    413, and transport/response failures return 502. Upstream HTTP errors and
    redirects retain their status codes but receive generic bodies. No upstream
    headers or exception text are returned. A supplied client remains caller-owned.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ):
        try:
            parsed = urlsplit(base_url)
            valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and "@" not in parsed.netloc
                and parsed.path in {"", "/"}
                and "?" not in base_url
                and "#" not in base_url
                and "\\" not in base_url
                and all(ord(character) > 32 for character in base_url)
            )
            url = httpx.URL(base_url)
        except (TypeError, ValueError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise ValueError("Verification API URL must be an HTTP(S) origin without credentials")
        if api_key is not None and (
            not isinstance(api_key, str)
            or any(ord(character) < 32 or ord(character) > 126 for character in api_key)
        ):
            raise ValueError("Invalid verification API key configuration")
        self.base_url = str(url).rstrip("/")
        self.api_key = api_key
        self._client = (
            client
            if client is not None
            else httpx.Client(trust_env=False, follow_redirects=False, timeout=TIMEOUT)
        )
        self._owns_client = client is None
        self._closed = False
        self._jobs: set[str] = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_client:
            self._client.close()
        self._closed = True

    def restore_owned_jobs(self, job_ids):
        """Restore host-recorded job scope when Library resumes a verification attempt."""
        ids = set(job_ids)
        if len(ids) > 1000 or any(not canonical_uuid(job_id) for job_id in ids):
            raise ValueError("Invalid persisted verification job scope")
        self._jobs.update(ids)

    @staticmethod
    def _response(event_id, status_code, body):
        return {
            "type": "api_response",
            "id": event_id,
            "status_code": status_code,
            "body": body,
        }

    def _allowed(self, method, path):
        if method == "GET" and path in {"/health", "/openapi.json"}:
            return True
        if method == "POST" and path == "/jobs":
            return True
        match = JOB_PATH.fullmatch(path)
        if match is None:
            return False
        job_id, cancel = match.groups()
        return (
            canonical_uuid(job_id)
            and job_id in self._jobs
            and ((method == "GET" and not cancel) or (method == "POST" and cancel))
        )

    @staticmethod
    def _read_body(response):
        # Ask for identity encoding and reject compressed replies so decompression
        # cannot expand an otherwise bounded response in the host process.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Unsupported response encoding")
        if response.is_stream_consumed:
            content = response.content
            if len(content) > MAX_RESPONSE_BYTES:
                raise ValueError("Response limit exceeded")
            return content
        content = bytearray()
        for chunk in response.iter_raw(chunk_size=65536):
            if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("Response limit exceeded")
            content.extend(chunk)
        return bytes(content)

    def __call__(self, event: dict) -> dict:
        event_id = event.get("id", "") if isinstance(event, dict) else ""
        if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
            return self._response("", 400, {"error": "Invalid API request event"})
        if (
            set(event) - {"type", "id", "method", "path", "body"}
            or event.get("type") != "api_request"
            or event.get("method") not in ("GET", "POST")
            or not isinstance(event.get("path"), str)
            or (event.get("body") is not None and not isinstance(event["body"], dict))
        ):
            return self._response(event_id, 400, {"error": "Invalid API request event"})
        method, path = event["method"], event["path"]
        if not self._allowed(method, path):
            return self._response(event_id, 403, {"error": "Verification API route is not allowed"})
        body = event.get("body")
        if method == "GET" and body is not None:
            return self._response(event_id, 400, {"error": "GET requests cannot include a body"})
        try:
            content = (
                None
                if body is None
                else json.dumps(
                    body, allow_nan=False, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
            return self._response(event_id, 400, {"error": "Invalid API request body"})
        if content is not None and len(content) > MAX_REQUEST_BYTES:
            return self._response(event_id, 413, {"error": "API request body exceeds limit"})
        if self._closed:
            return self._response(event_id, 502, {"error": "Verification API bridge is closed"})
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if content is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with self._client.stream(
                method,
                self.base_url + path,
                content=content,
                headers=headers,
                auth=None,
                follow_redirects=False,
                timeout=TIMEOUT,
            ) as response:
                if response.status_code >= 300:
                    return self._response(
                        event_id,
                        response.status_code,
                        {"error": "Verification API request was not successful"},
                    )
                data = json.loads(self._read_body(response))
                if method == "POST" and path == "/jobs":
                    if (
                        response.status_code != 202
                        or not isinstance(data, dict)
                        or not canonical_uuid(data.get("id"))
                    ):
                        raise ValueError("Invalid job submission response")
                    self._jobs.add(data["id"])
                reply = self._response(event_id, response.status_code, data)
                encoded = json.dumps(
                    reply, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                if len(encoded) > MAX_PROTOCOL_LINE_BYTES:
                    raise ValueError("Response exceeds the agent protocol envelope limit")
                return reply
        except (httpx.HTTPError, ValueError, RecursionError, UnicodeDecodeError):
            return self._response(event_id, 502, {"error": "Verification API request failed"})
