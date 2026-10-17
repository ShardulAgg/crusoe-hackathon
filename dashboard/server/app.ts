import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import express from 'express';
import type { ErrorRequestHandler } from 'express';
import { z } from 'zod';
import { finishSchema, jobSchema, leaseSchema, nodeSchema } from '../shared/models.ts';
import { CrusoeError, crusoeConfigured, fetchCrusoeCapacity } from './crusoe.ts';
import { ResourceStore, StoreError } from './store.ts';
import { BenchmarkStore } from './benchmark-store.ts';
import { benchmarkDefinitionSchema, solutionSubmissionSchema } from '../shared/api-schemas.ts';

const querySchema = z.object({
  node_id: z.string().max(100).optional(),
  status: z.enum(['queued', 'running', 'completed', 'failed', 'cancelled']).optional(),
  limit: z.coerce.number().int().min(1).max(500).default(100),
  offset: z.coerce.number().int().min(0).max(2 ** 31).default(0),
}).strict();

export function createApp(store: ResourceStore, benchmarks = new BenchmarkStore(store)) {
  const app = express();
  app.disable('x-powered-by');
  app.use(express.json({ limit: '256kb' }));
  app.use('/api', (req, res, next) => {
    res.set('Cache-Control', 'no-store');
    if (!['127.0.0.1', 'localhost'].includes(req.hostname)) {
      return res.status(403).json({ error: 'Host is not allowed' });
    }
    const origin = req.get('origin');
    // The local dashboard never accepts browser requests from unrelated sites.
    if (origin && !['http://127.0.0.1:5173', 'http://localhost:5173',
      `http://127.0.0.1:${process.env.DASHBOARD_API_PORT || 3001}`,
      `http://localhost:${process.env.DASHBOARD_API_PORT || 3001}`].includes(origin)) {
      return res.status(403).json({ error: 'Origin is not allowed' });
    }
    const key = process.env.DASHBOARD_API_KEY;
    if (key && req.path !== '/health') {
      const provided = Buffer.from((req.get('authorization') ?? '').replace(/^Bearer /, ''));
      const expected = Buffer.from(key);
      if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
        return res.status(401).json({ error: 'Dashboard API key required' });
      }
    }
    next();
  });

  app.get('/api/health', (_req, res) => res.json({ status: 'ok', database: 'sqlite' }));
  app.get('/api/schemas/benchmark', (_req, res) => res.json(benchmarkDefinitionSchema));
  app.get('/api/schemas/submission', (_req, res) => res.json(solutionSubmissionSchema));
  app.get('/api/benchmarks', (_req, res) => res.json({ benchmarks: benchmarks.listBenchmarks() }));
  app.post('/api/benchmarks', (req, res) => {
    const benchmark = benchmarks.createBenchmark(req.body);
    res.location(`/api/benchmarks/${benchmark.id}`).status(201).json(benchmark);
  });
  app.get('/api/benchmarks/:id', (req, res) => res.json(benchmarks.getBenchmark(req.params.id)));
  app.get('/api/benchmarks/:id/submissions', (req, res) => {
    const query = z.object({
      limit: z.coerce.number().int().min(1).max(500).default(100),
      offset: z.coerce.number().int().min(0).max(2 ** 31).default(0),
    }).strict().parse(req.query);
    res.json(benchmarks.listSubmissions(req.params.id, query.limit, query.offset));
  });
  app.post('/api/benchmarks/:id/submissions', (req, res) => {
    const submission = benchmarks.createSubmission(req.params.id, req.body);
    res.location(`/api/submissions/${submission.id}`).status(202).json(submission);
  });
  app.get('/api/benchmarks/:id/leaderboard', (req, res) => res.json(benchmarks.leaderboard(req.params.id)));
  app.get('/api/submissions/:id', (req, res) => res.json(benchmarks.getSubmission(req.params.id)));
  app.get('/api/nodes', (_req, res) => res.json({ nodes: store.listNodes() }));
  app.post('/api/nodes/register', (req, res) => res.status(201).json(store.registerNode(nodeSchema.parse(req.body))));
  app.get('/api/nodes/:id', (req, res) => res.json(store.getNode(req.params.id)));
  app.post('/api/nodes/:id/heartbeat', (req, res) => res.json(store.heartbeatNode(req.params.id)));
  app.get('/api/nodes/:id/jobs', (req, res) => {
    store.getNode(req.params.id);
    const query = querySchema.omit({ node_id: true }).parse(req.query);
    res.json(store.listJobs(req.params.id, query.status, query.limit, query.offset));
  });
  app.get('/api/resources/available', (req, res) => {
    const query = z.object({
      cpus: z.coerce.number().finite().nonnegative().default(0),
      memory_mb: z.coerce.number().int().nonnegative().default(0),
      gpus: z.coerce.number().int().nonnegative().default(0),
      gpu_memory_mb: z.coerce.number().int().nonnegative().default(0),
      gpu_model: z.string().max(200).optional(),
      provider: z.string().max(100).optional(),
      region: z.string().max(100).optional(),
      allocation_mode: z.enum(['always_on', 'on_demand']).optional(),
    }).strict().parse(req.query);
    const nodes = store.listNodes().filter((node) => node.status === 'available'
      && node.available.cpus >= query.cpus && node.available.memory_mb >= query.memory_mb
      && node.available.gpus >= query.gpus
      && node.gpu_memory_mb >= query.gpu_memory_mb
      && (!query.gpu_model || node.gpu_model.toLowerCase() === query.gpu_model.toLowerCase())
      && (!query.provider || node.provider === query.provider)
      && (!query.region || node.region === query.region)
      && (!query.allocation_mode || node.allocation_mode === query.allocation_mode));
    res.json({ nodes });
  });
  app.get('/api/jobs', (req, res) => {
    const query = querySchema.parse(req.query);
    res.json(store.listJobs(query.node_id, query.status, query.limit, query.offset));
  });
  app.post('/api/jobs', (req, res) => res.status(201).json(store.createJob(jobSchema.parse(req.body))));
  app.get('/api/jobs/:id', (req, res) => res.json(store.getJob(req.params.id)));
  app.post('/api/jobs/:id/start', (req, res) => {
    const body = z.object({ node_id: nodeSchema.shape.id }).strict().parse(req.body);
    res.json(store.startJob(req.params.id, body.node_id));
  });
  app.post('/api/jobs/:id/heartbeat', (req, res) => {
    res.json(store.heartbeatJob(req.params.id, leaseSchema.parse(req.body).lease_token));
  });
  app.post('/api/jobs/:id/finish', (req, res) => {
    const body = finishSchema.parse(req.body);
    res.json(store.finishJob(req.params.id, body.lease_token, body.status, body.error));
  });
  app.post('/api/jobs/:id/cancel', (req, res) => res.json(store.cancelJob(req.params.id)));

  app.get('/api/providers/crusoe', (_req, res) => res.json(store.cloudInventory(crusoeConfigured())));
  let syncInFlight: Promise<void> | null = null;
  app.post('/api/providers/crusoe/sync', async (_req, res) => {
    if (!crusoeConfigured()) throw new StoreError(503, 'Connect Crusoe Cloud API credentials to check live capacity');
    // Coalesce concurrent requests; provider reads do not provision or reserve machines.
    syncInFlight ??= fetchCrusoeCapacity().then((items) => { store.saveCloudCapacity(items); })
      .catch((error: unknown) => {
        const message = error instanceof CrusoeError ? error.message : 'Crusoe capacity sync failed';
        store.saveSyncError(message);
        throw new StoreError(502, message);
      }).finally(() => { syncInFlight = null; });
    await syncInFlight;
    res.json(store.cloudInventory(true));
  });

  app.use('/api', (_req, res) => res.status(404).json({ error: 'API route not found' }));
  app.use(express.static(fileURLToPath(new URL('../dist/', import.meta.url))));
  const onError: ErrorRequestHandler = (error: unknown, _req, res, _next) => {
    if (error instanceof z.ZodError) {
      res.status(400).json({ error: 'Invalid request', details: error.issues.map(({ path, message }) => ({ path, message })) });
    } else if (error instanceof StoreError) {
      res.status(error.status).json({ error: error.message });
    } else if (error instanceof SyntaxError && 'body' in error) {
      res.status(400).json({ error: 'Invalid JSON body' });
    } else {
      console.error('Dashboard request failed:', error instanceof Error ? error.name : 'Unknown error');
      res.status(500).json({ error: 'Unable to complete request' });
    }
  };
  app.use(onError);
  return app;
}
