# ResourceManager

ResourceManager is an independent compute queue. Clients discover available resources, request bounded CPU/memory/scratch/runtime allocations, and exchange opaque UTF-8 lines with the resulting process. ResourceManager owns resource reservations and execution; the caller owns the workload and the meaning of its output.

```text
Any compute client → ResourceManager API → durable compute queue
                              ↕                    ↓
                       opaque job I/O       reserve registered capacity
                                                   ↓
                                       fresh isolated process environment
```

The service does not read papers, call an LLM, extract requirements, invoke a verification API, or interpret reports and verdicts. [Library](../Library/README.md) is one client: it owns those application responsibilities and uses the same generic compute interface available to other clients.

## Start the service

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```sh
cd ResourceManager
uv sync --python 3.12
uv run uvicorn resource_manager.main:app --host 127.0.0.1 --port 8002
```

API docs: <http://127.0.0.1:8002/docs>. Machine-readable contract: `/openapi.json`.

In another terminal, start the execution worker:

```sh
cd ResourceManager
uv run python -m resource_manager.worker
```

API and worker share ResourceManager's local persistent database and resource inventory. The worker reloads inventory before each claim. `--db-path` and `--resources-dir` override their locations; `--poll-seconds` sets idle polling, and `--once` processes at most one queued job. Multiple workers can share this local database, with atomic claims and capacity reservations.

Set `RESOURCE_MANAGER_API_KEY` to require `Authorization: Bearer <key>` on compute, resource, channel, and node routes. Health and API docs remain public. Without the key, all callers who can reach the API share access; there is no per-client ownership system. Use authenticated HTTPS or a protected private network for remote clients.

## Configure resources and register a node

The sibling [Resources folder](../Resources/README.md) contains operator-owned pool definitions. The included `local-cpu.json` is disabled and illustrative. Configure an existing Docker host, its approved installed image, node binding, quotas, and optional Docker context before enabling a pool. Image and Docker context remain private; clients cannot submit either through the job API.

Use `image` and `max_concurrent_jobs` in new configuration files. The legacy names `agent_image` and `max_concurrent_agents` remain accepted for existing inventory. Each enabled pool must bind one registered `node_id`; only one enabled pool may bind a node.

Register the node with this service:

```sh
curl --fail-with-body http://127.0.0.1:8002/nodes/register \
  -H 'Content-Type: application/json' \
  --data-binary @examples/node.json
```

The registration declares `id`, `cpus`, `memory_mb`, and optional `status` (`online` or `offline`). It does not probe the machine or configure Docker access. Bind the inventory record to that exact ID and to the Docker context reaching that host.

An online node is eligible only while its registration is less than **120 seconds** old. A node-side agent or operator must refresh it regularly, for example every 30 seconds, using:

```sh
curl --fail-with-body -X POST http://127.0.0.1:8002/nodes/compute-node-1/heartbeat
```

Compute-job lease renewals do not renew node registration. `GET /nodes` reports registration state. `GET /resources` returns eligible pools with remaining CPU/memory capacity and concurrency slots, capped by both configured quotas and registered hardware. This is declared capacity minus ResourceManager reservations, rather than a probe of other processes' utilization.

Resource records support `allocation_mode: on_demand` or `reserved`. The mode describes the operator's supplied capacity; both use the same bounded job-allocation path and create a fresh environment per job. Reserved mode does not purchase a reservation or pre-create cloud infrastructure. These settings use existing registered Docker hosts.

## Submit a generic job

```sh
curl --fail-with-body http://127.0.0.1:8002/jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: echo-job-1' \
  --data-binary @examples/job.json
```

The request contains exactly these six resource fields:

```json
{
  "resource_id": "local-cpu",
  "node_id": "compute-node-1",
  "cpus": 0.25,
  "memory_mb": 128,
  "scratch_mb": 16,
  "timeout_seconds": 60
}
```

Use IDs and limits from your configured resources. `POST /jobs` returns HTTP 202, a compute job `id`, and a `Location` header. Submission creates a durable queued job; the worker reserves capacity and provisions its environment when the selected resource is eligible. Capacity is accounted by node identity, and simultaneous claims cannot oversubscribe its CPU, memory, or concurrency quota.

`Idempotency-Key` is required: nonblank printable ASCII, at most 200 characters. Retry an uncertain submission with the same key and identical normalized allocation to recover the existing job. Reusing a key for a different allocation returns 409. A new execution request uses a new key. Keys and jobs persist atomically in the manager database.

Job requests do not accept application payloads, source documents, images, commands, environment variables, Docker settings, or credentials. Send workload input through the job's channel instead. The operator-approved image supplies the executable entrypoint.

## Input, output, and completion

Send a single UTF-8 input line with a caller-chosen message ID:

```sh
curl --fail-with-body http://127.0.0.1:8002/jobs/JOB_ID/stdin \
  -H 'Content-Type: application/json' \
  -d '{"id":"message-1","line":"hello compute"}'
curl --fail-with-body -X POST http://127.0.0.1:8002/jobs/JOB_ID/stdin/close
curl --fail-with-body 'http://127.0.0.1:8002/jobs/JOB_ID/stdout?after=0'
```

Replace `JOB_ID` with the returned ID. The runtime appends a newline when delivering each accepted input line. Repeating a message ID with the same line is idempotent, including after completion; reusing it for different content returns 409. Closing stdin is idempotent and delivers EOF after queued input has been written. New input is rejected after closure or job termination.

Output is returned as `{"lines":[{"sequence":1,"line":"hello compute"}],"next_cursor":1}`. Poll using `after=next_cursor` to fetch subsequent lines. Pages contain at most 64 lines and at most 8 MiB of serialized output. Lines are opaque: JSON, plain text, or another application format may be transported without interpretation. Channels require UTF-8 and single lines rather than arbitrary binary streams. Stdin and stdout are each capped at 16 MiB per job, with a 2 MiB limit per line. Stderr is separately bounded to 16 MiB and discarded, not returned through the stdout API.

Statuses are `queued → provisioning → running → completed`, with `failed` and `cancelled` terminal outcomes. `completed` means the process exited with code 0 and cleanup was confirmed. Other exits or runtime failures produce generic failure metadata. Compute status and `exit_code` do not establish any application result; clients interpret output themselves.

| Request | Purpose |
| --- | --- |
| `GET /resources` | Available public resource snapshot |
| `POST /nodes/register` | Register declared node capacity |
| `GET /nodes` | Read registration and freshness state |
| `POST /nodes/{id}/heartbeat` | Refresh node registration |
| `POST /jobs` | Queue an allocation with an idempotency key |
| `GET /jobs` | List jobs oldest first |
| `GET /jobs/{id}` | Read compute status, limits, exit code, and cleanup state |
| `POST /jobs/{id}/stdin` | Append an idempotent opaque input line |
| `POST /jobs/{id}/stdin/close` | Close input after queued lines are delivered |
| `GET /jobs/{id}/stdout?after=0` | Read output after a cursor |
| `POST /jobs/{id}/cancel` | Cancel queued or running work |
| `GET /health` | API process health |

Job listings accept `status`, `resource_id`, `node_id`, `limit` (1–200, default 50), and `offset`. Responses contain `jobs`, `total`, `limit`, and `offset`. Unknown job IDs return 404; invalid input returns 422.

## Runtime and cleanup

The Docker backend uses a read-only root filesystem, non-root user, disabled networking, dropped capabilities, no privilege escalation, bounded CPU/memory/scratch/runtime, and a 128-process limit. Images must be installed beforehand, must not declare volumes, and are pinned to their inspected immutable ID. No submitted value controls the image, command, mount, environment, or network. No host directories, Docker socket, or host credentials enter the container.

Compute ownership uses 120-second renewable leases. Cancellation and lease loss stop local supervision and trigger removal of the named remote container. A daemon outage, forced worker kill, or ambiguous creation outcome can prevent confirmation. Such jobs expose `cleanup_required`, and their capacity stays reserved until a trusted operator reconciles the container and acknowledges cleanup. Public clients cannot release reservations. Do not reuse held capacity based only on a disconnected client or expired lease.

Docker containers share their host kernel. Use dedicated execution hosts or a stronger isolation runtime where needed. The service provisions containers on configured hosts; it does not create cloud VMs, install Docker, or provision Crusoe infrastructure. See [deployment notes](deploy/crusoe.md).

## Echo example

The included example demonstrates generic I/O without an application protocol:

```sh
docker build -f examples/Dockerfile.echo -t compute-echo:local .
```

Build or load this image on the Docker host selected by the resource's context. Configure the `local-cpu` pool with `node_id: compute-node-1`, `image: compute-echo:local`, the intended context, and `enabled: true`; keep quotas within the registered host's capacity. Register the example node, start the worker, and submit `examples/job.json`. The process echoes each input line and exits on EOF. It does not parse JSON or implement a verification-agent protocol.

## Configuration and checks

| Variable | Purpose |
| --- | --- |
| `RESOURCE_MANAGER_DB_PATH` | Own local SQLite database; default `ResourceManager/data/resource-manager.sqlite3` |
| `RESOURCE_MANAGER_API_KEY` | Optional shared bearer credential for protected API routes |
| `RESOURCE_MANAGER_RESOURCES_DIR` | Operator inventory; defaults to sibling `Resources` |

`.env.example` is a reference and is not loaded automatically. Preserve the manager database across restarts, including job and idempotency records. It uses independent `compute_nodes` and `compute_jobs` tables and leaves historical paper tables untouched. Do not share SQLite over a network filesystem or reuse Library's database. ResourceManager requires no LLM-provider or verification-API credentials.

```sh
uv run pytest
uv run ruff check .
```

Tests cover generic job contracts, idempotency, node freshness, capacity reservations, opaque channels, cancellation, leases, runtime restrictions, and cleanup retention. A real Docker host and installed image are required for a live execution run.
