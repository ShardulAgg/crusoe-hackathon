"""Operator-owned compute quotas and runtime configuration."""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .models import Identifier, JobSubmission


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    id: Identifier
    node_id: Identifier | None = None
    description: str = Field(default="", max_length=5000)
    enabled: bool = False
    image: str = Field(
        min_length=1, max_length=512, validation_alias=AliasChoices("image", "agent_image")
    )
    docker_context: str | None = None
    cpus: float = Field(gt=0, le=256, allow_inf_nan=False)
    memory_mb: int = Field(ge=64, le=1048576, strict=True)
    scratch_mb: int = Field(default=64, ge=16, le=1048576, strict=True)
    max_timeout_seconds: int = Field(default=600, ge=1, le=86400, strict=True)
    max_concurrent_jobs: int = Field(
        default=1,
        ge=1,
        le=1000,
        strict=True,
        validation_alias=AliasChoices("max_concurrent_jobs", "max_concurrent_agents"),
    )
    cost_per_hour_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    allocation_mode: Literal["on_demand", "reserved"] = "on_demand"

    @field_validator("image")
    @classmethod
    def safe_image(cls, value):
        if value.startswith("-") or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("image must be an operator-installed image reference")
        return value

    @field_validator("docker_context")
    @classmethod
    def safe_context(cls, value):
        if value is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value) is None:
            raise ValueError("docker_context must name an operator-configured Docker context")
        return value

    def check_request(self, request: JobSubmission):
        if not self.enabled or self.node_id != request.node_id or self.id != request.resource_id:
            raise ValueError(
                "The requested resource must be enabled and bound to the requested node"
            )
        for requested, maximum in (
            (request.cpus, self.cpus),
            (request.memory_mb, self.memory_mb),
            (request.scratch_mb, self.scratch_mb),
            (request.timeout_seconds, self.max_timeout_seconds),
        ):
            if requested > maximum:
                raise ValueError("Requested compute exceeds the approved resource quota")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Non-finite JSON number")


class ResourceCatalog:
    def __init__(self, resources):
        self.resources = tuple(ResourceSpec.model_validate(resource) for resource in resources)
        if len(self.resources) > 100:
            raise ValueError("At most 100 resource records are supported")
        self._by_id = {resource.id: resource for resource in self.resources}
        if len(self._by_id) != len(self.resources):
            raise ValueError("Resource IDs must be unique")
        nodes = [
            item.node_id for item in self.resources if item.enabled and item.node_id is not None
        ]
        if len(nodes) != len(set(nodes)):
            raise ValueError("Only one enabled resource may bind a node")

    def get(self, resource_id):
        try:
            return self._by_id[resource_id]
        except KeyError as exc:
            raise ValueError("Unknown resource") from exc

    @classmethod
    def load(cls, path):
        directory = Path(path).expanduser()
        if not directory.is_dir():
            raise ValueError("Resource inventory folder does not exist")
        files = sorted(directory.glob("*.json"))
        if len(files) > 100:
            raise ValueError("At most 100 resource files are supported")
        resources = []
        for file in files:
            with file.open("rb") as stream:
                content = stream.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError("Resource record exceeds 1 MiB")
            data = json.loads(
                content, object_pairs_hook=unique_object, parse_constant=invalid_constant
            )
            resources.append(ResourceSpec.model_validate(data))
        return cls(resources)
