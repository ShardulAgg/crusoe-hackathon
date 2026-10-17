import type { Benchmark, Submission } from '../shared/benchmarks.ts';
import { StoreError } from './store.ts';

const forbidden = new Set(['__proto__', 'prototype', 'constructor']);

/** A dot path or JSON Pointer; "$" (or an empty path) selects the whole response. */
export function readPath(value: unknown, path: string): unknown {
  if (!path || path === '$') return value;
  const segments = path.startsWith('/') ? path.slice(1).split('/').map((key) => key.replaceAll('~1', '/').replaceAll('~0', '~'))
    : path.replace(/^\$\./, '').split('.');
  let current = value;
  for (const key of segments) {
    if (forbidden.has(key) || current === null || typeof current !== 'object'
      || !Object.hasOwn(current, key)) return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

const tokenPattern = /\{\{\s*([^{}]+?)\s*\}\}/g;
function allowedToken(token: string) {
  return ['benchmark.id', 'benchmark.title', 'benchmark.version', 'submission.id', 'submission.title',
    'submission.submitted_by', 'submission.payload'].includes(token)
    || (token.startsWith('submission.payload.') && token.split('.').every((part) => !forbidden.has(part)));
}

export function validateRequestTemplate(value: unknown) {
  if (JSON.stringify(value).length > 65536) throw new StoreError(400, 'Verification request template must be at most 64 KiB');
  function walk(item: unknown) {
    if (typeof item === 'string') {
      for (const match of item.matchAll(tokenPattern)) {
        if (!allowedToken(match[1])) throw new StoreError(400, `Unknown verification request placeholder: ${match[1]}`);
      }
    } else if (Array.isArray(item)) item.forEach(walk);
    else if (item && typeof item === 'object') {
      for (const [key, child] of Object.entries(item)) {
        if (forbidden.has(key)) throw new StoreError(400, 'Unsupported verification request property');
        if (key.includes('{{')) throw new StoreError(400, 'Request placeholders belong in values, not field names');
        walk(child);
      }
    }
  }
  walk(value);
}

export function renderTemplate(template: unknown, benchmark: Benchmark, submission: Submission): unknown {
  const context = { benchmark, submission };
  function resolve(token: string) {
    if (!allowedToken(token)) throw new Error('Unknown request placeholder');
    const value = readPath(context, token);
    if (value === undefined) throw new Error(`Submission is missing the request field ${token}`);
    return value;
  }
  function walk(item: unknown): unknown {
    if (typeof item === 'string') {
      const exact = item.match(/^\{\{\s*([^{}]+?)\s*\}\}$/);
      if (exact) return resolve(exact[1]);
      return item.replace(tokenPattern, (_match, token: string) => {
        const value = resolve(token);
        return typeof value === 'string' ? value : JSON.stringify(value);
      });
    }
    if (Array.isArray(item)) return item.map(walk);
    if (item && typeof item === 'object') return Object.fromEntries(Object.entries(item).map(([key, value]) => [key, walk(value)]));
    return item;
  }
  return walk(template);
}
