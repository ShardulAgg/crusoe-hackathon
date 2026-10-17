import json

import pytest
from pydantic import ValidationError

from resource_manager.models import JobSubmission, NodeRegistration
from resource_manager.resource_catalog import ResourceCatalog, ResourceSpec


def test_legacy_operator_field_aliases(resource):
    fields = resource.model_dump()
    fields["agent_image"] = fields.pop("image")
    fields["max_concurrent_agents"] = fields.pop("max_concurrent_jobs")
    parsed = ResourceSpec.model_validate(fields)
    assert parsed == resource
    assert "agent_image" not in parsed.model_dump()
    assert "max_concurrent_agents" not in parsed.model_dump()


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "-flag"),
        ("image", "bad image"),
        ("docker_context", "--bad"),
        ("cpus", float("nan")),
        ("memory_mb", True),
        ("node_id", "bad/node"),
        ("allocation_mode", "arbitrary"),
        ("max_concurrent_jobs", 0),
    ],
)
def test_runtime_config_strict(resource, field, value):
    with pytest.raises(ValidationError):
        ResourceSpec.model_validate({**resource.model_dump(), field: value})


def test_catalog_rejects_duplicate_node_bindings_and_ids(resource):
    with pytest.raises(ValueError, match="IDs"):
        ResourceCatalog([resource, resource])
    with pytest.raises(ValueError, match="node"):
        ResourceCatalog([resource, resource.model_copy(update={"id": "other"})])


@pytest.mark.parametrize(
    "content",
    [
        '{"id":"a","id":"b"}',
        '{"cpus":NaN}',
        "{}",
        "[]",
        '"wrong"',
    ],
)
def test_invalid_inventory_is_rejected(tmp_path, content):
    (tmp_path / "invalid.json").write_text(content)
    with pytest.raises(ValueError):
        ResourceCatalog.load(tmp_path)


def test_valid_inventory_loaded(resource, tmp_path):
    (tmp_path / "cpu.json").write_text(resource.model_dump_json())
    assert ResourceCatalog.load(tmp_path).get("cpu") == resource
    assert json.loads((tmp_path / "cpu.json").read_text())["allocation_mode"] == "on_demand"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cpus", True),
        ("cpus", float("inf")),
        ("memory_mb", True),
        ("scratch_mb", 1),
        ("timeout_seconds", 0),
        ("resource_id", "--option"),
    ],
)
def test_requests_strict(body, field, value):
    with pytest.raises(ValidationError):
        JobSubmission.model_validate({**body.model_dump(), field: value})


def test_node_status_is_only_compute_liveness():
    with pytest.raises(ValidationError):
        NodeRegistration(id="node", cpus=1, memory_mb=512, status="verified")
