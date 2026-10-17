import assert from 'node:assert/strict';
import test from 'node:test';
import { benchmarkSchema, submissionSchema } from '../shared/benchmarks.ts';
import { BenchmarkStore } from './benchmark-store.ts';
import { ResourceStore } from './store.ts';
import { VerificationWorker } from './verification-worker.ts';

function definition(verifier: Record<string, unknown> = {}) {
  return benchmarkSchema.parse({
    title: 'Verifier boundaries', prompt: 'Solve the objective.',
    submission_schema: { type: 'object' },
    verifier: { url: 'https://verifier.example/evaluate', ...verifier },
  });
}

function mock(handler: (url: string, init: RequestInit) => Response | Promise<Response>): typeof fetch {
  return (async (url: string | URL | Request, init: RequestInit = {}) => handler(String(url), init)) as typeof fetch;
}

for (const [httpStatus, status] of [[202, 'queued'], [200, 'running']] as const) {
  test(`synchronous mode cannot publish a provisional score from HTTP ${httpStatus} / ${status}`, async () => {
    const resources = new ResourceStore(':memory:');
    const store = new BenchmarkStore(resources);
    try {
      const benchmark = store.createBenchmark(definition());
      const submission = store.createSubmission(benchmark.id, submissionSchema.parse({ title: 'Solution', payload: {} }));
      const worker = new VerificationWorker(store, {
        request: mock(() => new Response(JSON.stringify({ status, score: 0.9 }), { status: httpStatus })),
      });
      await worker.tick();
      assert.notEqual(store.getSubmission(submission.id).status, 'verified');
      assert.equal(store.getSubmission(submission.id).score, null);
      assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
    } finally { resources.close(); }
  });
}

test('malformed authenticated asynchronous endpoints return schema errors without throwing', () => {
  const invalid = {
    ...definition(), verifier: {
      url: 'not-a-url', mode: 'asynchronous', poll_url: 'https://verifier.example/jobs/{{job_id}}',
      auth_env: 'BENCHMARK_VERIFIER_TEST',
    },
  };
  let result: ReturnType<typeof benchmarkSchema.safeParse> | undefined;
  assert.doesNotThrow(() => { result = benchmarkSchema.safeParse(invalid); });
  assert.equal(result?.success, false);
});

test('job IDs cannot alter the origin receiving verifier credentials', () => {
  const invalid = {
    ...definition(), verifier: {
      url: 'https://job-id.verifier.example/evaluate', mode: 'asynchronous',
      poll_url: 'https://{{job_id}}.verifier.example/result', auth_env: 'BENCHMARK_VERIFIER_TEST',
    },
  };
  assert.equal(benchmarkSchema.safeParse(invalid).success, false);
});

test('a transient polling network failure resumes the same remote job without replaying submission', async () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition({
      mode: 'asynchronous', poll_url: 'https://verifier.example/jobs/{{job_id}}', poll_interval_seconds: 1,
    }));
    const submission = store.createSubmission(benchmark.id, submissionSchema.parse({ title: 'Solution', payload: {} }));
    const methods: string[] = [];
    const worker = new VerificationWorker(store, { request: mock((_url, init) => {
      methods.push(String(init.method));
      if (methods.length === 1) return new Response('{"id":"job-123","status":"queued"}', { status: 202 });
      if (methods.length === 2) throw new TypeError('Temporary connection failure');
      return new Response('{"status":"completed","score":0}');
    }) });
    await worker.tick();
    now += 1001;
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).status, 'running');
    assert.equal(store.getSubmission(submission.id).external_job_id, 'job-123');
    now += 1001;
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).status, 'verified');
    assert.equal(store.getSubmission(submission.id).score, 0);
    assert.deepEqual(methods, ['POST', 'GET', 'GET']);
  } finally { resources.close(); }
});

test('expired dispatch leases fail safely and stale claims cannot add leaderboard scores', () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition());
    const submission = store.createSubmission(benchmark.id, submissionSchema.parse({ title: 'Solution', payload: {} }));
    const claim = store.claimDue()!;
    now += 60_001;
    assert.equal(store.claimDue(), null);
    assert.equal(store.getSubmission(submission.id).status, 'failed');
    store.finish(claim, 'verified', 0.99);
    assert.equal(store.getSubmission(submission.id).status, 'failed');
    assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
  } finally { resources.close(); }
});
