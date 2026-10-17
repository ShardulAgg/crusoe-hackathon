import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { jobSchema, nodeSchema } from '../shared/models.ts';
import type { Capacity, CloudCapacity, Job, JobInput, NodeInput, Offering, ResourceNode } from '../shared/models.ts';
import { crusoeCatalog } from './catalog.ts';

export const LEASE_MS = 120_000;
export const CAPACITY_TTL_MS = 60_000;
const zero = (): Capacity => ({ cpus: 0, memory_mb: 0, gpus: 0 });

export class StoreError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type NodeRow = { id: string; definition: string; last_seen: number; created_at: number; updated_at: number };
type JobRow = Omit<Job, keyof JobInput | 'allocation_held'> & {
  definition: string; allocation_held: number; claim_token: string | null;
};

export class ResourceStore {
  db: DatabaseSync;
  now: () => number;

  constructor(path: string, now: () => number = Date.now) {
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });
    this.now = now;
    this.db = new DatabaseSync(path);
    this.db.exec(`
      PRAGMA busy_timeout = 5000;
      PRAGMA journal_mode = WAL;
      PRAGMA foreign_keys = ON;
      CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY, definition TEXT NOT NULL, last_seen INTEGER NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, definition TEXT NOT NULL,
        node_id TEXT REFERENCES nodes(id),
        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
        allocation_held INTEGER NOT NULL DEFAULT 0 CHECK(allocation_held IN (0,1)),
        claim_token TEXT, lease_until INTEGER, error TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS jobs_node_allocation ON jobs(node_id, allocation_held);
      CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
      CREATE TABLE IF NOT EXISTS offerings (id TEXT PRIMARY KEY, definition TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS cloud_capacities (
        product_name TEXT NOT NULL, location TEXT NOT NULL, quantity INTEGER NOT NULL,
        num_slices INTEGER, checked_at INTEGER NOT NULL,
        PRIMARY KEY(product_name, location)
      );
      CREATE TABLE IF NOT EXISTS provider_sync (
        provider TEXT PRIMARY KEY, last_success_at INTEGER, error TEXT
      );
      PRAGMA user_version = 1;
    `);
    this.transaction(() => {
      const insert = this.db.prepare('INSERT INTO offerings(id,definition) VALUES(?,?) ON CONFLICT(id) DO NOTHING');
      for (const offering of crusoeCatalog) insert.run(offering.id, JSON.stringify(offering));
    });
  }

  close() { this.db.close(); }

  transaction<T>(action: () => T): T {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = action();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }

  expire() {
    // An expired lease does not prove that the process has stopped. Keep its allocation.
    this.db.prepare(`UPDATE jobs SET status='failed', error='Worker heartbeat expired', updated_at=?
      WHERE status='running' AND lease_until<=?`).run(this.now(), this.now());
  }

  jobView(row: JobRow): Job {
    const { definition, claim_token: _token, allocation_held, ...record } = row;
    return { ...JSON.parse(definition), ...record, allocation_held: Boolean(allocation_held) };
  }

  getJob(id: string): Job {
    this.expire();
    const row = this.db.prepare('SELECT * FROM jobs WHERE id=?').get(id) as JobRow | undefined;
    if (!row) throw new StoreError(404, 'Job not found');
    return this.jobView(row);
  }

  listJobs(nodeId?: string, status?: string, limit = 100, offset = 0) {
    return this.transaction(() => {
      this.expire();
      const rows = this.db.prepare(`SELECT * FROM jobs
        WHERE (? IS NULL OR node_id=?) AND (? IS NULL OR status=?)
        ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?`)
        .all(nodeId ?? null, nodeId ?? null, status ?? null, status ?? null, limit, offset) as JobRow[];
      const count = this.db.prepare(`SELECT count(*) AS total FROM jobs
        WHERE (? IS NULL OR node_id=?) AND (? IS NULL OR status=?)`)
        .get(nodeId ?? null, nodeId ?? null, status ?? null, status ?? null) as { total: number };
      return { jobs: rows.map((row) => this.jobView(row)), total: count.total, limit, offset };
    });
  }

  nodeView(row: NodeRow): ResourceNode {
    const definition = nodeSchema.parse(JSON.parse(row.definition));
    const held = this.db.prepare('SELECT * FROM jobs WHERE node_id=? AND allocation_held=1 ORDER BY created_at')
      .all(row.id) as JobRow[];
    const jobs = held.map((job) => this.jobView(job));
    const reserved = jobs.reduce((sum, job) => ({
      cpus: (Math.round(sum.cpus * 1000) + Math.round(job.cpus * 1000)) / 1000,
      memory_mb: sum.memory_mb + job.memory_mb, gpus: sum.gpus + job.gpus,
    }), zero());
    const remaining = {
      cpus: Math.max(0, (Math.round(definition.cpus * 1000) - Math.round(reserved.cpus * 1000)) / 1000),
      memory_mb: Math.max(0, definition.memory_mb - reserved.memory_mb),
      gpus: Math.max(0, definition.gpus - reserved.gpus),
    };
    const slots = Math.max(0, definition.max_concurrent_jobs - held.length);
    const offline = row.last_seen <= this.now() - LEASE_MS;
    const cleanup = jobs.some((job) => job.status !== 'running');
    const status = offline ? 'offline' : cleanup ? 'cleanup_required'
      : slots === 0 || remaining.cpus <= 0 || remaining.memory_mb <= 0 ? 'busy' : 'available';
    return {
      ...definition, last_seen: row.last_seen, created_at: row.created_at, updated_at: row.updated_at,
      status, reserved, available: status === 'available' ? remaining : zero(),
      available_slots: status === 'available' ? slots : 0, jobs,
    };
  }

  getNode(id: string): ResourceNode {
    this.expire();
    const row = this.db.prepare('SELECT * FROM nodes WHERE id=?').get(id) as NodeRow | undefined;
    if (!row) throw new StoreError(404, 'Node not found');
    return this.nodeView(row);
  }

  listNodes(): ResourceNode[] {
    return this.transaction(() => {
      this.expire();
      return (this.db.prepare('SELECT * FROM nodes ORDER BY id').all() as NodeRow[])
        .map((row) => this.nodeView(row));
    });
  }

  registerNode(input: NodeInput): ResourceNode {
    const node = nodeSchema.parse(input);
    return this.transaction(() => {
      this.expire();
      if (this.db.prepare('SELECT id FROM jobs WHERE node_id=? AND allocation_held=1').get(node.id)) {
        throw new StoreError(409, 'Finish and confirm cleanup of assigned jobs before changing node specifications');
      }
      const now = this.now();
      this.db.prepare(`INSERT INTO nodes(id,definition,last_seen,created_at,updated_at) VALUES(?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET definition=excluded.definition, last_seen=excluded.last_seen,
        updated_at=excluded.updated_at`).run(node.id, JSON.stringify(node), now, now, now);
      return this.getNode(node.id);
    });
  }

  heartbeatNode(id: string): ResourceNode {
    return this.transaction(() => {
      if (!this.db.prepare('UPDATE nodes SET last_seen=? WHERE id=?').run(this.now(), id).changes) {
        throw new StoreError(404, 'Node not found');
      }
      return this.getNode(id);
    });
  }

  createJob(input: JobInput): Job {
    const job = jobSchema.parse(input);
    const id = randomUUID();
    const now = this.now();
    this.db.prepare(`INSERT INTO jobs(id,definition,status,created_at,updated_at) VALUES(?,?,'queued',?,?)`)
      .run(id, JSON.stringify(job), now, now);
    return this.getJob(id);
  }

  startJob(id: string, nodeId: string): { job: Job; lease_token: string } {
    return this.transaction(() => {
      const job = this.getJob(id);
      if (job.status !== 'queued') throw new StoreError(409, 'Only queued jobs can start');
      const node = this.getNode(nodeId);
      if (node.status !== 'available' || job.cpus > node.available.cpus
        || job.memory_mb > node.available.memory_mb || job.gpus > node.available.gpus) {
        throw new StoreError(409, 'Node does not have enough available capacity');
      }
      const token = randomUUID();
      this.db.prepare(`UPDATE jobs SET node_id=?,status='running',allocation_held=1,claim_token=?,
        lease_until=?,updated_at=? WHERE id=?`)
        .run(nodeId, token, this.now() + LEASE_MS, this.now(), id);
      return { job: this.getJob(id), lease_token: token };
    });
  }

  heartbeatJob(id: string, token: string) {
    return this.transaction(() => {
      this.expire();
      const row = this.db.prepare(`SELECT node_id FROM jobs WHERE id=? AND claim_token=? AND status='running'`)
        .get(id, token) as { node_id: string } | undefined;
      if (!row) throw new StoreError(409, 'Job lease is no longer active');
      this.db.prepare('UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?')
        .run(this.now() + LEASE_MS, this.now(), id);
      this.db.prepare('UPDATE nodes SET last_seen=? WHERE id=?').run(this.now(), row.node_id);
      return this.getJob(id);
    });
  }

  finishJob(id: string, token: string, status: 'completed' | 'failed', error?: string): Job {
    return this.transaction(() => {
      this.expire();
      const row = this.db.prepare('SELECT * FROM jobs WHERE id=? AND claim_token=? AND allocation_held=1')
        .get(id, token) as JobRow | undefined;
      if (!row) throw new StoreError(409, 'Job allocation or cleanup token is no longer active');
      this.db.prepare(`UPDATE jobs SET status=?,error=?,allocation_held=0,claim_token=NULL,
        lease_until=NULL,updated_at=? WHERE id=?`)
        .run(row.status === 'running' ? status : row.status,
          row.status === 'running' ? (error ?? null) : row.error, this.now(), id);
      // A completion confirms this job stopped, not that the node is still healthy.
      return this.getJob(id);
    });
  }

  cancelJob(id: string): Job {
    return this.transaction(() => {
      const job = this.getJob(id);
      if (!['queued', 'running'].includes(job.status)) throw new StoreError(409, 'Job is already finished');
      this.db.prepare(`UPDATE jobs SET status='cancelled',lease_until=NULL,updated_at=? WHERE id=?`)
        .run(this.now(), id);
      return this.getJob(id);
    });
  }

  listOfferings(): Offering[] {
    return (this.db.prepare('SELECT definition FROM offerings ORDER BY id').all() as { definition: string }[])
      .map((row) => JSON.parse(row.definition));
  }

  saveCloudCapacity(capacities: Omit<CloudCapacity, 'checked_at'>[]) {
    this.transaction(() => {
      const now = this.now();
      this.db.exec('DELETE FROM cloud_capacities');
      const insert = this.db.prepare(`INSERT INTO cloud_capacities
        (product_name,location,quantity,num_slices,checked_at) VALUES(?,?,?,?,?)`);
      for (const item of capacities) insert.run(item.product_name, item.location, item.quantity, item.num_slices, now);
      this.db.prepare(`INSERT INTO provider_sync(provider,last_success_at,error) VALUES('crusoe',?,NULL)
        ON CONFLICT(provider) DO UPDATE SET last_success_at=excluded.last_success_at,error=NULL`).run(now);
    });
  }

  saveSyncError(error: string) {
    this.db.prepare(`INSERT INTO provider_sync(provider,error) VALUES('crusoe',?)
      ON CONFLICT(provider) DO UPDATE SET error=excluded.error`).run(error);
  }

  cloudInventory(configured: boolean) {
    const sync = this.db.prepare("SELECT last_success_at,error FROM provider_sync WHERE provider='crusoe'")
      .get() as { last_success_at: number | null; error: string | null } | undefined;
    return {
      configured,
      status: !configured ? 'not_configured' : sync?.error ? 'error' : sync?.last_success_at != null ? 'synced' : 'never_synced',
      last_success_at: sync?.last_success_at ?? null,
      error: sync?.error ?? null,
      stale: !configured || Boolean(sync?.error) || sync?.last_success_at == null
        || this.now() - sync.last_success_at >= CAPACITY_TTL_MS,
      offerings: this.listOfferings(),
      capacities: this.db.prepare('SELECT * FROM cloud_capacities ORDER BY product_name,location').all() as CloudCapacity[],
    };
  }
}
