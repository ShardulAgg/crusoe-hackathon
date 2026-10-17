import { randomUUID } from 'node:crypto';
import { benchmarkSchema, submissionSchema } from '../shared/benchmarks.ts';
import type { Benchmark, BenchmarkInput, LeaderboardEntry, Submission, SubmissionInput, SubmissionStatus } from '../shared/benchmarks.ts';
import { ResourceStore, StoreError } from './store.ts';
import { validateSubmissionPayload, validateSubmissionSchema } from './submission-schema.ts';
import { validateRequestTemplate } from './verifier-adapter.ts';

type BenchmarkRow = { id: string; definition: string; version: number; created_at: number };
type SubmissionRow = Omit<Submission, keyof SubmissionInput> & {
  definition: string; phase: string; next_check_at: number; deadline_at: number;
  lease_token: string | null; lease_until: number | null;
};
export type VerificationClaim = {
  submission: Submission; benchmark: Benchmark; token: string;
  phase: 'submit' | 'poll'; deadline_at: number;
};

export class BenchmarkStore {
  resources: ResourceStore;

  constructor(resources: ResourceStore) {
    this.resources = resources;
    resources.db.exec(`
      CREATE TABLE IF NOT EXISTS benchmarks (
        id TEXT PRIMARY KEY, definition TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS benchmark_submissions (
        id TEXT PRIMARY KEY, benchmark_id TEXT NOT NULL REFERENCES benchmarks(id), definition TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','running','verified','failed','rejected')),
        score REAL, feedback TEXT, error TEXT, external_job_id TEXT,
        phase TEXT NOT NULL CHECK(phase IN ('submit','dispatching','poll','done')),
        next_check_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL,
        lease_token TEXT, lease_until INTEGER,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, completed_at INTEGER,
        CHECK((status='verified' AND score IS NOT NULL) OR (status!='verified' AND score IS NULL))
      );
      CREATE INDEX IF NOT EXISTS benchmark_submission_history ON benchmark_submissions(benchmark_id,created_at);
      CREATE INDEX IF NOT EXISTS benchmark_submission_queue ON benchmark_submissions(phase,next_check_at);
    `);
  }

  benchmarkView(row: BenchmarkRow): Benchmark {
    const definition = JSON.parse(row.definition) as BenchmarkInput;
    const counts = this.resources.db.prepare(`SELECT count(*) AS submission_count,
      count(CASE WHEN status='verified' THEN 1 END) AS verified_count,
      ${definition.score_direction === 'lower' ? 'min' : 'max'}(CASE WHEN status='verified' THEN score END) AS best_score
      FROM benchmark_submissions WHERE benchmark_id=?`).get(row.id) as {
        submission_count: number; verified_count: number; best_score: number | null;
      };
    return { ...definition, id: row.id, version: row.version, created_at: row.created_at, ...counts };
  }

  createBenchmark(input: BenchmarkInput | unknown): Benchmark {
    const definition = benchmarkSchema.parse(input);
    validateSubmissionSchema(definition.submission_schema);
    validateRequestTemplate(definition.verifier.request_template);
    const id = randomUUID();
    this.resources.db.prepare('INSERT INTO benchmarks(id,definition,created_at) VALUES(?,?,?)')
      .run(id, JSON.stringify(definition), this.resources.now());
    return this.getBenchmark(id);
  }

  getBenchmark(id: string): Benchmark {
    const row = this.resources.db.prepare('SELECT * FROM benchmarks WHERE id=?').get(id) as BenchmarkRow | undefined;
    if (!row) throw new StoreError(404, 'Benchmark not found');
    return this.benchmarkView(row);
  }

  listBenchmarks(): Benchmark[] {
    return (this.resources.db.prepare('SELECT * FROM benchmarks ORDER BY created_at DESC,rowid DESC').all() as BenchmarkRow[])
      .map((row) => this.benchmarkView(row));
  }

  submissionView(row: SubmissionRow): Submission {
    return {
      ...JSON.parse(row.definition), id: row.id, benchmark_id: row.benchmark_id, status: row.status,
      score: row.score, feedback: row.feedback, error: row.error, external_job_id: row.external_job_id,
      created_at: row.created_at, updated_at: row.updated_at, completed_at: row.completed_at,
    };
  }

  createSubmission(benchmarkId: string, input: SubmissionInput | unknown): Submission {
    const benchmark = this.getBenchmark(benchmarkId);
    const definition = submissionSchema.parse(input);
    validateSubmissionPayload(benchmark.submission_schema, definition.payload);
    const id = randomUUID();
    const now = this.resources.now();
    this.resources.db.prepare(`INSERT INTO benchmark_submissions
      (id,benchmark_id,definition,status,phase,next_check_at,deadline_at,created_at,updated_at)
      VALUES(?,?,?,'queued','submit',?,?,?,?)`)
      .run(id, benchmarkId, JSON.stringify(definition), now, now + benchmark.verifier.timeout_seconds * 1000, now, now);
    return this.getSubmission(id);
  }

  getSubmission(id: string): Submission {
    const row = this.resources.db.prepare('SELECT * FROM benchmark_submissions WHERE id=?').get(id) as SubmissionRow | undefined;
    if (!row) throw new StoreError(404, 'Submission not found');
    return this.submissionView(row);
  }

  listSubmissions(benchmarkId: string, limit = 100, offset = 0) {
    this.getBenchmark(benchmarkId);
    const rows = this.resources.db.prepare(`SELECT * FROM benchmark_submissions WHERE benchmark_id=?
      ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?`).all(benchmarkId, limit, offset) as SubmissionRow[];
    const { total } = this.resources.db.prepare('SELECT count(*) AS total FROM benchmark_submissions WHERE benchmark_id=?')
      .get(benchmarkId) as { total: number };
    return { submissions: rows.map((row) => this.submissionView(row)), total, limit, offset };
  }

  leaderboard(benchmarkId: string) {
    const benchmark = this.getBenchmark(benchmarkId);
    const rows = this.resources.db.prepare(`SELECT * FROM benchmark_submissions
      WHERE benchmark_id=? AND status='verified'
      ORDER BY score ${benchmark.score_direction === 'lower' ? 'ASC' : 'DESC'},created_at ASC,id ASC LIMIT 1000`)
      .all(benchmarkId) as SubmissionRow[];
    let rank = 0;
    const entries: LeaderboardEntry[] = rows.map((row, index) => {
      if (index === 0 || row.score !== rows[index - 1].score) rank = index + 1;
      return { ...this.submissionView(row), rank };
    });
    const { total } = this.resources.db.prepare(`SELECT count(*) AS total FROM benchmark_submissions
      WHERE benchmark_id=? AND status='verified'`).get(benchmarkId) as { total: number };
    return { entries, total };
  }

  claimDue(): VerificationClaim | null {
    return this.resources.transaction(() => {
      const now = this.resources.now();
      // A lost POST acknowledgement may have queued work remotely. Never replay that POST automatically.
      this.resources.db.prepare(`UPDATE benchmark_submissions SET status='failed',phase='done',
        error='Verification dispatch outcome is unknown. Check the verifier before submitting again.',
        lease_token=NULL,lease_until=NULL,completed_at=?,updated_at=?
        WHERE phase='dispatching' AND lease_until<=?`).run(now, now, now);
      this.resources.db.prepare(`UPDATE benchmark_submissions SET status='failed',phase='done',
        error='Verification time limit exceeded',lease_token=NULL,lease_until=NULL,completed_at=?,updated_at=?
        WHERE phase!='done' AND deadline_at<=?`).run(now, now, now);
      const row = this.resources.db.prepare(`SELECT * FROM benchmark_submissions
        WHERE phase IN ('submit','poll') AND next_check_at<=? AND (lease_until IS NULL OR lease_until<=?)
        ORDER BY next_check_at,created_at,id LIMIT 1`).get(now, now) as SubmissionRow | undefined;
      if (!row) return null;
      const token = randomUUID();
      this.resources.db.prepare(`UPDATE benchmark_submissions SET lease_token=?,lease_until=?,
        phase=?,status='running',updated_at=? WHERE id=?`)
        .run(token, now + 60000, row.phase === 'submit' ? 'dispatching' : 'poll', now, row.id);
      return { submission: this.getSubmission(row.id), benchmark: this.getBenchmark(row.benchmark_id),
        token, phase: row.phase as 'submit' | 'poll', deadline_at: row.deadline_at };
    });
  }

  pending(claim: VerificationClaim, externalJobId: string, status: 'queued' | 'running', error: string | null = null) {
    const now = this.resources.now();
    this.resources.db.prepare(`UPDATE benchmark_submissions SET phase='poll',status=?,external_job_id=?,
      error=?,next_check_at=?,lease_token=NULL,lease_until=NULL,updated_at=?
      WHERE id=? AND lease_token=? AND phase!='done'`)
      .run(status, externalJobId, error, now + claim.benchmark.verifier.poll_interval_seconds * 1000,
        now, claim.submission.id, claim.token);
  }

  finish(claim: VerificationClaim, status: Extract<SubmissionStatus, 'verified' | 'failed' | 'rejected'>,
    score: number | null = null, feedback: string | null = null, error: string | null = null) {
    const now = this.resources.now();
    if (status === 'verified' && (score === null || !Number.isFinite(score))) {
      throw new StoreError(400, 'A verified submission requires a finite score');
    }
    if (now >= claim.deadline_at) { status = 'failed'; score = null; error = 'Verification time limit exceeded'; }
    this.resources.db.prepare(`UPDATE benchmark_submissions SET status=?,score=?,feedback=?,error=?,phase='done',
      lease_token=NULL,lease_until=NULL,completed_at=?,updated_at=? WHERE id=? AND lease_token=? AND phase!='done'`)
      .run(status, status === 'verified' ? score : null, feedback, error, now, now, claim.submission.id, claim.token);
  }
}
