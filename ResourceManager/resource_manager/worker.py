"""Run generic compute jobs. Payload semantics belong to callers."""

import argparse
import os
import signal
import socket
import time
from uuid import uuid4

from .main import DEFAULT_DB_PATH, DEFAULT_RESOURCES_DIR
from .queue import ComputeQueue, JobCancelled, LeaseLost, OutputLimit
from .resource_catalog import ResourceCatalog
from .runtime import DockerRuntime, RuntimeFailure


def run_once(queue, catalog, worker_id, runtime_factory=DockerRuntime):
    claim = queue.claim(worker_id, catalog)
    if claim is None:
        return False
    job, token, resource = claim
    runtime = None
    error = None
    exit_code = None
    interrupted = False
    try:
        runtime = runtime_factory(resource)
        exit_code = runtime.run(job, token, queue)
        if exit_code != 0:
            error = "process_exited_nonzero"
    except JobCancelled:
        error = "compute_cancelled"
    except LeaseLost:
        error = "compute_lease_expired"
    except OutputLimit:
        error = "stdout_output_limit"
    except RuntimeFailure as exc:
        error = str(exc)
    except KeyboardInterrupt:
        error = "worker_interrupted"
        interrupted = True
    except Exception:
        # Includes operator runtime factory errors; never expose credentials or raw diagnostics.
        error = "compute_execution_failed"
    finally:
        cleaned = runtime is None or runtime.cleanup_confirmed
        try:
            queue.finish(
                job["id"], token, exit_code=exit_code, error=error, cleanup_confirmed=cleaned
            )
        except LeaseLost:
            pass
        finally:
            if cleaned:
                queue.release_capacity(job["id"], token)
    if interrupted:
        raise KeyboardInterrupt
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run generic Resource Manager compute jobs")
    parser.add_argument(
        "--db-path", default=os.environ.get("RESOURCE_MANAGER_DB_PATH", str(DEFAULT_DB_PATH))
    )
    parser.add_argument(
        "--resources-dir",
        default=os.environ.get("RESOURCE_MANAGER_RESOURCES_DIR", str(DEFAULT_RESOURCES_DIR)),
    )
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{uuid4().hex[:12]}")
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 0.05 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.05 and 60")
    queue = ComputeQueue(args.db_path)
    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        while True:
            catalog = ResourceCatalog.load(args.resources_dir)
            worked = run_once(queue, catalog, args.worker_id)
            if args.once:
                return 0
            if not worked:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main())
