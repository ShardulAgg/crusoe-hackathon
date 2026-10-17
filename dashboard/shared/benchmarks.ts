import { z } from 'zod';

const endpoint = z.string().trim().min(1).max(2000).refine((value) => {
  try {
    const url = new URL(value.replaceAll('{{job_id}}', 'job-id'));
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.hash;
  } catch { return false; }
}, 'Use an HTTP or HTTPS endpoint without embedded credentials or a fragment');
const fieldPath = z.string().trim().max(200);

export const verifierSchema = z.object({
  url: endpoint,
  method: z.enum(['POST', 'PUT']).default('POST'),
  mode: z.enum(['synchronous', 'asynchronous']).default('synchronous'),
  request_template: z.json().default({
    submission: '{{submission.payload}}', submission_id: '{{submission.id}}', benchmark_id: '{{benchmark.id}}',
  }),
  score_path: fieldPath.default('score'),
  feedback_path: fieldPath.default('feedback'),
  job_id_path: fieldPath.default('id'),
  status_path: fieldPath.default('status'),
  success_statuses: z.array(z.string().trim().min(1).max(100)).min(1).max(20)
    .default(['completed', 'succeeded', 'verified']),
  failure_statuses: z.array(z.string().trim().min(1).max(100)).min(1).max(20)
    .default(['failed', 'cancelled', 'rejected', 'error']),
  poll_url: z.union([endpoint, z.literal('')]).default(''),
  poll_interval_seconds: z.number().int().min(1).max(3600).default(5),
  timeout_seconds: z.number().int().min(1).max(86400).default(3600),
  auth_env: z.union([z.string().regex(/^BENCHMARK_VERIFIER_[A-Z0-9_]+$/), z.literal('')]).default(''),
}).strict().superRefine((value, context) => {
  if (value.url.includes('{{')) {
    context.addIssue({ code: 'custom', path: ['url'], message: 'Job placeholders are only supported in the polling endpoint' });
  }
  if (value.mode === 'asynchronous' && !value.poll_url) {
    context.addIssue({ code: 'custom', path: ['poll_url'], message: 'Queued verification requires a polling endpoint' });
  }
  if (value.success_statuses.some((status) => value.failure_statuses.includes(status))) {
    context.addIssue({ code: 'custom', path: ['failure_statuses'], message: 'Success and failure statuses must be different' });
  }
  try {
    const poll = value.poll_url ? new URL(value.poll_url.replaceAll('{{job_id}}', 'benchmark-job-reference')) : null;
    if (poll?.host.includes('benchmark-job-reference')) {
      context.addIssue({ code: 'custom', path: ['poll_url'], message: 'Job placeholders are only allowed in the polling path or query' });
    }
    if (value.auth_env && value.mode === 'asynchronous' && poll && new URL(value.url).origin !== poll.origin) {
      context.addIssue({ code: 'custom', path: ['poll_url'], message: 'Authenticated polling must use the verification endpoint origin' });
    }
  } catch { /* Endpoint refinements report malformed URLs without throwing. */ }
});

export const benchmarkSchema = z.object({
  title: z.string().trim().min(1).max(200),
  prompt: z.string().trim().min(1).max(30000),
  submission_instructions: z.string().trim().min(1).max(10000).default('Submit your solution as JSON.'),
  submission_schema: z.record(z.string(), z.json()),
  score_name: z.string().trim().min(1).max(100).default('Score'),
  score_direction: z.enum(['higher', 'lower']).default('higher'),
  score_min: z.number().finite().nullable().default(null),
  score_max: z.number().finite().nullable().default(null),
  verifier: verifierSchema,
}).strict().superRefine((value, context) => {
  if (value.score_min !== null && value.score_max !== null && value.score_min >= value.score_max) {
    context.addIssue({ code: 'custom', path: ['score_max'], message: 'Maximum score must be greater than minimum score' });
  }
});

export const submissionSchema = z.object({
  title: z.string().trim().min(1).max(200),
  submitted_by: z.string().trim().min(1).max(200).default('Anonymous'),
  payload: z.json(),
}).strict();

export type VerifierConfig = z.infer<typeof verifierSchema>;
export type BenchmarkInput = z.infer<typeof benchmarkSchema>;
export type Benchmark = BenchmarkInput & {
  id: string; version: number; created_at: number;
  submission_count: number; verified_count: number; best_score: number | null;
};
export type SubmissionInput = z.infer<typeof submissionSchema>;
export type SubmissionStatus = 'queued' | 'running' | 'verified' | 'failed' | 'rejected';
export type Submission = SubmissionInput & {
  id: string; benchmark_id: string; status: SubmissionStatus;
  score: number | null; feedback: string | null; error: string | null;
  external_job_id: string | null; created_at: number; updated_at: number; completed_at: number | null;
};
export type LeaderboardEntry = Submission & { rank: number };
