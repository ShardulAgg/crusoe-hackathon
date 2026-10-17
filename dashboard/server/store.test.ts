import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { jobSchema, nodeSchema } from '../shared/models.ts';
import { CAPACITY_TTL_MS, LEASE_MS, ResourceStore, StoreError } from './store.ts';

const node = (changes = {}) => nodeSchema.parse({
  id: 'gpu-01', name: 'GPU server', cpus: 16, memory_mb: 65536, gpus: 2,
  max_concurrent_jobs: 4, ...changes,
});
const job = (changes = {}) => jobSchema.parse({ title: 'Verification', cpus: 4, memory_mb: 8192, gpus: 1, ...changes });

test('nodes, specifications, assignments and job history persist across restart', () => {
  const directory = mkdtempSync(join(tmpdir(), 'dashboard-store-'));
  const path = join(directory, 'db.sqlite');
  let store = new ResourceStore(path);
  try {
    store.registerNode(node({ specifications: { os: 'Linux', cuda: '12.8' } }));
    const queued = store.createJob(job());
    const claim = store.startJob(queued.id, 'gpu-01');
    store.close();
    store = new ResourceStore(path);
    const restored = store.getNode('gpu-01');
    assert.equal(restored.specifications.os, 'Linux');
    assert.deepEqual(restored.available, { cpus: 12, memory_mb: 57344, gpus: 1 });
    assert.equal(restored.jobs[0].id, queued.id);
    assert.ok(!JSON.stringify(restored).includes(claim.lease_token));
    store.finishJob(queued.id, claim.lease_token, 'completed');
    assert.equal(store.getNode('gpu-01').jobs.length, 0);
    assert.equal(store.listJobs('gpu-01').jobs[0].status, 'completed');
  } finally { store.close(); rmSync(directory, { recursive: true }); }
});

test('resource reservations cannot oversubscribe GPUs, CPU, RAM or job slots', () => {
  for (const request of [job({ cpus: 17 }), job({ memory_mb: 65537 }), job({ gpus: 3 })]) {
    const store = new ResourceStore(':memory:');
    try {
      store.registerNode(node());
      const queued = store.createJob(request);
      assert.throws(() => store.startJob(queued.id, 'gpu-01'), StoreError);
      assert.equal(store.getJob(queued.id).status, 'queued');
    } finally { store.close(); }
  }
  const store = new ResourceStore(':memory:');
  try {
    store.registerNode(node({ max_concurrent_jobs: 1 }));
    store.startJob(store.createJob(job()).id, 'gpu-01');
    const second = store.createJob(job());
    assert.throws(() => store.startJob(second.id, 'gpu-01'), /available capacity/);
    assert.equal(store.getNode('gpu-01').status, 'busy');
    assert.equal(store.getJob(second.id).allocation_held, false);
  } finally { store.close(); }
});

test('separate database clients share authoritative reservations', () => {
  const directory = mkdtempSync(join(tmpdir(), 'dashboard-concurrency-'));
  const path = join(directory, 'db.sqlite');
  const first = new ResourceStore(path);
  const second = new ResourceStore(path);
  try {
    first.registerNode(node({ gpus: 1 }));
    const a = first.createJob(job());
    const b = second.createJob(job());
    first.startJob(a.id, 'gpu-01');
    assert.throws(() => second.startJob(b.id, 'gpu-01'), /available capacity/);
    assert.equal(second.getNode('gpu-01').reserved.gpus, 1);
  } finally { first.close(); second.close(); rmSync(directory, { recursive: true }); }
});

test('fractional CPU capacity is exact at one thousandth of a core', () => {
  const store = new ResourceStore(':memory:');
  try {
    store.registerNode(node({ cpus: 0.3 }));
    store.startJob(store.createJob(job({ cpus: 0.1, gpus: 0 })).id, 'gpu-01');
    store.startJob(store.createJob(job({ cpus: 0.2, gpus: 0 })).id, 'gpu-01');
    assert.equal(store.getNode('gpu-01').reserved.cpus, 0.3);
    assert.equal(store.getNode('gpu-01').available.cpus, 0);
  } finally { store.close(); }
});

test('offline nodes advertise no capacity and cannot accept assignments', () => {
  let now = 1_000_000;
  const store = new ResourceStore(':memory:', () => now);
  try {
    store.registerNode(node());
    now += LEASE_MS;
    assert.equal(store.getNode('gpu-01').status, 'offline');
    assert.deepEqual(store.getNode('gpu-01').available, { cpus: 0, memory_mb: 0, gpus: 0 });
    assert.throws(() => store.startJob(store.createJob(job()).id, 'gpu-01'), StoreError);
    assert.equal(store.heartbeatNode('gpu-01').status, 'available');
  } finally { store.close(); }
});

test('expired or cancelled jobs hold resources until their owner confirms cleanup', () => {
  for (const outcome of ['expire', 'cancel']) {
    let now = 1_000_000;
    const store = new ResourceStore(':memory:', () => now);
    try {
      store.registerNode(node());
      const queued = store.createJob(job());
      const claim = store.startJob(queued.id, 'gpu-01');
      if (outcome === 'expire') now += LEASE_MS;
      else store.cancelJob(queued.id);
      store.heartbeatNode('gpu-01');
      assert.equal(store.getNode('gpu-01').status, 'cleanup_required');
      assert.equal(store.getNode('gpu-01').reserved.gpus, 1);
      assert.throws(() => store.startJob(store.createJob(job()).id, 'gpu-01'), StoreError);
      assert.throws(() => store.registerNode(node()), StoreError);
      assert.throws(() => store.finishJob(queued.id, 'wrong-token', 'completed'), StoreError);
      const finished = store.finishJob(queued.id, claim.lease_token, 'completed');
      assert.equal(finished.status, outcome === 'expire' ? 'failed' : 'cancelled');
      assert.equal(store.getNode('gpu-01').status, 'available');
    } finally { store.close(); }
  }
});

test('job heartbeats renew both the execution lease and node liveness', () => {
  let now = 1_000_000;
  const store = new ResourceStore(':memory:', () => now);
  try {
    store.registerNode(node());
    const queued = store.createJob(job());
    const claim = store.startJob(queued.id, 'gpu-01');
    now += LEASE_MS - 1;
    store.heartbeatJob(queued.id, claim.lease_token);
    now += 100;
    assert.equal(store.getJob(queued.id).status, 'running');
    assert.equal(store.getNode('gpu-01').last_seen, now - 100);
  } finally { store.close(); }
});

test('cloud stock snapshots replace old rows, expire, and stay separate from registered nodes', () => {
  let now = 1_000_000;
  const store = new ResourceStore(':memory:', () => now);
  try {
    assert.equal(store.cloudInventory(false).status, 'not_configured');
    assert.equal(store.cloudInventory(true).status, 'never_synced');
    store.saveCloudCapacity([{ product_name: 'l40s-48gb.1x', location: 'us-east1-a', quantity: 5, num_slices: 1 }]);
    assert.equal(store.cloudInventory(true).stale, false);
    assert.deepEqual(store.listNodes(), []);
    now += CAPACITY_TTL_MS;
    assert.equal(store.cloudInventory(true).stale, true);
    store.saveSyncError('Provider unavailable');
    assert.equal(store.cloudInventory(true).capacities.length, 1);
    assert.equal(store.cloudInventory(true).status, 'error');
    store.saveCloudCapacity([]);
    assert.equal(store.cloudInventory(true).capacities.length, 0);
    assert.equal(store.cloudInventory(true).error, null);
    assert.ok(store.listOfferings().filter((item) => item.access === 'on_demand')
      .every((item) => item.id.startsWith('a100-80gb.') || item.id.startsWith('l40s-')));
  } finally { store.close(); }
});
