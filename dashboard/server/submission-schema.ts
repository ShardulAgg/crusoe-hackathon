import Ajv from 'ajv';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import type { ErrorObject, ValidateFunction } from 'ajv';
import { StoreError } from './store.ts';

const MAX_SCHEMA_BYTES = 64 * 1024;
const MAX_SCHEMA_DEPTH = 64;
const validators = new Map<string, ValidateFunction>();
const allowedDrafts = new Set([
  'https://json-schema.org/draft/2020-12/schema',
  'https://json-schema.org/draft/2020-12/schema#',
  'http://json-schema.org/draft-07/schema#',
  'https://json-schema.org/draft-07/schema#',
]);

function inspect(value: unknown, depth = 0): void {
  if (depth > MAX_SCHEMA_DEPTH) throw new StoreError(400, 'Submission schema exceeds the nesting limit of 64');
  if (!value || typeof value !== 'object') return;
  if (Array.isArray(value)) {
    for (const child of value) inspect(child, depth + 1);
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    if (key === '__proto__') throw new StoreError(400, 'Submission schema cannot contain __proto__ properties');
    if (['$ref', '$dynamicRef', '$recursiveRef'].includes(key)
      && (typeof child !== 'string' || !child.startsWith('#'))) {
      throw new StoreError(400, 'Submission schema references must be local fragments, such as #/$defs/solution');
    }
    if (key === '$async') throw new StoreError(400, 'Submission schema must validate synchronously');
    inspect(child, depth + 1);
  }
}

function compile(schema: Record<string, unknown>): ValidateFunction {
  if (schema === null || Array.isArray(schema) || typeof schema !== 'object') {
    throw new StoreError(400, 'Submission schema must be a JSON Schema object');
  }
  let encoded: string;
  try { encoded = JSON.stringify(schema); }
  catch { throw new StoreError(400, 'Submission schema must contain JSON values'); }
  if (Buffer.byteLength(encoded) > MAX_SCHEMA_BYTES) {
    throw new StoreError(400, 'Submission schema must be at most 64 KiB');
  }
  const cached = validators.get(encoded);
  if (cached) return cached;
  // Work on JSON-only data and never fetch a schema, reference, or identifier.
  const definition = JSON.parse(encoded) as Record<string, unknown>;
  inspect(definition);
  const draft = definition.$schema;
  if (draft !== undefined && (typeof draft !== 'string' || !allowedDrafts.has(draft))) {
    throw new StoreError(400, 'Submission schema must use JSON Schema draft 2020-12 or draft-07');
  }
  const options = {
    allErrors: true, strictSchema: true, strictTypes: false, strictTuples: false,
    strictRequired: false, ownProperties: true, coerceTypes: false,
    useDefaults: false, removeAdditional: false, validateFormats: true,
  };
  const ajv = typeof draft === 'string' && draft.includes('draft-07')
    ? new Ajv(options) : new Ajv2020(options);
  addFormats(ajv);
  // AJV registers the draft-07 meta-schema at its canonical http identifier.
  if (draft === 'https://json-schema.org/draft-07/schema#') {
    definition.$schema = 'http://json-schema.org/draft-07/schema#';
  }
  try {
    const validate = ajv.compile(definition);
    if (validators.size >= 64) validators.delete(validators.keys().next().value!);
    validators.set(encoded, validate);
    return validate;
  } catch (error) {
    if (error instanceof StoreError) throw error;
    const detail = error instanceof Error ? error.message.slice(0, 1000) : 'Invalid JSON Schema';
    throw new StoreError(400, `Invalid submission schema: ${detail}`);
  }
}

function describe(error: ErrorObject): string {
  const path = `payload${error.instancePath}`;
  if (error.keyword === 'required') return `${path}: required field "${String(error.params.missingProperty)}" is missing`;
  if (error.keyword === 'additionalProperties') return `${path}: field "${String(error.params.additionalProperty)}" is not allowed`;
  return `${path}: ${error.message ?? 'does not match the submission schema'}`;
}

export function validateSubmissionSchema(schema: Record<string, unknown>): void {
  compile(schema);
}

export function validateSubmissionPayload(schema: Record<string, unknown>, payload: unknown): void {
  const validate = compile(schema);
  if (!validate(payload)) {
    const errors = (validate.errors ?? []).slice(0, 8).map(describe);
    throw new StoreError(422, `Submission does not match the benchmark format. ${errors.join('; ')}`);
  }
}
