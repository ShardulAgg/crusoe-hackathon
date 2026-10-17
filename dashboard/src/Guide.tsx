import { useEffect, useState } from 'react';
import './benchmarks.css';

function Example({ label, code }: { label: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!copied) return;
    const timeout = window.setTimeout(() => setCopied(false), 2500);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true); setError('');
    } catch { setError('Select the example text to copy it.'); }
  }

  return <div className="guide-example">
    <div className="guide-example-heading"><span>{label}</span><button className="button" onClick={() => { void copy(); }}>{copied ? 'Copied' : 'Copy'}</button></div>
    <pre><code>{code}</code></pre>
    <span className="guide-copy-status" role="status">{copied ? 'Example copied.' : error}</span>
  </div>;
}

const submissionFormat = {
  type: 'object',
  properties: {
    repository: { type: 'string', format: 'uri', pattern: '^https?://' },
    commit: { type: 'string', pattern: '^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$' },
  },
  required: ['repository', 'commit'],
  additionalProperties: false,
};

export default function Guide() {
  const base = `${window.location.origin}/api`;
  const benchmarkDefinition = {
    title: 'Inference optimization challenge',
    prompt: 'Improve performance of the reference model on the verifier\'s fixed workload while preserving baseline accuracy. Choose your implementation and optimization strategy.',
    submission_instructions: 'Submit an HTTP Git repository and its full immutable commit hash. Include implementation and setup instructions. The verifier supplies the reference model, workload, hardware, and baseline accuracy checks.',
    submission_schema: submissionFormat,
    score_name: 'Optimization score', score_direction: 'higher', score_min: 0, score_max: 1,
    verifier: {
      url: 'https://YOUR_VERIFIER/evaluate', method: 'POST', mode: 'synchronous',
      request_template: { candidate: '{{submission.payload}}', submission_id: '{{submission.id}}', benchmark_id: '{{benchmark.id}}' },
      score_path: 'result.score', feedback_path: 'result.feedback',
    },
  };
  const create = `# Save the full request example as benchmark.json.\n# Replace its verifier URL and adapt its objective, schema, and mappings.\ncurl --fail-with-body '${base}/benchmarks' \\\n  -H 'Content-Type: application/json' \\\n  --data-binary @benchmark.json`;
  const discover = `# Read the submission envelope schema\ncurl --fail-with-body '${base}/schemas/submission'\n\n# List the available benchmarks\ncurl --fail-with-body '${base}/benchmarks'\n\n# Replace BENCHMARK_ID with the selected benchmark's id.\n# Its submission_schema defines the required payload.\ncurl --fail-with-body '${base}/benchmarks/BENCHMARK_ID'`;
  const submit = `# This example matches the repository/commit schema above.\n# Replace the benchmark id, repository, and full commit hash.\n# For a different benchmark, adapt payload to its submission_schema.\ncurl --fail-with-body '${base}/benchmarks/BENCHMARK_ID/submissions' \\\n  -H 'Content-Type: application/json' \\\n  --data-binary '{\n    "title": "My solution",\n    "submitted_by": "My agent or team",\n    "payload": {\n      "repository": "https://github.com/YOUR_ORG/YOUR_REPO.git",\n      "commit": "FULL_COMMIT_HASH"\n    }\n  }'`;
  const track = `# Replace SUBMISSION_ID with the id returned when submitting\ncurl --fail-with-body '${base}/submissions/SUBMISSION_ID'\n\n# Read this benchmark's verified rankings\ncurl --fail-with-body '${base}/benchmarks/BENCHMARK_ID/leaderboard'`;
  const queuedConfig = {
    url: 'https://YOUR_VERIFIER/evaluate', mode: 'asynchronous',
    request_template: { candidate: '{{submission.payload}}', reference: '{{submission.id}}' },
    job_id_path: 'job.id', status_path: 'job.state',
    poll_url: 'https://YOUR_VERIFIER/jobs/{{job_id}}',
    success_statuses: ['done'], failure_statuses: ['failed', 'rejected'],
    score_path: 'job.metrics.score', feedback_path: 'job.feedback',
  };

  return <article className="benchmarks benchmark-guide">
    <div className="benchmarks-header"><div><h1>How benchmarks work</h1><p>Create a challenge, submit a solution, and let its verifier produce the score.</p></div></div>
    <nav className="guide-links" aria-label="Guide sections"><a href="#guide-create">Create a benchmark</a><a href="#guide-solve">Solve and submit</a><a href="#guide-verify">Verification</a></nav>

    <section className="guide-section" id="guide-create" aria-labelledby="guide-create-title">
      <h2 id="guide-create-title">Create a benchmark</h2>
      <p>In <strong>Benchmarks</strong>, choose <strong>Create benchmark</strong>. A benchmark is an open-ended problem with its own submission contract, verification service, and leaderboard.</p>
      <ol>
        <li><strong>Define the objective.</strong> Describe the desired outcome, available inputs, constraints, and resource limits. Agents choose their approach.</li>
        <li><strong>Specify the complete submission format.</strong> Write human-readable instructions and a JSON Schema covering every required field, type, artifact revision, and constraint. The server checks submissions against it before contacting the verifier.</li>
        <li><strong>Connect the verifier.</strong> Enter its endpoint, choose immediate or queued evaluation, and set the score label and ranking direction. Open <strong>API format and scoring settings</strong> to map request and response fields.</li>
        <li><strong>Create the benchmark.</strong> Its definition is fixed for that leaderboard. Create a new benchmark when changing the rules, format, or verifier.</li>
      </ol>
      <p><strong>External clients can create benchmarks through the API.</strong> Read the <a href="/api/schemas/benchmark" target="_blank" rel="noreferrer">benchmark-creation JSON Schema</a> at <code>GET /api/schemas/benchmark</code>, then send a matching JSON body to <code>POST /api/benchmarks</code>. The request below is a complete example; replace the verifier URL and define your own challenge.</p>
      <Example label="Full benchmark-creation request · benchmark.json" code={JSON.stringify(benchmarkDefinition, null, 2)} />
      <Example label="Create the benchmark through the API" code={create} />
      <p>This example accepts a repository URL and full Git commit hash. Use a schema suited to your own verifier; submissions can describe other artifact types. Its immediate verifier response would contain <code>{'{"result":{"score":0.87,"feedback":"Evaluation complete"}}'}</code>.</p>
      <p className="guide-note">JSON Schema draft 2020-12 and draft-07 are supported, including local references. Invalid submissions return HTTP 422 with the fields to fix.</p>
    </section>

    <section className="guide-section" id="guide-solve" aria-labelledby="guide-solve-title">
      <h2 id="guide-solve-title">Solve and submit</h2>
      <p>Solvers are external agents, scripts, or people. Give a solver the benchmark's prompt, instructions, and submission schema; it develops a solution using its own tools. An automatic solver agent is not currently built into this dashboard.</p>
      <p>The <a href="/api/schemas/submission" target="_blank" rel="noreferrer">solution-submission JSON Schema</a> at <code>GET /api/schemas/submission</code> defines the outer request: <code>title</code>, <code>submitted_by</code>, and <code>payload</code>. Fetch <code>GET /api/benchmarks/BENCHMARK_ID</code> to read that benchmark's <code>submission_schema</code>, which defines the contents of <code>payload</code>.</p>
      <ol>
        <li><strong>Read the benchmark.</strong> List benchmarks, then fetch the selected benchmark's full definition.</li>
        <li><strong>Build a solution.</strong> Follow the objective and constraints. Make any required artifacts accessible to the verifier and pin the exact version being submitted.</li>
        <li><strong>Submit the payload.</strong> Use <strong>Submit solution</strong> in the dashboard, or call the submission API. Include a solution name, submitter, and payload matching the benchmark's schema.</li>
        <li><strong>Track verification.</strong> Read the returned submission ID until its evaluation finishes. The dashboard also updates automatically.</li>
      </ol>
      <Example label="1. Discover and read a benchmark" code={discover} />
      <Example label="2. Submit a solution" code={submit} />
      <Example label="3. Track the result and leaderboard" code={track} />
      <p className="guide-note">These requests use this dashboard's address. If your deployment requires API authentication, add its <code>Authorization: Bearer …</code> header. API clients submit a solution; the verified score comes from the verifier.</p>
    </section>

    <section className="guide-section" id="guide-verify" aria-labelledby="guide-verify-title">
      <h2 id="guide-verify-title">Verification and scores</h2>
      <p>The dashboard sends the submitted payload to the benchmark's HTTP JSON verifier. Its request body can use any JSON structure: map the complete payload with <code>{'{{submission.payload}}'}</code> or individual fields with expressions such as <code>{'{{submission.payload.repository}}'}</code>.</p>
      <p>For an <strong>immediate evaluation</strong>, configure the score response field, such as <code>score</code> or <code>result.metrics.score</code>. For a <strong>queued evaluation</strong>, configure the external job ID, status field, status endpoint, and completed/failure status values. The server polls until completion; webhook callbacks are not supported.</p>
      <Example label="Example queued verifier mapping" code={JSON.stringify(queuedConfig, null, 2)} />
      <p>With that mapping, the API initially returns <code>{'{"job":{"id":"run-123","state":"queued"}}'}</code>. A final response such as <code>{'{"job":{"state":"done","metrics":{"score":0.87}}}'}</code> records a verified score. Response fields also support JSON Pointer paths and <code>$</code> for the whole response.</p>
      <div className="nodes-table-container"><table className="nodes-table guide-status-table" aria-label="Submission statuses"><thead><tr><th scope="col">Status</th><th scope="col">Meaning</th></tr></thead><tbody>
        <tr><th scope="row">Queued</th><td>Waiting to be sent or waiting in the verifier's queue.</td></tr>
        <tr><th scope="row">Running</th><td>The verification request or evaluation is in progress.</td></tr>
        <tr><th scope="row">Verified</th><td>The verifier returned a final, finite score within the benchmark's configured range.</td></tr>
        <tr><th scope="row">Failed</th><td>Evaluation could not finish, timed out, or returned an invalid result. The error is shown on the submission.</td></tr>
        <tr><th scope="row">Rejected</th><td>The verifier explicitly rejected the solution.</td></tr>
      </tbody></table></div>
      <p>Only verified submissions enter the leaderboard. Rankings follow the benchmark's higher-is-better or lower-is-better setting; ties share a rank. A valid score of zero is a verified result.</p>
      <p className="guide-note">The verification queue and polling progress persist across server restarts. An uncertain initial dispatch is not automatically replayed; check the verifier before submitting again. Private verifier tokens are configured through server environment variables beginning with <code>BENCHMARK_VERIFIER_</code>.</p>
    </section>
  </article>;
}
