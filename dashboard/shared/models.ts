import { z } from 'zod';

export const resourceSchema = z.object({
  cpus: z.number().finite().min(0.001).multipleOf(0.001).max(65536),
  memory_mb: z.number().int().positive().max(2 ** 40),
  gpus: z.number().int().min(0).max(1024).default(0),
}).strict();

export const nodeSchema = resourceSchema.extend({
  id: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$/),
  name: z.string().trim().min(1).max(200),
  provider: z.string().trim().min(1).max(100).default('local'),
  region: z.string().trim().max(100).default(''),
  allocation_mode: z.enum(['always_on', 'on_demand']).default('always_on'),
  gpu_model: z.string().trim().max(200).default(''),
  gpu_memory_mb: z.number().int().min(0).max(2 ** 30).default(0),
  max_concurrent_jobs: z.number().int().min(1).max(1000).default(1),
  specifications: z.record(z.string().max(100), z.json()).default({}),
}).strict();

export const jobSchema = resourceSchema.extend({
  title: z.string().trim().min(1).max(300),
  metadata: z.record(z.string().max(100), z.json()).default({}),
}).strict();

export const leaseSchema = z.object({ lease_token: z.string().min(1).max(200) }).strict();
export const finishSchema = leaseSchema.extend({
  status: z.enum(['completed', 'failed']),
  cleanup_confirmed: z.literal(true),
  error: z.string().max(2000).optional(),
}).strict();

export type Capacity = z.infer<typeof resourceSchema>;
export type NodeInput = z.infer<typeof nodeSchema>;
export type JobInput = z.infer<typeof jobSchema>;
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
export type Job = JobInput & {
  id: string;
  node_id: string | null;
  status: JobStatus;
  allocation_held: boolean;
  lease_until: number | null;
  error: string | null;
  created_at: number;
  updated_at: number;
};

export type ResourceNode = NodeInput & {
  status: 'available' | 'busy' | 'offline' | 'cleanup_required';
  available: Capacity;
  reserved: Capacity;
  available_slots: number;
  jobs: Job[];
  last_seen: number;
  created_at: number;
  updated_at: number;
};

export type Offering = {
  id: string;
  gpu_model: string;
  gpus: number;
  gpu_memory_gb: number | null;
  cpus: number | null;
  memory_gb: number | null;
  regions: string[];
  access: 'on_demand' | 'reservation' | 'contact_sales';
  source_url: string;
  verified_on: string;
};

export type CloudCapacity = {
  product_name: string;
  location: string;
  quantity: number;
  num_slices: number | null;
  checked_at: number;
};

export type CrusoeInventory = {
  configured: boolean;
  status: 'not_configured' | 'never_synced' | 'synced' | 'error';
  last_success_at: number | null;
  error: string | null;
  stale: boolean;
  offerings: Offering[];
  capacities: CloudCapacity[];
};
