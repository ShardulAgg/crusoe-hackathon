import type { Offering } from '../shared/models.ts';

// Published specifications, not stock counts or account entitlements.
// Access models: https://support.crusoecloud.com/hc/en-us/articles/51755978765467
const source_url = 'https://docs.crusoecloud.com/compute/virtual-machines/overview/';
const verified_on = '2026-10-17';

export const crusoeCatalog: Offering[] = [
  ...[1, 2, 4, 8].map((count): Offering => ({
    id: `a100-80gb.${count}x`, gpu_model: 'NVIDIA A100 PCIe', gpus: count,
    gpu_memory_gb: 80, cpus: 12 * count, memory_gb: 120 * count,
    regions: ['us-east1-a'], access: 'on_demand', source_url, verified_on,
  })),
  ...[1, 2, 4, 8, 10].map((count): Offering => ({
    id: `l40s-48gb.${count}x`, gpu_model: 'NVIDIA L40S', gpus: count,
    gpu_memory_gb: 48, cpus: 8 * count, memory_gb: 147 * count,
    regions: ['us-east1-a', 'us-southcentral1-a'], access: 'on_demand',
    source_url, verified_on,
  })),
  {
    id: 'h100-80gb-sxm-ib.8x', gpu_model: 'NVIDIA H100 SXM', gpus: 8,
    gpu_memory_gb: 80, cpus: 176, memory_gb: 960,
    regions: ['us-east1-a', 'us-southcentral1-a', 'eu-iceland1-a'],
    access: 'reservation', source_url, verified_on,
  },
  {
    id: 'h200-141gb-sxm-ib.8x', gpu_model: 'NVIDIA H200 SXM', gpus: 8,
    gpu_memory_gb: 141, cpus: 176, memory_gb: 2000,
    regions: ['eu-iceland1-a'], access: 'contact_sales', source_url, verified_on,
  },
  {
    id: 'a100-80gb-sxm-ib.8x', gpu_model: 'NVIDIA A100 SXM', gpus: 8,
    gpu_memory_gb: 80, cpus: 96, memory_gb: 960,
    regions: ['us-east1-a'], access: 'reservation', source_url, verified_on,
  },
  {
    id: 'b200-180gb-sxm-ib-8x', gpu_model: 'NVIDIA B200 SXM', gpus: 8,
    gpu_memory_gb: 180, cpus: 176, memory_gb: 3000,
    regions: ['eu-iceland1-a', 'eu-norway1-a', 'us-west1-a'],
    access: 'reservation', source_url, verified_on,
  },
  {
    id: 'b300-288gb-sxm-ib.8x', gpu_model: 'NVIDIA B300 SXM', gpus: 8,
    gpu_memory_gb: 288, cpus: 240, memory_gb: 3100,
    regions: ['eu-iceland1-a'], access: 'reservation', source_url, verified_on,
  },
  {
    id: 'gb200-186gb-nvl-4x', gpu_model: 'NVIDIA GB200', gpus: 4,
    gpu_memory_gb: 186, cpus: 128, memory_gb: 886,
    regions: ['eu-iceland1-a'], access: 'reservation', source_url, verified_on,
  },
  {
    id: 'mi300x-192gb-ib.8x', gpu_model: 'AMD MI300X', gpus: 8,
    gpu_memory_gb: 192, cpus: 240, memory_gb: 2000,
    regions: ['us-east1-a'], access: 'reservation', source_url, verified_on,
  },
  {
    id: 'mi355x-288gb-roce.8x', gpu_model: 'AMD MI355X', gpus: 8,
    gpu_memory_gb: 288, cpus: 240, memory_gb: 3000,
    regions: ['us-east2-a'], access: 'contact_sales', source_url, verified_on,
  },
];
