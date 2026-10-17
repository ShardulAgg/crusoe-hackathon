# Crusoe Hackathon

Open-ended leaderboard benchmarks with explicit submission contracts and independently verified scores.

## Run the dashboard

Requires Node.js 24 or later.

```sh
cd dashboard
npm ci
npm run dev
```

Open http://127.0.0.1:5173. The **Benchmarks** tab creates benchmarks and accepts solutions; the **Guide** explains benchmark authoring, external solvers, and verifier integration. The server persists benchmarks, submissions, and evaluation state in its own SQLite database. `npm start` serves a production build; run `npm run build` first.

## External API contract

- `POST /api/benchmarks`: create an immutable benchmark with `title`, `prompt`, `submission_schema`, and `verifier`. Also define human-readable `submission_instructions`, score label/direction, and optional score limits.
- `GET /api/schemas/benchmark`: full machine-readable JSON Schema for benchmark creation.
- `GET /api/benchmarks/{id}`: retrieve the objective, submission schema, scoring rules, and verifier configuration.
- `POST /api/benchmarks/{id}/submissions`: submit `{ "title": "Solution", "submitted_by": "solver", "payload": { ... } }`. The payload must satisfy that benchmark's full JSON Schema.
- `GET /api/schemas/submission`: machine-readable schema for the submission envelope.
- `GET /api/submissions/{id}`: poll evaluation progress, feedback, and verified score.
- `GET /api/benchmarks/{id}/leaderboard`: ranked verified submissions.

See [dashboard/README.md](dashboard/README.md) for a complete benchmark definition and API examples.

## Verification mechanism

1. The server validates the submission envelope and checks its payload against the benchmark's JSON Schema. Invalid solutions are rejected before any verifier request.
2. A durable verification queue sends the solution to the benchmark-owned HTTP JSON endpoint. Request templates and response paths adapt to the verifier's format.
3. Synchronous verifiers return a final score. Asynchronous verifiers return a job reference; the worker polls their configured status endpoint until completion or timeout.
4. Only finite final scores within the configured range become verified leaderboard entries. Pending results and failures receive no score. A valid zero score is preserved.

Service credentials stay in server environment variables. Lost submission acknowledgements are never automatically dispatched again. Persisted remote job references resume polling after restart. Benchmark definitions are immutable: create a new benchmark when evaluation rules change.

## How a solver works

A solver is an external person, agent, or program. It reads the benchmark prompt, constraints, and submission schema, chooses an approach, produces a matching JSON payload, submits it, and polls for the final result. This repository does not automatically launch solver agents.

## Components

| Directory | Purpose |
| --- | --- |
| `dashboard/` | Benchmark and resource dashboard, persistent API, verifier worker, instructions |
| `Library/` | Paper intake, LLM planning, restricted verification access, and verdicts |
| `ResourceManager/` | Generic compute queue, reservations, and opaque job channels |
| `Resources/` | Operator-owned resource inventory; example disabled by default |
| `verification api/` | Git submodule: separate Git-pinned runner service and execution nodes |

Clone with `git clone --recurse-submodules` to include the separate Verification API. The dashboard's endpoint-based benchmark verification runs independently of the paper-verification services.

## Checks

```sh
cd dashboard
npm test
npm run build
```

The tests cover schema enforcement, custom verifier formats, queued evaluation, final-score integrity, ranking, persistence, leases, timeouts, duplicate-dispatch prevention, and resource accounting.
