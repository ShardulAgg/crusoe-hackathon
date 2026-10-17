import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import test from 'node:test';
import { fetchCrusoeCapacity, signedHeaders } from './crusoe.ts';

const secret = Buffer.from('fixture-only-secret').toString('base64url');

test('Crusoe signing uses the documented path, decoded secret, UTC timestamp and empty query line', () => {
  const timestamp = '2026-10-17T18:00:00+00:00';
  const signature = createHmac('sha256', Buffer.from('fixture-only-secret'))
    .update(`/v1/capacities\n\nGET\n${timestamp}\n`).digest('base64url');
  assert.deepEqual(signedHeaders('/v1/capacities', 'fixture-key', secret, timestamp), {
    'X-Crusoe-Timestamp': timestamp,
    Authorization: `Bearer 1.0:fixture-key:${signature}`, Accept: 'application/json',
  });
});

test('capacity adapter makes only an authenticated read to the official endpoint', async () => {
  const request: typeof fetch = async (url, options) => {
    assert.equal(url, 'https://api.cloud.crusoe.ai/v1/capacities');
    assert.equal(options?.method ?? 'GET', 'GET');
    assert.equal(options?.redirect, 'error');
    assert.ok((options?.headers as Record<string, string>).Authorization.startsWith('Bearer 1.0:fixture-key:'));
    return Response.json({ items: [{ type: 'l40s-48gb.1x', location: 'us-east1-a', quantity: 3, num_slices: 1 }] });
  };
  assert.deepEqual(await fetchCrusoeCapacity('fixture-key', secret, request), [
    { product_name: 'l40s-48gb.1x', location: 'us-east1-a', quantity: 3, num_slices: 1 },
  ]);
});

test('provider errors and invalid stock data never become fabricated availability', async () => {
  for (const response of [
    Response.json({ items: [{ type: 'a', location: 'b', quantity: -1 }] }),
    Response.json({ items: [{ type: 'a', location: 'b', quantity: '4' }] }),
    Response.json({ items: [
      { type: 'a', location: 'b', quantity: 1 }, { type: 'a', location: 'b', quantity: 2 },
    ] }),
    new Response('fixture-secret-that-must-not-leak', { status: 401 }),
    new Response('not json'),
  ]) {
    await assert.rejects(fetchCrusoeCapacity('fixture-key', secret, async () => response), (error: Error) => {
      assert.ok(!error.message.includes('fixture-secret-that-must-not-leak'));
      return true;
    });
  }
});

test('missing credentials do not make a provider request', async () => {
  let requested = false;
  await assert.rejects(fetchCrusoeCapacity('', '', async () => {
    requested = true;
    return Response.json({ items: [] });
  }), /credentials/);
  assert.equal(requested, false);
});
