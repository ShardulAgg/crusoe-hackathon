"""Read papers, plan verification, and run agents on Resource Manager compute jobs."""

import argparse
import json
import logging
import math
import os
import signal
import socket
import threading
import time
from queue import Empty, Queue

from .agent_io import AgentIO, AgentProtocolError, response_message_id
from .main import DEFAULT_DB_PATH
from .models import VerificationReport
from .planner import LLMPlanner, PlannerError
from .queue import PaperQueue, QueueConflict
from .resource_catalog import ResourceCatalog, ResourceSpec, SandboxPlan
from .resource_manager import (
    ResourceManager,
    ResourceManagerError,
    _invalid_constant,
    _unique_object,
)
from .verification_api import VerificationAPI

logger = logging.getLogger("paper_library.worker")
ACTIVE_COMPUTE = {"queued", "provisioning", "running"}


def supervised_call(function, heartbeat, timeout_seconds=90):
    result = Queue(maxsize=1)

    def call():
        try:
            result.put((True, function()))
        except Exception as exc:
            result.put((False, exc))

    threading.Thread(target=call, daemon=True).start()
    deadline, next_heartbeat = time.monotonic() + timeout_seconds, 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_heartbeat:
            heartbeat()
            next_heartbeat = time.monotonic() + 20
        try:
            succeeded, value = result.get(timeout=0.1)
        except Empty:
            continue
        if time.monotonic() >= deadline:
            break
        if not succeeded:
            raise value
        return value
    raise AgentProtocolError("verification_operation_timeout")


def run_once(queue, manager, planner, api_factory, worker_id):
    paper = queue.claim_for_verification(worker_id)
    if paper is None:
        return False
    token = paper.pop("lease_token")
    compute_key = paper.pop("compute_key")
    paper_id, job = paper["id"], None

    def heartbeat():
        queue.heartbeat(paper_id, token)

    try:
        if paper["status"] in {"completed", "failed", "blocked"}:
            job = manager.get(paper["compute_job_id"])
            if job["status"] in ACTIVE_COMPUTE:
                job = manager.cancel(job["id"])
            queue.save_compute_job(paper_id, token, job)
            queue.defer_verification(paper_id, token, "cleanup_reconciliation")
            return True
        if paper["plan"] is None:
            resources = ResourceCatalog(
                ResourceSpec.model_validate(item) for item in manager.resources()
            )
            if not resources.enabled_resources():
                queue.defer_verification(paper_id, token, "no_compute_available")
                return True
            plan = supervised_call(lambda: planner.plan(paper, resources), heartbeat)
            plan = SandboxPlan.model_validate(plan)
            paper = queue.save_plan(paper_id, token, plan.model_dump(mode="json"))
        else:
            plan = SandboxPlan.model_validate(paper["plan"])
        if plan.decision == "blocked":
            queue.block_verification(paper_id, token, plan.rationale)
            return True
        job = (
            manager.get(paper["compute_job_id"])
            if paper["compute_job_id"]
            else manager.submit(plan, compute_key)
        )
        paper = queue.save_compute_job(paper_id, token, job)
        if job["status"] in {"failed", "cancelled"}:
            queue.finish_verification(
                paper_id,
                token,
                error="compute_job_failed",
                cleanup_required=job["cleanup_required"],
            )
            return True
        io = AgentIO(queue)
        if job["status"] in ACTIVE_COMPUTE:
            manager.write_stdin(job["id"], "task", io.task(job["id"], paper))
        cursor, report = 0, None
        with api_factory() as api:
            api.restore_owned_jobs(io.owned_jobs(job["id"]))
            while True:
                output = manager.stdout(job["id"], cursor)
                for item in output["lines"]:
                    event = json.loads(
                        item["line"],
                        object_pairs_hook=_unique_object,
                        parse_constant=_invalid_constant,
                    )
                    if not isinstance(event, dict) or report is not None:
                        raise AgentProtocolError("unexpected_agent_output")
                    if event.get("type") == "result" and set(event) == {"type", "report"}:
                        report = VerificationReport.model_validate(event["report"])
                    elif event.get("type") == "api_request":
                        response = io.response(
                            job["id"],
                            item["sequence"],
                            event,
                            allow_new=job["status"] in ACTIVE_COMPUTE,
                        )
                        if response is None:
                            value = supervised_call(lambda: api(event), heartbeat, 60)
                            response = io.save_response(job["id"], event["id"], value)
                        decoded = json.loads(response)
                        if (
                            event["method"] == "POST"
                            and event["path"] == "/jobs"
                            and decoded["status_code"] == 502
                        ):
                            raise AgentProtocolError("verification_api_outcome_unknown")
                        if job["status"] in ACTIVE_COMPUTE:
                            manager.write_stdin(
                                job["id"], response_message_id(event["id"]), response
                            )
                    else:
                        raise AgentProtocolError("unexpected_agent_output")
                cursor = output["next_cursor"]
                if not output["lines"]:
                    break
                heartbeat()
        if report is not None and job["status"] in ACTIVE_COMPUTE:
            manager.close_stdin(job["id"])
        if job["status"] == "completed":
            if type(job.get("exit_code")) is not int or job["exit_code"] != 0 or report is None:
                raise AgentProtocolError("agent_finished_without_valid_report")
            queue.finish_verification(
                paper_id, token, report=report, cleanup_required=job["cleanup_required"]
            )
        else:
            queue.defer_verification(paper_id, token, "awaiting_verification_agent")
    except QueueConflict:
        logger.warning("Lost verification ownership for paper %s", paper_id)
    except ResourceManagerError as exc:
        try:
            queue.defer_verification(paper_id, token, exc.code)
        except QueueConflict:
            pass
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, PlannerError)
            else str(exc)
            if isinstance(exc, AgentProtocolError)
            else "verification_failed"
        )
        cleanup_required = bool(paper.get("compute_job_id"))
        if paper.get("compute_job_id"):
            try:
                cancelled = manager.cancel(paper["compute_job_id"])
                queue.save_compute_job(paper_id, token, cancelled)
                cleanup_required = (
                    cancelled["cleanup_required"] or cancelled["status"] in ACTIVE_COMPUTE
                )
            except Exception:
                pass
        try:
            queue.finish_verification(
                paper_id, token, error=code, cleanup_required=cleanup_required
            )
        except QueueConflict:
            pass
    return True


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a finite positive number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resource-manager-url", default=os.environ.get("LIBRARY_RESOURCE_MANAGER_URL")
    )
    parser.add_argument("--planner-base-url", default=os.environ.get("LIBRARY_PLANNER_BASE_URL"))
    parser.add_argument("--planner-model", default=os.environ.get("LIBRARY_PLANNER_MODEL"))
    parser.add_argument(
        "--db-path", default=os.environ.get("LIBRARY_DB_PATH", str(DEFAULT_DB_PATH))
    )
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--poll-seconds", type=positive_seconds, default=2)
    parser.add_argument(
        "--once", action="store_true", help="Process one paper or poll its compute job"
    )
    args = parser.parse_args(argv)
    if not args.resource_manager_url or not args.planner_base_url or not args.planner_model:
        parser.error("Configure Library's resource manager URL and planner endpoint/model")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    manager = planner = None
    try:
        manager = ResourceManager(
            args.resource_manager_url, os.environ.get("LIBRARY_RESOURCE_MANAGER_API_KEY")
        )
        planner = LLMPlanner(
            args.planner_base_url, args.planner_model, os.environ.get("LIBRARY_PLANNER_API_KEY")
        )
        queue = PaperQueue(args.db_path)

        def api_factory():
            return VerificationAPI(
                os.environ.get("LIBRARY_VERIFICATION_API_URL", "http://127.0.0.1:8000"),
                os.environ.get("LIBRARY_VERIFICATION_API_KEY"),
            )

        with api_factory():
            pass
    except Exception:
        if manager is not None:
            manager.close()
        if planner is not None:
            planner.close()
        logger.error("Library verification configuration is invalid")
        return 1

    def stop_worker(signum, frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, stop_worker)
    try:
        with manager, planner:
            while True:
                run_once(queue, manager, planner, api_factory, args.worker_id)
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
