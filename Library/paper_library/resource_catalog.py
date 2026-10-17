"""Resource Manager compute snapshots and Library verification plans."""

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .verification_requirements import (
    ExtractedVerificationRequirement,
    validate_extracted_requirements,
)

MAX_RESOURCE_FILES = 100
MAX_RESOURCE_FILE_BYTES = 1024 * 1024

ResourceId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
]
NodeId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
]
Rationale = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
VerificationStep = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    id: ResourceId
    node_id: NodeId | None = None
    description: str = Field(default="", max_length=5000)
    enabled: bool = False
    cpus: float = Field(gt=0, le=256, allow_inf_nan=False)
    memory_mb: int = Field(ge=64, le=1048576)
    scratch_mb: int = Field(default=64, ge=16, le=1048576)
    max_timeout_seconds: int = Field(default=600, ge=1, le=86400)
    max_concurrent_jobs: int = Field(
        default=1,
        ge=1,
        le=1000,
        validation_alias=AliasChoices("max_concurrent_jobs", "max_concurrent_agents"),
    )
    cost_per_hour_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    allocation_mode: Literal["on_demand", "reserved"] = "on_demand"

    def planner_view(self) -> dict:
        """Expose capacities while keeping execution configuration out of the LLM prompt."""
        return self.model_dump(
            include={
                "id",
                "node_id",
                "description",
                "cpus",
                "memory_mb",
                "scratch_mb",
                "max_timeout_seconds",
                "max_concurrent_jobs",
                "cost_per_hour_usd",
                "allocation_mode",
            }
        )


class SandboxPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")

    decision: Literal["launch", "blocked"]
    rationale: Rationale
    verification_steps: list[VerificationStep] = Field(default_factory=list, max_length=30)
    verification_requirements: list[ExtractedVerificationRequirement] = Field(
        default_factory=list, max_length=50
    )
    missing_information: list[VerificationStep] = Field(default_factory=list, max_length=50)
    resource_id: ResourceId | None = None
    node_id: NodeId | None = None
    cpus: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    memory_mb: int | None = Field(default=None, ge=64)
    scratch_mb: int | None = Field(default=None, ge=16)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("verification_requirements")
    @classmethod
    def valid_extracted_requirements(cls, value):
        return validate_extracted_requirements(value)

    @model_validator(mode="after")
    def consistent_allocation(self):
        allocations = (
            self.resource_id,
            self.cpus,
            self.memory_mb,
            self.scratch_mb,
            self.timeout_seconds,
        )
        if self.decision == "launch" and any(value is None for value in allocations):
            raise ValueError("A launch plan requires a resource and every allocation field")
        if self.decision == "blocked" and (
            self.node_id is not None or any(value is not None for value in allocations)
        ):
            raise ValueError("A blocked plan must not contain resource allocations")
        return self


class ResourceCatalog:
    def __init__(self, resources: Iterable[ResourceSpec]):
        self.resources = tuple(ResourceSpec.model_validate(resource) for resource in resources)
        if len(self.resources) > MAX_RESOURCE_FILES:
            raise ValueError(
                f"A resource catalog must contain at most {MAX_RESOURCE_FILES} resources"
            )
        self._by_id = {resource.id: resource for resource in self.resources}
        if len(self._by_id) != len(self.resources):
            raise ValueError("Resource IDs must be unique")
        bound_nodes = [
            resource.node_id
            for resource in self.resources
            if resource.enabled and resource.node_id is not None
        ]
        if len(set(bound_nodes)) != len(bound_nodes):
            raise ValueError("Enabled resource pools must use distinct node IDs")

    def enabled_resources(self) -> list[ResourceSpec]:
        return [resource for resource in self.resources if resource.enabled]

    def get(self, resource_id: str) -> ResourceSpec:
        try:
            return self._by_id[resource_id]
        except KeyError as exc:
            raise ValueError(f"Unknown resource: {resource_id}") from exc


def validate_plan(plan: SandboxPlan, catalog: ResourceCatalog) -> ResourceSpec | None:
    plan = SandboxPlan.model_validate(plan)
    if plan.decision == "blocked":
        return None
    resource = ResourceSpec.model_validate(catalog.get(plan.resource_id))
    if not resource.enabled:
        raise ValueError("The selected resource is disabled")
    if plan.node_id != resource.node_id:
        raise ValueError("The plan's node_id must match the selected resource's node_id")
    limits = (
        (plan.cpus, resource.cpus, "CPU"),
        (plan.memory_mb, resource.memory_mb, "memory"),
        (plan.scratch_mb, resource.scratch_mb, "scratch"),
        (plan.timeout_seconds, resource.max_timeout_seconds, "timeout"),
    )
    for requested, available, label in limits:
        if requested > available:
            raise ValueError(f"The plan exceeds the selected resource's {label} limit")
    return resource
