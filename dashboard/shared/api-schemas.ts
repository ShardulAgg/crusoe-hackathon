import { z } from 'zod';
import { benchmarkSchema, submissionSchema } from './benchmarks.ts';

export const benchmarkDefinitionSchema = {
  ...z.toJSONSchema(benchmarkSchema, { io: 'input' }),
  title: 'Create a benchmark',
  description: 'POST /api/benchmarks. Immutable definition: objective, submission JSON Schema, scoring rules, and verifier integration. Semantic URL, status, template, and score-range checks also apply.',
};
export const solutionSubmissionSchema = {
  ...z.toJSONSchema(submissionSchema, { io: 'input' }),
  title: 'Submit a benchmark solution',
  description: 'POST /api/benchmarks/{id}/submissions. The payload must additionally satisfy that benchmark\'s submission_schema. Scores are written only by the verifier.',
};
