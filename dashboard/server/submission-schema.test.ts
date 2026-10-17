import assert from 'node:assert/strict';
import test from 'node:test';
import { validateSubmissionPayload, validateSubmissionSchema } from './submission-schema.ts';
import { StoreError } from './store.ts';

const schema = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  title: 'Candidate solution',
  description: 'The complete submission contract',
  type: 'object',
  properties: {
    repository: { type: 'string', format: 'uri' },
    commit: { type: 'string', pattern: '^[a-f0-9]{40}$' },
    strategy: { enum: ['baseline', 'optimized'] },
    parameters: {
      type: 'object', properties: { batch_size: { type: 'integer', minimum: 1, maximum: 32 } },
      required: ['batch_size'], additionalProperties: false,
    },
  },
  required: ['repository', 'commit', 'strategy', 'parameters'],
  additionalProperties: false,
};
const valid = {
  repository: 'https://github.com/example/solution', commit: 'a'.repeat(40),
  strategy: 'optimized', parameters: { batch_size: 4 },
};
const status = (expected: number, text?: RegExp) => (error: unknown): boolean => {
  assert.ok(error instanceof StoreError);
  assert.equal(error.status, expected);
  if (text) assert.match(error.message, text);
  return true;
};

test('submission format validates nested required fields, enums, constraints and additional properties', () => {
  validateSubmissionSchema(schema);
  validateSubmissionPayload(schema, valid);
  assert.throws(() => validateSubmissionPayload(schema, { ...valid, parameters: {} }), status(422, /batch_size/));
  assert.throws(() => validateSubmissionPayload(schema, { ...valid, strategy: 'unknown' }), status(422, /allowed values/));
  assert.throws(() => validateSubmissionPayload(schema, { ...valid, parameters: { batch_size: 33 } }), status(422, /payload\/parameters\/batch_size/));
  assert.throws(() => validateSubmissionPayload(schema, { ...valid, extra: true }), status(422, /extra/));
  assert.throws(() => validateSubmissionPayload(schema, { ...valid, repository: 'not a URL' }), status(422, /uri/));
  assert.throws(() => validateSubmissionPayload(schema, { ...valid, commit: 'main' }), status(422, /pattern/));
});

test('submission validation never coerces values, inserts defaults, removes fields or changes zero and false', () => {
  const contract = {
    type: 'object', properties: {
      score: { type: 'number', default: 10 }, enabled: { type: 'boolean', default: true },
      note: { type: 'string', default: 'hello' },
    }, additionalProperties: false,
  };
  const payload = { score: 0, enabled: false };
  validateSubmissionPayload(contract, payload);
  assert.deepEqual(payload, { score: 0, enabled: false });
  const invalid = { score: '0', extra: true };
  assert.throws(() => validateSubmissionPayload(contract, invalid), status(422));
  assert.deepEqual(invalid, { score: '0', extra: true });
});

test('offline local definitions and draft-07 schemas are supported', () => {
  const contract = { $defs: { id: { type: 'string', minLength: 3 } }, type: 'array', items: { $ref: '#/$defs/id' } };
  validateSubmissionPayload(contract, ['abc']);
  assert.throws(() => validateSubmissionPayload(contract, ['a']), status(422));
  const draft7 = { $schema: 'http://json-schema.org/draft-07/schema#', type: 'integer', minimum: 0 };
  validateSubmissionPayload(draft7, 0);
  assert.throws(() => validateSubmissionPayload(draft7, -1), status(422));
});

test('remote references, async schemas, invalid contracts and excessive schema data are rejected at creation', () => {
  assert.throws(() => validateSubmissionSchema({ $ref: 'https://example.com/schema.json' }), status(400, /local fragments/));
  assert.throws(() => validateSubmissionSchema({ $dynamicRef: 'https://example.com/schema.json#id' }), status(400));
  assert.throws(() => validateSubmissionSchema({ $async: true, type: 'object' }), status(400, /synchronously/));
  assert.throws(() => validateSubmissionSchema({ type: 'object', properties: { value: { type: 'made-up-type' } } }), status(400, /Invalid submission schema/));
  assert.throws(() => validateSubmissionSchema({ type: 'string', format: 'invented-format' }), status(400));
  assert.throws(() => validateSubmissionSchema({ $schema: 'https://example.com/custom-schema' }), status(400));
  assert.throws(() => validateSubmissionSchema({ description: 'x'.repeat(64 * 1024) }), status(400, /64 KiB/));
  assert.throws(() => validateSubmissionSchema(JSON.parse('{"properties":{"__proto__":{"type":"string"}}}')), status(400));
});

test('array and scalar submissions are supported and inherited fields do not satisfy required fields', () => {
  validateSubmissionPayload({ type: 'array', items: { type: 'number' }, minItems: 2 }, [0, 1]);
  validateSubmissionPayload({ type: 'boolean' }, false);
  const contract = { type: 'object', properties: { value: { type: 'string' } }, required: ['value'] };
  assert.throws(() => validateSubmissionPayload(contract, Object.create({ value: 'inherited' })), status(422));
});
