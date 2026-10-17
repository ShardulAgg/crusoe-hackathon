from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
]
JobStatus = Literal["queued", "provisioning", "running", "completed", "failed", "cancelled"]
MAX_LINE_BYTES = 2 * 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class JobSubmission(StrictModel):
    resource_id: Identifier
    node_id: Identifier
    cpus: float = Field(gt=0, le=256, allow_inf_nan=False)
    memory_mb: int = Field(ge=64, le=1048576, strict=True)
    scratch_mb: int = Field(ge=16, le=1048576, strict=True)
    timeout_seconds: int = Field(ge=1, le=86400, strict=True)


class NodeRegistration(StrictModel):
    id: Identifier
    cpus: float = Field(gt=0, le=65536, allow_inf_nan=False)
    memory_mb: int = Field(ge=64, le=2**40, strict=True)
    status: Literal["online", "offline"] = "online"


class StdinMessage(StrictModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    line: str = Field(max_length=MAX_LINE_BYTES)

    @field_validator("id")
    @classmethod
    def utf8_id(cls, value):
        value.encode("utf-8")
        return value

    @field_validator("line")
    @classmethod
    def single_bounded_utf8_line(cls, value):
        if "\n" in value or len(value.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValueError("line must be one UTF-8 line of at most 2 MiB without a newline")
        return value


class ComputeJob(BaseModel):
    id: str
    status: JobStatus
    resource_id: str
    node_id: str
    cpus: float
    memory_mb: int
    scratch_mb: int
    timeout_seconds: int
    sandbox_id: str
    exit_code: int | None = None
    error: str | None = None
    cleanup_required: bool = False
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
