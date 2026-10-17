# Dashboard

React and TypeScript dashboard with its own SQLite database, benchmark leaderboards, and local resource API. Requires Node.js 24 or later.

```sh
npm install
npm run dev
```

The UI runs at `http://127.0.0.1:5173` and the API at `http://127.0.0.1:3001`. Both bind to loopback. `npm run build` checks TypeScript and builds the UI. `npm start` serves the built UI and API together at port 3001. `npm test` checks benchmark contracts and verification, persistence, reservations, worker cleanup, HTTP routes, and Crusoe capacity handling.

## Benchmarks

**Benchmarks** replaces the Tasks tab. Choose **Create benchmark** to define an open-ended objective, submission instructions, a full submission format, and the verification API that scores solutions. Open the benchmark and choose **Submit solution** to provide a name, submitter, and JSON payload. The dashboard shows verification progress, feedback, submitted payloads, and the resulting leaderboard. No example benchmarks or scores are inserted automatically.

Every benchmark has a `submission_schema` JSON Schema object. The server validates the schema at creation and validates each payload before queueing verification; invalid payloads return HTTP 422. Draft 2020-12 is the default, and explicit draft-07 is also supported. Local fragment references such as `#/$defs/solution` work; remote references are rejected. Validation checks types, required fields, formats, patterns, and other schema constraints without coercing values or inserting defaults. Define artifact revisions, inputs, and other reproducibility requirements in the schema and instructions.

Benchmark definitions are immutable. Create a new benchmark when changing the objective, submission contract, verifier, or scoring rules so existing leaderboard scores keep their original meaning.

### Verification integration

The verifier can use its own HTTP JSON request and response format. Configure:

- `url`, `method` (`POST` or `PUT`), and an arbitrary JSON `request_template`.
- `score_path` and optional `feedback_path` to read response values. Paths support dots (`result.metrics.score`), JSON Pointer (`/result/metrics/score`), and `$` for the complete response.
- `mode: "synchronous"` for a response containing the final score, or `"asynchronous"` for a queued job. Async mode requires `poll_url`, `job_id_path`, `status_path`, and the API's completed/failure status values. The dashboard polls with GET; callbacks are not implemented.
- An optional `auth_env` naming a server environment variable beginning with `BENCHMARK_VERIFIER_`. Its value becomes the verifier's bearer token and stays on the server. Authenticated polling must use the same origin as the initial endpoint. Redirects are rejected.

Request templates support `{{submission.payload}}`, nested payload values such as `{{submission.payload.repository}}`, `{{submission.id}}`, `{{submission.title}}`, `{{submission.submitted_by}}`, `{{benchmark.id}}`, `{{benchmark.title}}`, and `{{benchmark.version}}`. A whole-value placeholder preserves the original JSON type; placeholders inside strings interpolate text. Placeholders are values, not property names. Poll URLs use `{{job_id}}`, which is URL-encoded before requesting the job status.

`server/index.ts` starts the verification worker automatically with the API. The SQLite queue persists across restarts, including external job references and polling progress. An interrupted or uncertain initial dispatch is marked failed rather than automatically replayed, because the verifier may already have accepted it; inspect the verifier before submitting again. Outgoing submissions include their stable ID as `Idempotency-Key`.

Only final, finite numeric scores within the configured optional minimum/maximum enter the leaderboard. Async jobs must explicitly report a configured completed status. Pending, failed, and rejected submissions never rank; a valid zero score does. Rankings support higher-is-better or lower-is-better, with equal scores sharing a rank. The leaderboard returns up to 1,000 entries plus the total verified count. Submission history supports pagination. Evaluation errors remain distinct from scores.

### API example

External clients can discover the machine-readable request contracts:

- [Benchmark-creation JSON Schema](http://127.0.0.1:3001/api/schemas/benchmark): `GET /api/schemas/benchmark` defines the body for `POST /api/benchmarks`.
- [Solution-submission JSON Schema](http://127.0.0.1:3001/api/schemas/submission): `GET /api/schemas/submission` defines the outer submission envelope (`title`, `submitted_by`, `payload`).
- `GET /api/benchmarks/{id}` returns the selected benchmark's `submission_schema`, which defines the contents of `payload`.

Use the deployed dashboard's origin in place of `http://127.0.0.1:3001` when connecting remotely. The **Guide** tab includes these schema links and copyable creation/submission examples using its current origin.

This example assumes you operate a verifier at `http://127.0.0.1:9000`; replace its routes and field mappings with your service's contract. Save the following as `benchmark.json`:

```json
{
  "title": "Inference throughput challenge",
  "prompt": "Improve throughput for the reference model on the verifier's fixed workload while preserving baseline accuracy. Choose your implementation and optimization strategy.",
  "submission_instructions": "Submit an HTTP Git repository and its full immutable commit hash. The repository must include the implementation and setup instructions required by the evaluator. The verifier provides the reference model, workload, hardware, and accuracy checks.",
  "submission_schema": {
    "type": "object",
    "properties": {
      "repository": { "type": "string", "format": "uri", "pattern": "^https?://" },
      "commit": { "type": "string", "pattern": "^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$" }
    },
    "required": ["repository", "commit"],
    "additionalProperties": false
  },
  "score_name": "Throughput (tokens/s)",
  "score_direction": "higher",
  "score_min": 0,
  "verifier": {
    "url": "http://127.0.0.1:9000/evaluations",
    "mode": "asynchronous",
    "request_template": {
      "source": {
        "repository": "{{submission.payload.repository}}",
        "commit": "{{submission.payload.commit}}"
      },
      "submission_id": "{{submission.id}}"
    },
    "job_id_path": "evaluation.id",
    "status_path": "evaluation.state",
    "poll_url": "http://127.0.0.1:9000/evaluations/{{job_id}}",
    "success_statuses": ["done"],
    "failure_statuses": ["failed", "rejected"],
    "score_path": "evaluation.metrics.throughput",
    "feedback_path": "evaluation.feedback"
  }
}
```

Create the benchmark, then use its returned `id` to submit a solution. Replace the example repository and commit with your actual artifact:

```sh
curl --fail-with-body http://127.0.0.1:3001/api/benchmarks \
  -H 'Content-Type: application/json' --data-binary @benchmark.json

curl --fail-with-body http://127.0.0.1:3001/api/benchmarks/BENCHMARK_ID/submissions \
  -H 'Content-Type: application/json' \
  --data-binary '{"title":"Optimization attempt 1","submitted_by":"research-agent","payload":{"repository":"http://127.0.0.1:9001/solution.git","commit":"0123456789abcdef0123456789abcdef01234567"}}'
```

The initial verifier response can be `{"evaluation":{"id":"run-123","state":"queued"}}`. Its eventual completed response must include `evaluation.state: "done"` and a numeric `evaluation.metrics.throughput`. A string such as `"1000"` is not a numeric score.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/schemas/benchmark` | Machine-readable JSON Schema for creating a benchmark |
| `GET /api/schemas/submission` | Machine-readable JSON Schema for the submission envelope |
| `POST /api/benchmarks` | Create an immutable benchmark; HTTP 201 |
| `GET /api/benchmarks` | List benchmarks and submission/score summaries |
| `GET /api/benchmarks/{id}` | Read the full benchmark contract |
| `POST /api/benchmarks/{id}/submissions` | Validate and queue a solution; HTTP 202 |
| `GET /api/benchmarks/{id}/submissions` | History with `limit` (1–500) and `offset` |
| `GET /api/benchmarks/{id}/leaderboard` | Verified entries and total |
| `GET /api/submissions/{id}` | Submission, verification state, score, and feedback |

If `DASHBOARD_API_KEY` is enabled, include its bearer authentication header on these requests.

## Database ownership

The dashboard owns `data/dashboard.sqlite`, independent of the sibling Library and Verification API databases. It persists these tables:

| Table | Contents |
| --- | --- |
| `nodes` | Identity, specifications, provider/region, availability mode, last heartbeat |
| `jobs` | Job requirements, assigned node, status, lease, held allocation, timestamps |
| `offerings` | Published Crusoe GPU server specifications and access model |
| `cloud_capacities` | Last successful Crusoe capacity snapshot by server type and location |
| `provider_sync` | Successful check time and safe provider error message |
| `benchmarks` | Immutable objectives, submission schemas, verifier integrations, scoring rules |
| `benchmark_submissions` | Submitted payloads, durable verification queue, external job references, scores and feedback |

The database and `.env` files are ignored. Set `DASHBOARD_DB_PATH` to change the database location. Back up the SQLite database with SQLite's backup API or stop the server before copying the database and its WAL files.

Workers must report registrations and job lifecycle events to this API. Existing sibling services are not automatically mirrored, and recording an allocation does not launch a process or provision a VM.

## Nodes and capacity

Register a node in Resources or send `POST /api/nodes/register` with:

```json
{
  "id": "gpu-node-01",
  "name": "GPU node 01",
  "provider": "crusoe",
  "region": "us-east1-a",
  "allocation_mode": "on_demand",
  "cpus": 12,
  "memory_mb": 122880,
  "gpus": 1,
  "gpu_model": "NVIDIA A100 PCIe",
  "gpu_memory_mb": 81920,
  "max_concurrent_jobs": 1,
  "specifications": { "os": "Linux", "cuda_version": "12.8" }
}
```

Values are operator/worker declarations, not hardware discovery. Use actual allocated capacity. Memory fields use MiB, GPU memory is per GPU, GPU counts are whole devices, and CPU allocations support increments of 0.001 cores. No example node is inserted automatically.

Nodes must send `POST /api/nodes/{id}/heartbeat` at least every two minutes; every 30 seconds is recommended. Offline nodes advertise zero available capacity. A job heartbeat also refreshes its assigned node.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/nodes` | Specifications, free/total capacity, held jobs, liveness |
| `GET /api/nodes/{id}` | One node |
| `POST /api/nodes/register` | Register or update an idle node |
| `POST /api/nodes/{id}/heartbeat` | Report node liveness |
| `GET /api/nodes/{id}/jobs` | Paginated job history for a node |
| `GET /api/resources/available` | Match currently available resources |

Availability queries support `cpus`, `memory_mb`, `gpus`, `gpu_memory_mb`, `gpu_model`, `provider`, `region`, and `allocation_mode`. For example, `/api/resources/available?gpus=1&memory_mb=16384&allocation_mode=on_demand`. Results are advisory snapshots; starting a job checks and reserves capacity atomically.

## Job lifecycle

1. `POST /api/jobs` creates a queued job: `{"title":"Verification run","cpus":4,"memory_mb":8192,"gpus":1}`. Optional `metadata` stores integration identifiers.
2. `POST /api/jobs/{id}/start` with `{"node_id":"gpu-node-01"}` atomically reserves CPU, RAM, GPU count, and a job slot. It returns the job and a `lease_token` for its worker. Concurrent claims cannot exceed registered limits.
3. Send `POST /api/jobs/{id}/heartbeat` with `{"lease_token":"..."}` every 30 seconds. The lease lasts two minutes.
4. After the process and its children have stopped, send `POST /api/jobs/{id}/finish` with `{"lease_token":"...","status":"completed","cleanup_confirmed":true}`. Use `status: "failed"` and an optional `error` for failures.

`POST /api/jobs/{id}/cancel` records cancellation. The worker must stop execution and then call `finish` with its original token to attest cleanup. Cancellation and expired leases **retain allocations** until cleanup is confirmed. They cannot silently make a still-running process's hardware available again. Cleanup does not overwrite an already failed or cancelled outcome.

`GET /api/jobs` accepts `node_id`, `status`, `limit` (1–500), and `offset`. `GET /api/jobs/{id}` returns one record. Public reads never return claim tokens. Node specification changes are rejected while any allocation is held. This registry accounts for declared reservations; it does not enforce operating-system resource limits or discover unrelated processes.

An optional `DASHBOARD_API_KEY` protects API requests with bearer authentication. In development, Vite reads the same key from `.env` and injects it server-side; it is never bundled into the frontend. With `npm start`, an authenticated client must provide the header if this option is enabled.

## Crusoe GPU servers

The initial catalog records the [published VM specifications](https://docs.crusoecloud.com/compute/virtual-machines/overview/) checked on October 17, 2026. [Crusoe's access-model guidance](https://support.crusoecloud.com/hc/en-us/articles/51755978765467-GPU-Quota-vs-Capacity-Understanding-On-Demand-and-Reserved-Instances-RIPA) classifies A100 PCIe and L40S as on demand, and H100 SXM, A100 SXM, MI300X, B200, B300, and GB200 as requiring reservations. Unconfirmed access models use “Contact sales.” Published server types are not registered nodes or guaranteed stock.

Copy `.env.example` to `.env` and configure `CRUSOE_ACCESS_KEY_ID` and `CRUSOE_SECRET_KEY` locally. Restart the API, then choose **Check live capacity** in Resources. `POST /api/providers/crusoe/sync` makes an authenticated **GET** to `https://api.cloud.crusoe.ai/v1/capacities`, using [Crusoe's documented HMAC authentication](https://docs.crusoecloud.com/reference/api/). Secrets stay on the server and are neither persisted in the database nor returned to the UI. Redirects are rejected.

`GET /api/providers/crusoe` returns the catalog, connection state, last successful check, and stock snapshot. Snapshots become stale after 60 seconds. Failed checks preserve the previous snapshot and label it stale. Empty successful snapshots clear prior stock. The UI also exposes all reported server types and locations, including products not yet in the published catalog. Quantities and slice counts are preserved as reported; they are not converted into guaranteed launchable VM counts.

Capacity and account quota are separate. This integration reads stock; it does not check every account entitlement, create VMs, reserve stock, or incur compute charges. Real-time capacity cannot be verified until valid Crusoe credentials are connected.
