# Compute resources

This folder is the operator-owned compute inventory used by the generic [ResourceManager service](../ResourceManager/README.md). Each top-level JSON file defines a resource pool. ResourceManager reports eligible available capacity through `GET /resources`, accepts resource allocations through `POST /jobs`, and creates a fresh job environment on demand. It does not read papers, run an LLM judge, interpret job I/O, or assign verification verdicts.

Library's judge reads the public resource snapshot and requests the minimum sufficient allocation for its verification agent. Other clients can use the same compute interface independently of Library. ResourceManager keeps image and Docker-host configuration private and controls reservations and execution limits.

`local-cpu.json` is a **disabled illustrative example** with 2 CPUs and 8192 MiB of memory. Its node ID, image, and host mapping are placeholders, not registered or detected capacity. Before enabling it, choose the intended registered node, measure the capacity available to jobs, install the approved compute image on that host, and configure the correct Docker context.

| Field | Meaning |
| --- | --- |
| `id` | Unique resource-pool identifier |
| `node_id` | Exact registered execution-node ID |
| `description` | Factual operator description of installed capabilities |
| `enabled` | Whether the pool may accept work; defaults to `false` |
| `allocation_mode` | `on_demand` creates a fresh execution environment when a job is allocated |
| `image` | Approved compute image installed on the selected host; private to ResourceManager |
| `docker_context` | Operator-configured Docker host mapping; private to ResourceManager |
| `cpus` | Total CPU capacity allocated to this pool |
| `memory_mb` | Total memory capacity allocated to this pool, in MiB |
| `scratch_mb` | Maximum temporary scratch space for one job, in MiB |
| `max_timeout_seconds` | Maximum runtime for one job |
| `max_concurrent_jobs` | Maximum concurrent execution environments in the pool |
| `cost_per_hour_usd` | Optional operator-supplied cost estimate; an unknown cost is not free |

The resource snapshot exposes IDs, capabilities, capacities, limits, and optional costs. Clients choose a resource, its bound node, and CPU/memory/scratch/runtime allocations. They cannot choose an image, host path, Docker context, credentials, or network policy through that request. ResourceManager validates and reserves the requested capacity before execution.

Bind each pool to the correct registered node and Docker context. The registry's ID does not configure or authenticate the Docker host automatically. Avoid counting the same node's capacity through multiple enabled pools. Inventory values are operator quotas and declared hardware metadata, rather than a live probe of every process on the host.

Set `RESOURCE_MANAGER_RESOURCES_DIR` on ResourceManager to select this folder. Inventory changes are administrative configuration; do not store credentials here. On-demand environment creation uses existing configured hosts. The example does not provision cloud VMs or discover machines automatically.
