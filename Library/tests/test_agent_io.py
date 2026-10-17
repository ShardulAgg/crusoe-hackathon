import json
from uuid import uuid4

import httpx
import pytest

from paper_library.agent_io import AgentIO, AgentProtocolError
from paper_library.queue import PaperQueue
from paper_library.verification_api import VerificationAPI


@pytest.fixture
def io(tmp_path):
    return AgentIO(PaperQueue(tmp_path / "library.sqlite3"))


def event(**changes):
    return {
        "type": "api_request",
        "id": "1",
        "method": "POST",
        "path": "/jobs",
        "body": {},
        **changes,
    }


def test_initial_task_is_frozen_across_worker_restarts(io):
    job_id = str(uuid4())
    first = io.task(job_id, {"submission": {"paper_text": "Paper"}, "worker_id": "original"})
    restarted = AgentIO(PaperQueue(io.queue.path))
    assert restarted.task(job_id, {"worker_id": "new"}) == first


def test_durable_reply_replays_without_repeating_the_effect(io):
    job_id = str(uuid4())
    assert io.response(job_id, 1, event(), allow_new=True) is None
    reply = {"type": "api_response", "id": "1", "status_code": 202, "body": {"id": str(uuid4())}}
    line = io.save_response(job_id, "1", reply)
    restarted = AgentIO(PaperQueue(io.queue.path))
    assert restarted.response(job_id, 1, event(), allow_new=False) == line
    assert json.loads(line) == reply


def test_pending_effect_is_never_silently_reexecuted_after_a_crash(io):
    job_id = str(uuid4())
    io.response(job_id, 1, event(), allow_new=True)
    with pytest.raises(AgentProtocolError, match="verification_api_outcome_unknown"):
        AgentIO(PaperQueue(io.queue.path)).response(job_id, 1, event(), allow_new=True)


def test_agent_cannot_reuse_an_id_for_another_message(io):
    job_id = str(uuid4())
    io.response(job_id, 1, event(), allow_new=True)
    io.save_response(job_id, "1", {"type": "api_response", "id": "1", "status_code": 200})
    with pytest.raises(AgentProtocolError, match="duplicate_agent_request"):
        io.response(job_id, 2, event(), allow_new=True)


def test_only_prior_created_api_jobs_are_restored_to_the_resumed_broker_scope(io):
    compute_id, api_job = str(uuid4()), str(uuid4())
    io.response(compute_id, 1, event(), allow_new=True)
    io.save_response(
        compute_id,
        "1",
        {"type": "api_response", "id": "1", "status_code": 202, "body": {"id": api_job}},
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": api_job}))
    )
    with client, VerificationAPI("https://verification.example", client=client) as broker:
        broker.restore_owned_jobs(AgentIO(PaperQueue(io.queue.path)).owned_jobs(compute_id))
        assert (
            broker(event(id="2", method="GET", path="/jobs/" + api_job, body=None))["status_code"]
            == 200
        )
        assert (
            broker(event(id="3", method="GET", path="/jobs/" + str(uuid4()), body=None))[
                "status_code"
            ]
            == 403
        )


def test_terminal_compute_output_cannot_start_new_verification_api_work(io):
    with pytest.raises(AgentProtocolError, match="unfinished_agent_request"):
        io.response(str(uuid4()), 1, event(), allow_new=False)
