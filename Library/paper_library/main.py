import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import Paper, PaperList, PaperSubmission
from .queue import PaperQueue, QueueConflict

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "library.sqlite3"
bearer = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.api_key = os.environ.get("LIBRARY_API_KEY")
    app.state.queue = PaperQueue(os.environ.get("LIBRARY_DB_PATH", str(DEFAULT_DB_PATH)))
    yield


app = FastAPI(
    title="Library",
    description="Read and verify queued papers using generic compute jobs from Resource Manager.",
    version="0.1.0",
    lifespan=lifespan,
)


def authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    key = request.app.state.api_key
    if key and (
        credentials is None
        or not secrets.compare_digest(credentials.credentials.encode(), key.encode())
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid Library API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Return validation details without copying a submitted paper into the response.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post(
    "/papers",
    status_code=202,
    response_model=Paper,
    response_model_exclude_unset=True,
    dependencies=[Depends(authenticate)],
)
def submit_paper(body: PaperSubmission, request: Request, response: Response):
    paper = request.app.state.queue.submit(body)
    response.headers["Location"] = f"/papers/{paper['id']}"
    return paper


@app.get(
    "/papers",
    response_model=PaperList,
    response_model_exclude_unset=True,
    dependencies=[Depends(authenticate)],
)
def list_papers(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2**63 - 1),
    status: Literal[
        "pending_verification", "planning", "verifying", "blocked", "completed", "failed"
    ]
    | None = None,
):
    return request.app.state.queue.list(limit, offset, status)


@app.get(
    "/papers/{paper_id}",
    response_model=Paper,
    response_model_exclude_unset=True,
    dependencies=[Depends(authenticate)],
)
def get_paper(paper_id: UUID, request: Request):
    paper = request.app.state.queue.get(str(paper_id))
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper


@app.post(
    "/papers/{paper_id}/retry",
    response_model=Paper,
    response_model_exclude_unset=True,
    status_code=202,
    dependencies=[Depends(authenticate)],
)
def retry_paper(paper_id: UUID, request: Request):
    try:
        paper = request.app.state.queue.retry(str(paper_id))
    except QueueConflict as exc:
        raise HTTPException(
            status_code=409, detail="Only blocked or failed papers can retry"
        ) from exc
    if paper is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper
