"""Request generic compute jobs and exchange opaque input/output with Resource Manager."""

import json
import math
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024
JOB_STATUSES = {"queued", "provisioning", "running", "completed", "failed", "cancelled"}


class ResourceManagerError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Resource manager request failed ({code})")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON value")


class ResourceManager:
    def __init__(self, base_url, api_key=None, client=None, timeout_seconds=30):
        try:
            parsed = urlsplit(base_url)
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and "@" not in parsed.netloc
                and "?" not in base_url
                and "#" not in base_url
                and "\\" not in base_url
                and ".." not in parsed.path.split("/")
                and not parsed.path.startswith("//")
                and all(ord(character) > 32 for character in base_url)
            )
            url = httpx.URL(base_url)
        except (TypeError, ValueError, httpx.InvalidURL):
            valid_url = False
        if (
            not valid_url
            or (
                api_key is not None
                and (
                    not isinstance(api_key, str)
                    or any(not 32 <= ord(character) <= 126 for character in api_key)
                )
            )
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ResourceManagerError("invalid_configuration")
        self.base_url = str(url).rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5))
        self.client = (
            client
            if client is not None
            else httpx.Client(trust_env=False, follow_redirects=False, timeout=self.timeout)
        )
        self.owns_client = client is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.owns_client:
            self.client.close()

    def _request(self, method, path, body=None, dispatch_key=None):
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if dispatch_key is not None:
            if (
                not isinstance(dispatch_key, str)
                or not dispatch_key.strip()
                or len(dispatch_key) > 200
                or any(not 33 <= ord(character) <= 126 for character in dispatch_key)
            ):
                raise ResourceManagerError("invalid_dispatch_key")
            headers["Idempotency-Key"] = dispatch_key
        content = None
        if body is not None:
            try:
                content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, RecursionError):
                raise ResourceManagerError("invalid_submission") from None
            if len(content) > MAX_REQUEST_BYTES:
                raise ResourceManagerError("request_too_large")
            headers["Content-Type"] = "application/json"
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with self.client.stream(
                method,
                self.base_url + path,
                content=content,
                headers=headers,
                auth=None,
                timeout=self.timeout,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    code = "idempotency_conflict" if response.status_code == 409 else "unavailable"
                    raise ResourceManagerError(code)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ResourceManagerError("invalid_response")
                data = bytearray()
                chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if time.monotonic() >= deadline:
                        raise ResourceManagerError("timeout")
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ResourceManagerError("response_too_large")
                    data.extend(chunk)
            job = json.loads(
                data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
            if not isinstance(job, dict):
                raise ResourceManagerError("invalid_response")
            return job
        except ResourceManagerError:
            raise
        except httpx.TimeoutException:
            raise ResourceManagerError("timeout") from None
        except httpx.HTTPError:
            raise ResourceManagerError("unavailable") from None
        except (TypeError, ValueError, KeyError, AttributeError, UnicodeError, RecursionError):
            raise ResourceManagerError("invalid_response") from None

    @staticmethod
    def _job_id(job_id):
        try:
            return str(UUID(job_id))
        except (TypeError, ValueError, AttributeError):
            raise ResourceManagerError("invalid_job_id") from None

    @classmethod
    def _job(cls, job, expected_id=None):
        job_id = cls._job_id(job.get("id"))
        if not isinstance(job.get("status"), str) or job["status"] not in JOB_STATUSES:
            raise ResourceManagerError("invalid_response")
        if expected_id is not None and job_id != expected_id:
            raise ResourceManagerError("job_mismatch")
        return job

    def resources(self):
        response = self._request("GET", "/resources")
        if not isinstance(response.get("resources"), list) or len(response["resources"]) > 100:
            raise ResourceManagerError("invalid_response")
        return response["resources"]

    def submit(self, plan, compute_key):
        if hasattr(plan, "model_dump"):
            plan = plan.model_dump(mode="json")
        fields = ("resource_id", "node_id", "cpus", "memory_mb", "scratch_mb", "timeout_seconds")
        body = {field: plan[field] for field in fields}
        return self._job(self._request("POST", "/jobs", body, compute_key))

    def get(self, job_id):
        job_id = self._job_id(job_id)
        return self._job(self._request("GET", "/jobs/" + job_id), job_id)

    def write_stdin(self, job_id, message_id, line):
        job_id = self._job_id(job_id)
        if not isinstance(line, str) or "\n" in line or "\r" in line:
            raise ResourceManagerError("invalid_input")
        return self._request("POST", "/jobs/" + job_id + "/stdin", {"id": message_id, "line": line})

    def stdout(self, job_id, after=0):
        job_id = self._job_id(job_id)
        if type(after) is not int or after < 0:
            raise ResourceManagerError("invalid_cursor")
        response = self._request("GET", f"/jobs/{job_id}/stdout?after={after}")
        lines, cursor = response.get("lines"), response.get("next_cursor")
        if type(lines) is not list or len(lines) > 64 or type(cursor) is not int:
            raise ResourceManagerError("invalid_response")
        last = after
        for item in lines:
            if (
                not isinstance(item, dict)
                or type(item.get("sequence")) is not int
                or item["sequence"] != last + 1
                or not isinstance(item.get("line"), str)
            ):
                raise ResourceManagerError("invalid_response")
            last = item["sequence"]
        if cursor != last:
            raise ResourceManagerError("invalid_response")
        return response

    def cancel(self, job_id):
        job_id = self._job_id(job_id)
        return self._job(self._request("POST", "/jobs/" + job_id + "/cancel"), job_id)

    def close_stdin(self, job_id):
        job_id = self._job_id(job_id)
        return self._request("POST", "/jobs/" + job_id + "/stdin/close")
