import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { benchmarkSchema, submissionSchema } from '../shared/benchmarks.ts';
import type { BenchmarkInput, Submission } from '../shared/benchmarks.ts';
import { createApp } from './app.ts';
import { BenchmarkStore } from './benchmark-store.ts';
import { ResourceStore, StoreError } from './store.ts';
import { VerificationWorker } from './verification-worker.ts';

function definition(overrides: Record<string, unknown> = {}): BenchmarkInput {
  return benchmarkSchema.parse({
    title: 'Open-ended optimization',
    prompt: 'Find an implementation that improves the objective while preserving correctness.',
    submission_instructions: 'Supply the numerical answer in the answer field.',
    submission_schema: {
      type: 'object', properties: { answer: { type: 'number' } }, required: ['answer'], additionalProperties: false,
    },
    verifier: { url: 'https://verifier.example/evaluate' },
    ...overrides,
  });
}

function solution(answer = 42, title = 'Candidate') {
  return submissionSchema.parse({ title, submitted_by: 'Research agent', payload: { answer } });
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function mockRequest(handler: (url: string, init: RequestInit) => Response | Promise<Response>): typeof fetch {
  return (async (url: string | URL | Request, init: RequestInit = {}) => handler(String(url), init)) as typeof fetch;
}

test('benchmark HTTP API persists complete submission definitions and rejects client-authored scores', async () => {
  const resources = new ResourceStore(':memory:');
  const benchmarks = new BenchmarkStore(resources);
  const server = createApp(resources, benchmarks).listen(0, '127.0.0.1');
  await once(server, 'listening');
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/api`;
  async function request(path: string, body?: unknown, method?: string) {
    const result = await fetch(url + path, {
      method: method ?? (body === undefined ? 'GET' : 'POST'),
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: result.status, body: await result.json() };
  }
  try {
    const missingSchema = { ...definition() } as Record<string, unknown>;
    delete missingSchema.submission_schema;
    assert.equal((await request('/benchmarks', missingSchema)).status, 400);
    const created = await request('/benchmarks', definition());
    assert.equal(created.status, 201);
    const benchmark = created.body;
    assert.deepEqual(benchmark.submission_schema, definition().submission_schema);
    assert.equal(benchmark.version, 1);
    assert.equal((await request('/benchmarks')).body.benchmarks.length, 1);
    assert.equal((await request(`/benchmarks/${benchmark.id}`)).body.prompt, definition().prompt);
    assert.equal((await request(`/benchmarks/${benchmark.id}/submissions`, {
      ...solution(), score: 999, status: 'verified',
    })).status, 400);
    assert.equal((await request(`/benchmarks/${benchmark.id}/submissions`, {
      ...solution(), payload: { answer: 'not a number' },
    })).status, 422);
    assert.equal((await request(`/benchmarks/${benchmark.id}/submissions`, {
      ...solution(), payload: { answer: 42, undeclared: true },
    })).status, 422);
    const accepted = await request(`/benchmarks/${benchmark.id}/submissions`, solution());
    assert.equal(accepted.status, 202);
    assert.equal(accepted.body.status, 'queued');
    assert.equal(accepted.body.score, null);
    assert.equal((await request(`/submissions/${accepted.body.id}`)).body.id, accepted.body.id);
    assert.equal((await request(`/benchmarks/${benchmark.id}/submissions`)).body.total, 1);
    assert.equal((await request(`/benchmarks/${benchmark.id}/leaderboard`)).body.entries.length, 0);
    assert.equal((await request(`/benchmarks/${benchmark.id}/submissions?limit=0`)).status, 400);
    assert.equal((await request('/benchmarks/missing')).status, 404);
    assert.equal((await request(`/benchmarks/${benchmark.id}`, definition(), 'PUT')).status, 404);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    resources.close();
  }
});

test('synchronous verifier receives the submitted JSON and zero is a verified score', async () => {
  const resources = new ResourceStore(':memory:');
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition());
    const submission = store.createSubmission(benchmark.id, solution(0));
    let calls = 0;
    const worker = new VerificationWorker(store, {
      request: mockRequest((url, init) => {
        calls += 1;
        assert.equal(url, benchmark.verifier.url);
        assert.equal(init.method, 'POST');
        assert.equal(init.redirect, 'error');
        assert.deepEqual(JSON.parse(String(init.body)), {
          submission: { answer: 0 }, submission_id: submission.id, benchmark_id: benchmark.id,
        });
        return response({ score: 0, feedback: 'Correct, with a valid zero score.' });
      }),
    });
    await worker.tick();
    const verified = store.getSubmission(submission.id);
    assert.equal(verified.status, 'verified');
    assert.equal(verified.score, 0);
    assert.equal(verified.feedback, 'Correct, with a valid zero score.');
    assert.notEqual(verified.completed_at, null);
    assert.equal(store.leaderboard(benchmark.id).entries[0].score, 0);
    assert.equal(store.getBenchmark(benchmark.id).best_score, 0);
    await worker.tick();
    assert.equal(calls, 1);
  } finally { resources.close(); }
});

test('queued verifier honors custom mappings, encoded job IDs, and only final scores', async () => {
  let now = 10_000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition({ verifier: {
      url: 'https://verifier.example/submit', mode: 'asynchronous',
      request_template: { candidate: '{{submission.payload}}', author: '{{submission.submitted_by}}' },
      poll_url: 'https://verifier.example/results/{{job_id}}',
      job_id_path: 'queue.reference', status_path: 'evaluation.state',
      score_path: 'evaluation.metrics.objective', feedback_path: 'evaluation.explanation',
      success_statuses: ['DONE'], failure_statuses: ['BROKEN', 'rejected'], poll_interval_seconds: 1,
    } }));
    const submission = store.createSubmission(benchmark.id, solution());
    const calls: string[] = [];
    const worker = new VerificationWorker(store, { request: mockRequest((url, init) => {
      calls.push(url);
      if (init.method === 'POST') {
        assert.deepEqual(JSON.parse(String(init.body)), { candidate: { answer: 42 }, author: 'Research agent' });
        return response({ queue: { reference: 'job/with spaces?' }, evaluation: { state: 'WAITING', metrics: { objective: 999 } } }, 202);
      }
      assert.equal(url, 'https://verifier.example/results/job%2Fwith%20spaces%3F');
      if (calls.length === 2) return response({ evaluation: { state: 'RUNNING', metrics: { objective: 1000 } } });
      return response({ evaluation: { state: 'DONE', metrics: { objective: 0.87 }, explanation: 'Measured independently.' } });
    }) });
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).score, null);
    assert.equal(store.getSubmission(submission.id).external_job_id, 'job/with spaces?');
    assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
    now += 1001;
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).score, null);
    assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
    now += 1001;
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).status, 'verified');
    assert.equal(store.getSubmission(submission.id).score, 0.87);
    assert.equal(store.getSubmission(submission.id).feedback, 'Measured independently.');
    assert.equal(calls.length, 3);
  } finally { resources.close(); }
});

test('leaderboards honor score direction, preserve tied ranks, and exclude unverified submissions', async () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    for (const direction of ['higher', 'lower'] as const) {
      const benchmark = store.createBenchmark(definition({ score_direction: direction }));
      const submissions: Submission[] = [];
      for (const score of [2, 7, 7]) {
        now += 1;
        submissions.push(store.createSubmission(benchmark.id, solution(score, `Score ${score}`)));
      }
      const worker = new VerificationWorker(store, { request: mockRequest((_url, init) => {
        const payload = JSON.parse(String(init.body));
        return response({ score: payload.submission.answer });
      }) });
      // A worker may use a bounded batch; tick enough times for all candidates.
      for (let i = 0; i < 3; i += 1) await worker.tick();
      store.createSubmission(benchmark.id, solution(999, 'Still waiting'));
      const entries = store.leaderboard(benchmark.id).entries;
      assert.deepEqual(entries.map((entry) => entry.score), direction === 'higher' ? [7, 7, 2] : [2, 7, 7]);
      assert.deepEqual(entries.map((entry) => entry.rank), direction === 'higher' ? [1, 1, 3] : [1, 2, 2]);
      assert.equal(entries.every((entry) => entry.status === 'verified'), true);
      assert.equal(store.getBenchmark(benchmark.id).verified_count, 3);
      assert.equal(store.getBenchmark(benchmark.id).submission_count, 4);
      assert.equal(store.getBenchmark(benchmark.id).best_score, direction === 'higher' ? 7 : 2);
    }
  } finally { resources.close(); }
});

for (const [label, result] of [
  ['missing score', {}], ['string score', { score: '0.8' }], ['out-of-range score', { score: 2 }],
  ['rejected evaluation', { status: 'rejected', score: 0.9, feedback: 'Invalid solution.' }],
] as const) {
  test(`${label} cannot enter the leaderboard`, async () => {
    const resources = new ResourceStore(':memory:');
    const store = new BenchmarkStore(resources);
    try {
      const benchmark = store.createBenchmark(definition({ score_min: 0, score_max: 1 }));
      const submission = store.createSubmission(benchmark.id, solution());
      const worker = new VerificationWorker(store, { request: mockRequest(() => response(result)) });
      await worker.tick();
      const saved = store.getSubmission(submission.id);
      assert.ok(['failed', 'rejected'].includes(saved.status));
      assert.equal(saved.score, null);
      assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
    } finally { resources.close(); }
  });
}

test('a lost dispatch response is recorded without automatically submitting the solution twice', async () => {
  let now = 10_000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition());
    const submission = store.createSubmission(benchmark.id, solution());
    let calls = 0;
    const worker = new VerificationWorker(store, { request: mockRequest(() => {
      calls += 1;
      throw new TypeError('Network connection lost after remote acceptance');
    }) });
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).status, 'failed');
    assert.equal(store.getSubmission(submission.id).score, null);
    now += 300_000;
    await worker.tick();
    assert.equal(calls, 1);
  } finally { resources.close(); }
});

test('nonfinite numbers, malformed reports, and HTTP failures never become verified scores', async () => {
  for (const invalidResponse of [
    () => new Response('{"score":1e999}', { status: 200 }),
    () => new Response('<html>upstream error</html>', { status: 200 }),
    () => response({ score: 0.8 }, 500),
  ]) {
    const resources = new ResourceStore(':memory:');
    const store = new BenchmarkStore(resources);
    try {
      const benchmark = store.createBenchmark(definition());
      const submission = store.createSubmission(benchmark.id, solution());
      const worker = new VerificationWorker(store, { request: mockRequest(invalidResponse) });
      await worker.tick();
      assert.equal(store.getSubmission(submission.id).status, 'failed');
      assert.equal(store.getSubmission(submission.id).score, null);
      assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
    } finally { resources.close(); }
  }
});

test('queued verification resumes after reopening the persistent database', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'dashboard-benchmarks-'));
  const path = join(directory, 'dashboard.sqlite');
  let resources = new ResourceStore(path);
  try {
    let store = new BenchmarkStore(resources);
    const benchmark = store.createBenchmark(definition());
    const submission = store.createSubmission(benchmark.id, solution());
    resources.close();
    resources = new ResourceStore(path);
    store = new BenchmarkStore(resources);
    assert.equal(store.getBenchmark(benchmark.id).title, benchmark.title);
    assert.deepEqual(store.getBenchmark(benchmark.id).submission_schema, benchmark.submission_schema);
    assert.equal(store.getSubmission(submission.id).status, 'queued');
    const worker = new VerificationWorker(store, { request: mockRequest(() => response({ score: 0.75 })) });
    await worker.tick();
    resources.close();
    resources = new ResourceStore(path);
    store = new BenchmarkStore(resources);
    assert.equal(store.getSubmission(submission.id).score, 0.75);
    assert.equal(store.leaderboard(benchmark.id).entries[0].id, submission.id);
  } finally {
    resources.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a remote queued job resumes polling after restart without dispatching it again', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'dashboard-remote-verification-'));
  const path = join(directory, 'dashboard.sqlite');
  let now = 1000;
  let resources = new ResourceStore(path, () => now);
  try {
    let store = new BenchmarkStore(resources);
    const benchmark = store.createBenchmark(definition({ verifier: {
      url: 'https://verifier.example/evaluate', mode: 'asynchronous',
      poll_url: 'https://verifier.example/jobs/{{job_id}}', poll_interval_seconds: 1,
    } }));
    const submission = store.createSubmission(benchmark.id, solution());
    let submissions = 0;
    let polls = 0;
    const request = mockRequest((_url, init) => {
      if (init.method === 'POST') {
        submissions += 1;
        return response({ id: 'remote-job-1', status: 'queued' }, 202);
      }
      polls += 1;
      return response({ status: 'completed', score: 0.9 });
    });
    await new VerificationWorker(store, { request }).tick();
    assert.equal(store.getSubmission(submission.id).external_job_id, 'remote-job-1');
    resources.close();
    now += 1001;
    resources = new ResourceStore(path, () => now);
    store = new BenchmarkStore(resources);
    await new VerificationWorker(store, { request }).tick();
    assert.equal(submissions, 1);
    assert.equal(polls, 1);
    assert.equal(store.getSubmission(submission.id).status, 'verified');
    assert.equal(store.getSubmission(submission.id).score, 0.9);
  } finally {
    resources.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('concurrent verifier workers cannot dispatch the same submission twice', async () => {
  const resources = new ResourceStore(':memory:');
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition());
    const submission = store.createSubmission(benchmark.id, solution());
    let calls = 0;
    const request = mockRequest(async () => {
      calls += 1;
      await Promise.resolve();
      return response({ score: 0.5 });
    });
    const first = new VerificationWorker(store, { request });
    const second = new VerificationWorker(store, { request });
    await Promise.all([first.tick(), second.tick(), first.tick()]);
    assert.equal(calls, 1);
    assert.equal(store.getSubmission(submission.id).status, 'verified');
  } finally { resources.close(); }
});

test('queued verification exceeding its time budget fails without another remote request', async () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition({ verifier: {
      url: 'https://verifier.example/evaluate', mode: 'asynchronous',
      poll_url: 'https://verifier.example/jobs/{{job_id}}', poll_interval_seconds: 1, timeout_seconds: 2,
    } }));
    const submission = store.createSubmission(benchmark.id, solution());
    let calls = 0;
    const worker = new VerificationWorker(store, { request: mockRequest(() => {
      calls += 1;
      return response({ id: 'remote-queued', status: 'queued' }, 202);
    }) });
    await worker.tick();
    now += 2001;
    await worker.tick();
    assert.equal(calls, 1);
    assert.equal(store.getSubmission(submission.id).status, 'failed');
    assert.match(store.getSubmission(submission.id).error ?? '', /time limit/i);
    assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
  } finally { resources.close(); }
});

test('a score arriving after the verification deadline is not published', async () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition({ verifier: {
      url: 'https://verifier.example/evaluate', timeout_seconds: 1,
    } }));
    const submission = store.createSubmission(benchmark.id, solution());
    const worker = new VerificationWorker(store, { request: mockRequest(() => {
      now += 1001;
      return response({ score: 1 });
    }) });
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).status, 'failed');
    assert.equal(store.getSubmission(submission.id).score, null);
    assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
  } finally { resources.close(); }
});

test('a crashed dispatcher is not replayed after its persistent lease expires', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'dashboard-dispatch-crash-'));
  const path = join(directory, 'dashboard.sqlite');
  let now = 1000;
  let resources = new ResourceStore(path, () => now);
  try {
    let store = new BenchmarkStore(resources);
    const benchmark = store.createBenchmark(definition());
    const submission = store.createSubmission(benchmark.id, solution());
    const claim = store.claimDue();
    assert.equal(claim?.submission.id, submission.id);
    // Simulate the process stopping after the request was claimed and potentially dispatched.
    resources.close();
    now += 60_001;
    resources = new ResourceStore(path, () => now);
    store = new BenchmarkStore(resources);
    let calls = 0;
    const worker = new VerificationWorker(store, { request: mockRequest(() => {
      calls += 1;
      return response({ score: 1 });
    }) });
    await worker.tick();
    assert.equal(calls, 0);
    assert.equal(store.getSubmission(submission.id).status, 'failed');
    assert.match(store.getSubmission(submission.id).error ?? '', /outcome is unknown/i);
    assert.equal(store.leaderboard(benchmark.id).entries.length, 0);
  } finally {
    resources.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('transient network failure while polling preserves the remote job and retries only GET', async () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition({ verifier: {
      url: 'https://verifier.example/evaluate', mode: 'asynchronous',
      poll_url: 'https://verifier.example/jobs/{{job_id}}', poll_interval_seconds: 1,
    } }));
    const submission = store.createSubmission(benchmark.id, solution());
    let submissions = 0;
    let polls = 0;
    const worker = new VerificationWorker(store, { request: mockRequest((_url, init) => {
      if (init.method === 'POST') {
        submissions += 1;
        return response({ id: 'durable-remote', status: 'queued' }, 202);
      }
      polls += 1;
      if (polls === 1) throw new TypeError('Temporary network outage');
      return response({ status: 'completed', score: 0.6 });
    }) });
    await worker.tick();
    now += 1001;
    await worker.tick();
    assert.equal(store.getSubmission(submission.id).external_job_id, 'durable-remote');
    assert.equal(store.getSubmission(submission.id).score, null);
    assert.ok(['running', 'queued'].includes(store.getSubmission(submission.id).status));
    now += 1001;
    await worker.tick();
    assert.equal(submissions, 1);
    assert.equal(polls, 2);
    assert.equal(store.getSubmission(submission.id).status, 'verified');
    assert.equal(store.getSubmission(submission.id).score, 0.6);
  } finally { resources.close(); }
});

test('a stale polling lease cannot publish or overwrite a newer verification result', () => {
  let now = 1000;
  const resources = new ResourceStore(':memory:', () => now);
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition({ verifier: {
      url: 'https://verifier.example/evaluate', mode: 'asynchronous',
      poll_url: 'https://verifier.example/jobs/{{job_id}}', poll_interval_seconds: 1,
    } }));
    const submission = store.createSubmission(benchmark.id, solution());
    const initial = store.claimDue();
    assert.ok(initial);
    store.pending(initial, 'remote-job', 'queued');
    now += 1001;
    const stale = store.claimDue();
    assert.ok(stale);
    now += 60_001;
    const current = store.claimDue();
    assert.ok(current);
    assert.notEqual(stale.token, current.token);
    store.finish(stale, 'verified', 0.99);
    assert.equal(store.getSubmission(submission.id).score, null);
    store.finish(current, 'verified', 0.4);
    store.finish(stale, 'verified', 1);
    assert.equal(store.getSubmission(submission.id).score, 0.4);
    assert.equal(store.leaderboard(benchmark.id).entries[0].score, 0.4);
  } finally { resources.close(); }
});

test('schema-invalid solutions never create verification jobs', () => {
  const resources = new ResourceStore(':memory:');
  const store = new BenchmarkStore(resources);
  try {
    const benchmark = store.createBenchmark(definition());
    assert.throws(() => store.createSubmission(benchmark.id, submissionSchema.parse({
      title: 'Invalid', payload: { answer: false },
    })), (error: unknown) => error instanceof StoreError && error.status === 422);
    assert.equal(store.listSubmissions(benchmark.id).total, 0);
    assert.equal(store.getBenchmark(benchmark.id).submission_count, 0);
  } finally { resources.close(); }
});
