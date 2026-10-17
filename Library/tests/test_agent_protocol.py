import io
import json

import pytest

from paper_library.agent_protocol import VERIFICATION_API_ACCESS, AgentSession


def test_protocol_client_receives_paper_calls_api_and_returns_report():
    extracted = [{"id": "throughput", "source_quotes": ["At least 1000 tokens/s."]}]
    paper = {
        "id": "paper-id",
        "submission": {"title": "Paper", "verification_requirements": []},
        "plan": {"verification_requirements": extracted},
    }
    task = {
        "type": "task",
        "protocol_version": 1,
        "paper": paper,
        "verification_api": VERIFICATION_API_ACCESS,
    }
    reply = {"type": "api_response", "id": "1", "status_code": 200, "body": {"status": "ok"}}
    input_stream = io.StringIO(json.dumps(task) + "\n" + json.dumps(reply) + "\n")
    output = io.StringIO()
    session = AgentSession(input_stream, output)
    assert session.paper == paper
    assert session.verification_requirements == extracted
    assert session.verification_api["transport"] == "stdio"
    assert "POST /jobs" in session.verification_api["routes"]
    assert session.request("GET", "/health") == reply
    session.finish("inconclusive", "Missing experimental evidence.")
    request, result = [json.loads(line) for line in output.getvalue().splitlines()]
    assert request == {
        "type": "api_request",
        "id": "1",
        "method": "GET",
        "path": "/health",
        "body": None,
    }
    assert result["report"]["verdict"] == "inconclusive"


def test_protocol_client_without_a_plan_has_no_extracted_requirements():
    task = {"type": "task", "protocol_version": 1, "paper": {"plan": None}}
    session = AgentSession(io.StringIO(json.dumps(task) + "\n"), io.StringIO())
    assert session.verification_requirements == []


def test_protocol_client_rejects_mismatched_response():
    task = {"type": "task", "protocol_version": 1, "paper": {}}
    reply = {"type": "api_response", "id": "foreign", "status_code": 200, "body": {}}
    session = AgentSession(
        io.StringIO(json.dumps(task) + "\n" + json.dumps(reply) + "\n"), io.StringIO()
    )
    with pytest.raises(ValueError):
        session.request("GET", "/health")
