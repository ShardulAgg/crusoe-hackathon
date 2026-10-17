import type { BenchmarkStore, VerificationClaim } from './benchmark-store.ts';
import { readPath, renderTemplate } from './verifier-adapter.ts';

const RESPONSE_LIMIT = 1024 * 1024;
class VerificationError extends Error {}

async function readResponse(response: Response) {
  if (!response.ok) {
    await response.body?.cancel();
    throw new VerificationError(`Verification endpoint returned HTTP ${response.status}`);
  }
  if (!response.body) throw new VerificationError('Verification endpoint returned an empty response');
  const reader = response.body.getReader();
  const parts: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.byteLength;
      if (bytes > RESPONSE_LIMIT) throw new VerificationError('Verification response exceeds 1 MiB');
      parts.push(part.value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  try { return JSON.parse(Buffer.concat(parts).toString('utf8')) as unknown; }
  catch { throw new VerificationError('Verification endpoint must return valid JSON'); }
}

function message(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  return (typeof value === 'string' ? value : JSON.stringify(value)).slice(0, 4000);
}

export class VerificationWorker {
  store: BenchmarkStore;
  request: typeof fetch;
  timer: ReturnType<typeof setInterval> | null = null;
  active: Promise<void> | null = null;
  controller = new AbortController();

  constructor(store: BenchmarkStore, options: { request?: typeof fetch } = {}) {
    this.store = store;
    this.request = options.request ?? fetch;
  }

  start() {
    if (this.timer) return;
    this.controller = new AbortController();
    this.timer = setInterval(() => { void this.tick(); }, 1000);
    this.timer.unref();
    void this.tick();
  }

  async stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.controller.abort();
    await this.active;
  }

  async tick() {
    if (this.active) return this.active;
    const run = async () => {
      const claims: VerificationClaim[] = [];
      for (let index = 0; index < 4; index++) {
        const claim = this.store.claimDue();
        if (!claim) break;
        claims.push(claim);
      }
      await Promise.all(claims.map((claim) => this.evaluate(claim)));
    };
    this.active = run().finally(() => { this.active = null; });
    return this.active;
  }

  async evaluate(claim: VerificationClaim) {
    const { benchmark, submission } = claim;
    const verifier = benchmark.verifier;
    try {
      const headers: Record<string, string> = { Accept: 'application/json' };
      if (verifier.auth_env) {
        const credential = process.env[verifier.auth_env];
        if (!credential) throw new VerificationError(`Verification credential ${verifier.auth_env} is not configured`);
        headers.Authorization = `Bearer ${credential}`;
      }
      let url = verifier.url;
      const options: RequestInit = {
        method: claim.phase === 'submit' ? verifier.method : 'GET', headers, redirect: 'error',
        signal: AbortSignal.any([this.controller.signal,
          AbortSignal.timeout(Math.max(1, Math.min(30000, claim.deadline_at - this.store.resources.now())))]),
      };
      if (claim.phase === 'submit') {
        headers['Content-Type'] = 'application/json';
        // Stable even if a verifier implements its own request deduplication.
        headers['Idempotency-Key'] = submission.id;
        options.body = JSON.stringify(renderTemplate(verifier.request_template, benchmark, submission));
      } else {
        if (!submission.external_job_id) throw new VerificationError('Verification job reference is missing');
        url = verifier.poll_url.replaceAll('{{job_id}}', encodeURIComponent(submission.external_job_id));
      }
      if (verifier.auth_env && new URL(url).origin !== new URL(verifier.url).origin) {
        throw new VerificationError('Authenticated polling endpoint origin changed');
      }
      const response = await this.request(url, options);
      const result = await readResponse(response);
      const rawStatus = readPath(result, verifier.status_path);
      const status = ['string', 'number', 'boolean'].includes(typeof rawStatus) ? String(rawStatus) : '';
      const feedback = message(readPath(result, verifier.feedback_path));
      if (verifier.failure_statuses.includes(status)) {
        this.store.finish(claim, status === 'rejected' ? 'rejected' : 'failed', null, feedback,
          feedback || `Verifier reported ${status}`);
        return;
      }
      if (verifier.mode === 'synchronous' || verifier.success_statuses.includes(status)) {
        if (verifier.mode === 'synchronous' && (response.status === 202
          || (status && !verifier.success_statuses.includes(status)))) {
          throw new VerificationError('Verifier returned an unfinished evaluation. Configure queued verification or completed statuses.');
        }
        const score = readPath(result, verifier.score_path);
        if (typeof score !== 'number' || !Number.isFinite(score)) {
          throw new VerificationError('Verifier did not return a finite numeric score at the configured score path');
        }
        if ((benchmark.score_min !== null && score < benchmark.score_min)
          || (benchmark.score_max !== null && score > benchmark.score_max)) {
          throw new VerificationError('Verified score is outside the benchmark score range');
        }
        this.store.finish(claim, 'verified', score, feedback);
        return;
      }
      const external = claim.phase === 'poll' ? submission.external_job_id : readPath(result, verifier.job_id_path);
      if (!['string', 'number'].includes(typeof external) || external === null || String(external).length === 0
        || String(external).length > 500 || (typeof external === 'number' && !Number.isFinite(external))) {
        throw new VerificationError('Queued verifier did not return a valid job reference at the configured job ID path');
      }
      if (claim.phase === 'poll' && !status) throw new VerificationError('Queued verifier did not return a job status');
      this.store.pending(claim, String(external), ['queued', 'pending'].includes(status) ? 'queued' : 'running');
    } catch (error) {
      const detail = error instanceof VerificationError ? error.message : 'Could not reach or read the verification endpoint';
      if (claim.phase === 'poll' && !(error instanceof VerificationError)) {
        this.store.pending(claim, submission.external_job_id!, 'running', detail);
      } else {
        this.store.finish(claim, 'failed', null, null,
          claim.phase === 'submit' && !(error instanceof VerificationError)
            ? `${detail}. Dispatch outcome is unknown; check the verifier before submitting again.` : detail);
      }
    }
  }
}
