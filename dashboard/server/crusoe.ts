import { createHmac } from 'node:crypto';
import { z } from 'zod';
import type { CloudCapacity } from '../shared/models.ts';

export class CrusoeError extends Error {}

const capacityResponse = z.object({
  items: z.array(z.object({
    type: z.string().min(1).max(200),
    location: z.string().min(1).max(200),
    quantity: z.number().int().nonnegative(),
    num_slices: z.number().int().nonnegative().optional(),
  })).max(10000),
});

export function crusoeConfigured() {
  return Boolean(process.env.CRUSOE_ACCESS_KEY_ID && process.env.CRUSOE_SECRET_KEY);
}

export function signedHeaders(path: string, accessKey: string, secretKey: string, timestamp: string) {
  if (!/^[A-Za-z0-9_-]+$/.test(accessKey) || !/^[A-Za-z0-9_-]+={0,2}$/.test(secretKey)) {
    throw new CrusoeError('Crusoe credentials are not in the expected format');
  }
  const signature = createHmac('sha256', Buffer.from(secretKey, 'base64url'))
    .update(`${path}\n\nGET\n${timestamp}\n`).digest('base64url');
  return {
    'X-Crusoe-Timestamp': timestamp,
    Authorization: `Bearer 1.0:${accessKey}:${signature}`,
    Accept: 'application/json',
  };
}

export async function fetchCrusoeCapacity(
  accessKey = process.env.CRUSOE_ACCESS_KEY_ID,
  secretKey = process.env.CRUSOE_SECRET_KEY,
  request: typeof fetch = fetch,
): Promise<Omit<CloudCapacity, 'checked_at'>[]> {
  if (!accessKey || !secretKey) {
    throw new CrusoeError('Connect Crusoe Cloud API credentials to check live capacity');
  }
  const path = '/v1/capacities';
  const timestamp = new Date().toISOString().replace(/\.\d{3}Z$/, '+00:00');
  try {
    // Only this read-only official endpoint receives the credential. Never follow redirects.
    const response = await request(`https://api.cloud.crusoe.ai${path}`, {
      headers: signedHeaders(path, accessKey, secretKey, timestamp),
      signal: AbortSignal.timeout(15000),
      redirect: 'error',
    });
    if (!response.ok) {
      throw new CrusoeError(response.status === 401 || response.status === 403
        ? 'Crusoe credentials were rejected or do not have capacity access'
        : 'Crusoe capacity service is currently unavailable');
    }
    if (!response.body) throw new CrusoeError('Crusoe returned an empty response');
    const chunks: Uint8Array[] = [];
    let size = 0;
    for await (const chunk of response.body) {
      size += chunk.byteLength;
      if (size > 2 * 1024 * 1024) throw new CrusoeError('Crusoe capacity response is too large');
      chunks.push(chunk);
    }
    const parsed = capacityResponse.safeParse(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    if (!parsed.success) throw new CrusoeError('Crusoe returned an unexpected capacity format');
    const seen = new Set<string>();
    return parsed.data.items.map((item) => {
      const key = JSON.stringify([item.type, item.location]);
      if (seen.has(key)) throw new CrusoeError('Crusoe returned duplicate capacity records');
      seen.add(key);
      return {
        product_name: item.type, location: item.location,
        quantity: item.quantity, num_slices: item.num_slices ?? null,
      };
    });
  } catch (error) {
    if (error instanceof CrusoeError) throw error;
    throw new CrusoeError('Unable to read live Crusoe capacity; try again shortly');
  }
}
