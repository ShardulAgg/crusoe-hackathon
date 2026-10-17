import assert from 'node:assert/strict';
import { once } from 'node:events';
import type { AddressInfo } from 'node:net';
import test from 'node:test';
import { createApp } from './app.ts';
import { ResourceStore } from './store.ts';

test('HTTP API covers registration, on-demand matching, running jobs and cleanup', async () => {
  const store = new ResourceStore(':memory:');
  const server = createApp(store).listen(0, '127.0.0.1');
  await once(server, 'listening');
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/api`;
  const request = async (path: string, body?: unknown) => {
    const response = await fetch(url + path, body === undefined ? undefined : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() };
  };
  try {
    assert.equal((await request('/nodes/register', { id: 'invalid' })).status, 400);
    assert.equal((await request('/nodes/register', {
      id: 'crusoe-01', name: 'Crusoe node', cpus: 8, memory_mb: 16384, gpus: 1,
      allocation_mode: 'on_demand', provider: 'crusoe',
    })).status, 201);
    assert.equal((await request('/resources/available?allocation_mode=on_demand&gpus=1')).body.nodes.length, 1);
    assert.equal((await request('/resources/available?cpus=9')).body.nodes.length, 0);
    const queued = (await request('/jobs', { title: 'Test run', cpus: 4, memory_mb: 8192, gpus: 1 })).body;
    const claim = (await request(`/jobs/${queued.id}/start`, { node_id: 'crusoe-01' })).body;
    assert.equal(claim.job.status, 'running');
    assert.equal((await request('/nodes/crusoe-01/jobs?status=running')).body.total, 1);
    assert.equal((await request('/jobs?node_id=crusoe-01')).body.total, 1);
    assert.equal((await request('/nodes')).body.nodes[0].available.gpus, 0);
    assert.ok(!JSON.stringify((await request('/jobs')).body).includes(claim.lease_token));
    assert.equal((await request(`/jobs/${queued.id}/finish`, {
      lease_token: claim.lease_token, status: 'completed',
    })).status, 400);
    assert.equal((await request(`/jobs/${queued.id}/finish`, {
      lease_token: claim.lease_token, status: 'completed', cleanup_confirmed: true,
    })).status, 200);
    assert.equal((await request('/resources/available?gpus=1')).body.nodes.length, 1);
    assert.equal((await request('/jobs?limit=0')).status, 400);
    assert.equal((await request('/nodes/missing')).status, 404);
    assert.equal((await request('/does-not-exist')).status, 404);
    const denied = await fetch(url + '/nodes', { headers: { Origin: 'https://unrelated.example' } });
    assert.equal(denied.status, 403);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    store.close();
  }
});
