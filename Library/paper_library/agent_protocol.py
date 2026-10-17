"""Standard-library client to include in a sandboxed agent image."""

import json
import sys

VERIFICATION_API_ACCESS = {
    "transport": "stdio",
    "request_type": "api_request",
    "response_type": "api_response",
    "routes": [
        "GET /health",
        "GET /openapi.json",
        "POST /jobs",
        "GET /jobs/{id}",
        "POST /jobs/{id}/cancel",
    ],
    "job_scope": "Only jobs created by this sandbox run",
}


class AgentSession:
    def __init__(self, input_stream=None, output_stream=None):
        self.input = input_stream if input_stream is not None else sys.stdin
        self.output = output_stream if output_stream is not None else sys.stdout
        task = self._read()
        if task.get("type") != "task" or task.get("protocol_version") != 1:
            raise ValueError("Expected a version 1 verification task")
        self.paper = task["paper"]
        self.verification_requirements = (self.paper.get("plan") or {}).get(
            "verification_requirements", []
        )
        self.verification_api = task.get("verification_api", {"transport": "stdio"})
        self.sequence = 0

    def _read(self):
        line = self.input.readline()
        if not line:
            raise EOFError("Verification worker disconnected")
        return json.loads(line)

    def _write(self, value):
        self.output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        self.output.flush()

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        self.sequence += 1
        request_id = str(self.sequence)
        self._write(
            {"type": "api_request", "id": request_id, "method": method, "path": path, "body": body}
        )
        response = self._read()
        if response.get("type") != "api_response" or response.get("id") != request_id:
            raise ValueError("Unexpected verification API response")
        return response

    def finish(self, verdict: str, summary: str, evidence: list[str] | None = None):
        self._write(
            {
                "type": "result",
                "report": {"verdict": verdict, "summary": summary, "evidence": evidence or []},
            }
        )
