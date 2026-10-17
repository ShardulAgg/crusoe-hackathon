# Library

Library accepts papers and runs their verification workflow. Its LLM judge reads the paper, extracts requirements and experimental conditions, and chooses the minimum sufficient resources from the separate [ResourceManager service](../ResourceManager/README.md). Library owns the verification agent's task, restricted verification API bridge, report, and verdict. ResourceManager supplies generic compute jobs and their input/output channels.

```text
Submitting agent → Library paper queue → Library LLM judge
                                             ↕
                                  ResourceManager GET /resources
                                             ↓
                                 Library verification orchestrator
                                             ↕
                              ResourceManager POST /jobs + channels
                                             ↕
                                  Isolated compute environment
                                  running Library's verification agent
                                             ↕ opaque job I/O
                                    Library verification API bridge
                                             ↕
                                        Verification API
```

ResourceManager does not interpret papers, verification conditions, or verdicts. A compute job's exit code is separate from a scientific result. The compute request contains resource allocations; Library sends the agent's task over the job's opaque input channel.

## Start Library

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```sh
cd Library
uv sync --python 3.12
uv run uvicorn paper_library.main:app --host 127.0.0.1 --port 8001
```

API docs: <http://127.0.0.1:8001/docs>. Machine-readable contract: `/openapi.json`.

In another terminal, configure and start the Library verification worker:

```sh
cd Library
export LIBRARY_RESOURCE_MANAGER_URL=http://127.0.0.1:8002
export LIBRARY_PLANNER_BASE_URL=https://your-llm-endpoint.example/v1
export LIBRARY_PLANNER_MODEL=YOUR_MODEL_ID
# Set LIBRARY_PLANNER_API_KEY and LIBRARY_RESOURCE_MANAGER_API_KEY as needed.
export LIBRARY_VERIFICATION_API_URL=http://127.0.0.1:8000
# Set LIBRARY_VERIFICATION_API_KEY if the verification API requires authentication.
uv run python -m paper_library.worker
```

Start ResourceManager separately using its [setup instructions](../ResourceManager/README.md). It manages node registration, the operator-owned [Resources inventory](../Resources/README.md), compute reservations, and container execution. Library communicates with it through HTTP and does not need Docker access.

## Submit a paper

```sh
curl --fail-with-body http://127.0.0.1:8001/papers \
  -H 'Content-Type: application/json' \
  --data-binary @examples/paper.json
```

Every submission requires `title` and at least one of `paper_url` or `paper_text`. Optional fields are `authors`, `submitted_by`, and `notes`. Requirements come from the paper itself; no separate requirements manifest is required. Replace the illustrative example with the actual paper before submission.

Submissions return HTTP 202, a unique `id`, `status: pending_verification`, and a `Location` header. Repeated submissions create separate entries. URLs are stored as references, not downloaded. Include `paper_text` so the judge can read the paper. File uploads are not implemented.

| Request | Purpose |
| --- | --- |
| `POST /papers` | Submit a paper |
| `GET /papers` | List submissions oldest first |
| `GET /papers?status=pending_verification` | List papers awaiting verification |
| `GET /papers/{id}` | Read the saved plan, compute progress, verification report, or error |
| `POST /papers/{id}/retry` | Requeue an eligible blocked or failed paper |
| `GET /health` | Check the Library API process |

List requests support `limit` (1–100, default 50) and `offset` (default 0), returning `papers`, `total`, `limit`, and `offset`. Unknown IDs return 404 and invalid input returns 422.

## Verification workflow

Library asks ResourceManager for available resources. The judge reads `paper_text` and records claims, procedures, exact conditions, measurable acceptance criteria, and verbatim `source_quotes` in `plan.verification_requirements`. It flags missing or ambiguous details in `plan.missing_information`, without inventing conditions or lowering thresholds. Quotes must occur in the source paper; quote membership establishes provenance rather than proving the interpretation is complete or correct.

The judge selects the minimum estimated sufficient CPU, memory, scratch space, and runtime. Resource capacities are ceilings. An agent that coordinates API calls does not need to duplicate the GPU resources used behind the verification API. Library validates the proposed allocation against ResourceManager's available snapshot and retains the plan with the paper.

Library requests a generic compute job using `POST /jobs` with only `resource_id`, `node_id`, `cpus`, `memory_mb`, `scratch_mb`, and `timeout_seconds`. A stable `Idempotency-Key` identifies the allocation attempt so a request timeout can be retried without creating another compute job. ResourceManager controls the approved image, host mapping, quotas, and fresh on-demand execution environment.

The paper's `compute_job_id` and `compute_job_status` track that allocation. Library sends its verification task through the job's input channel, reads agent output, and handles requests to the verification API itself. ResourceManager transports those opaque UTF-8 lines without applying a paper schema or deciding what they mean. Library records a verification outcome only after validating the agent's report and compute-job completion. A successful compute exit alone does not mark a paper verified.

Paper progress is `pending_verification → planning → verifying → completed`, with `blocked` and `failed` outcomes where appropriate. Reports contain a `verified`, `not_verified`, or `inconclusive` verdict with evidence. The original paper remains unchanged. Retries preserve that paper and begin a fresh assessment; active and completed papers cannot be retried. Unconfirmed remote cleanup must be resolved before a new attempt may start.

## Restricted verification access

The verification agent runs in ResourceManager's isolated compute environment with direct networking disabled. Library mediates the agent's only external capability through its job I/O channel:

- `GET /health` and `GET /openapi.json` on the configured verification API.
- `POST /jobs` to request an inference verification job.
- `GET /jobs/{id}` and `POST /jobs/{id}/cancel` for verification jobs created by that agent run.

Library rejects arbitrary destinations, redirects, worker/admin routes, and access to other runs' jobs. It attaches the verification API credential on the trusted Library side. Planner credentials, ResourceManager credentials, and verification API keys never enter the agent task. ResourceManager's generic job API does not implement these verification-specific permissions.

The compute image must contain a compatible agent runtime and dependencies. The standard-library `paper_library/agent_protocol.py` client defines Library's task, API-request, and result messages. That protocol belongs to Library; ResourceManager's channels carry opaque UTF-8 lines. Any model used within the isolated environment must be available locally or through an explicitly supported verification API capability.

## Configuration

| Variable | Purpose |
| --- | --- |
| `LIBRARY_DB_PATH` | Library SQLite file; default `Library/data/library.sqlite3` |
| `LIBRARY_API_KEY` | Optional bearer key for Library paper endpoints |
| `LIBRARY_RESOURCE_MANAGER_URL` | ResourceManager API base URL, for example `http://127.0.0.1:8002` |
| `LIBRARY_RESOURCE_MANAGER_API_KEY` | Credential for generic compute requests and job channels |
| `LIBRARY_PLANNER_BASE_URL` | Library's compatible LLM endpoint |
| `LIBRARY_PLANNER_MODEL` | Explicit planner model ID |
| `LIBRARY_PLANNER_API_KEY` | Planner provider credential |
| `LIBRARY_VERIFICATION_API_URL` | Inference verification API base URL |
| `LIBRARY_VERIFICATION_API_KEY` | Credential for Library's restricted verification bridge |

`.env.example` lists settings for reference; it is not loaded automatically. Paper contents are sent to the configured planner provider. Library API and worker share one local persistent database. ResourceManager uses its own independent database; do not share SQLite through a network filesystem.

All configured URLs are reached from the Library worker's machine. Use protected HTTPS endpoints between hosts. Keep these credentials on Library's trusted host. Without `LIBRARY_API_KEY`, anyone who can reach Library can submit and read papers. Health and API docs remain public.

Configure image selection, Docker contexts, node registration, and resource quotas through ResourceManager and the separate [Resources inventory](../Resources/README.md). Library selects allocations from the public resource snapshot; private image and execution-host settings stay inside ResourceManager.

## Deployment and checks

See [the Crusoe deployment notes](deploy/crusoe.md) for service placement.

```sh
uv run pytest
uv run ruff check .
```

Library tests cover paper intake, requirement extraction, resource selection, agent protocol, restricted verification access, and verification outcomes. ResourceManager tests cover generic compute jobs, reservations, execution, and channels.
