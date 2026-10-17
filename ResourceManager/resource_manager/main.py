import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import ComputeJob, JobStatus, JobSubmission, NodeRegistration, StdinMessage
from .queue import ComputeQueue, QueueError, idempotency_key
from .resource_catalog import ResourceCatalog

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "resource-manager.sqlite3"
DEFAULT_RESOURCES_DIR = Path(__file__).resolve().parents[2] / "Resources"
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app):
    app.state.api_key = os.environ.get("RESOURCE_MANAGER_API_KEY")
    app.state.queue = ComputeQueue(os.environ.get("RESOURCE_MANAGER_DB_PATH", str(DEFAULT_DB_PATH)))
    app.state.resources_dir = os.environ.get(
        "RESOURCE_MANAGER_RESOURCES_DIR", str(DEFAULT_RESOURCES_DIR)
    )
    yield


app = FastAPI(
    title="Resource Manager",
    version="0.2.0",
    lifespan=lifespan,
    description="Independent compute job queue with bounded opaque process I/O.",
)


def authenticate(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
):
    key = request.app.state.api_key
    if key and (
        credentials is None
        or not secrets.compare_digest(credentials.credentials.encode(), key.encode())
    ):
        raise HTTPException(
            401, "Invalid Resource Manager API key", headers={"WWW-Authenticate": "Bearer"}
        )


@app.exception_handler(QueueError)
async def queue_error(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.code})


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
        },
    )


def catalog(request):
    try:
        return ResourceCatalog.load(request.app.state.resources_dir)
    except (OSError, ValueError):
        raise HTTPException(503, "Compute resource inventory is unavailable") from None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/resources", dependencies=[Depends(authenticate)])
def resources(request: Request):
    return {"resources": request.app.state.queue.resources(catalog(request))}


@app.post("/nodes/register", dependencies=[Depends(authenticate)])
def register_node(body: NodeRegistration, request: Request):
    return request.app.state.queue.register_node(body)


@app.get("/nodes", dependencies=[Depends(authenticate)])
def nodes(request: Request):
    return {"nodes": request.app.state.queue.nodes()}


@app.post("/nodes/{node_id}/heartbeat", dependencies=[Depends(authenticate)])
def node_heartbeat(node_id: str, request: Request):
    return request.app.state.queue.heartbeat_node(node_id)


@app.post("/jobs", status_code=202, response_model=ComputeJob, dependencies=[Depends(authenticate)])
def submit_job(
    body: JobSubmission,
    request: Request,
    response: Response,
    key: str = Header(alias="Idempotency-Key"),
):
    key = idempotency_key(key)
    job = request.app.state.queue.submit(body, key, catalog(request))
    response.headers["Location"] = f"/jobs/{job['id']}"
    return job


@app.get("/jobs", dependencies=[Depends(authenticate)])
def list_jobs(
    request: Request,
    status: JobStatus | None = None,
    resource_id: str | None = None,
    node_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
):
    return request.app.state.queue.list_jobs(status, resource_id, node_id, limit, offset)


@app.get("/jobs/{job_id}", response_model=ComputeJob, dependencies=[Depends(authenticate)])
def get_job(job_id: UUID, request: Request):
    return request.app.state.queue.get(str(job_id))


@app.post("/jobs/{job_id}/stdin", status_code=202, dependencies=[Depends(authenticate)])
def stdin(job_id: UUID, body: StdinMessage, request: Request):
    return request.app.state.queue.put_stdin(str(job_id), body)


@app.post("/jobs/{job_id}/stdin/close", status_code=202, dependencies=[Depends(authenticate)])
def close_stdin(job_id: UUID, request: Request):
    return request.app.state.queue.close_stdin(str(job_id))


@app.get("/jobs/{job_id}/stdout", dependencies=[Depends(authenticate)])
def stdout(job_id: UUID, request: Request, after: int = Query(default=0, ge=0, le=2**63 - 1)):
    return request.app.state.queue.stdout(str(job_id), after)


@app.post("/jobs/{job_id}/cancel", response_model=ComputeJob, dependencies=[Depends(authenticate)])
def cancel(job_id: UUID, request: Request):
    return request.app.state.queue.cancel(str(job_id))
