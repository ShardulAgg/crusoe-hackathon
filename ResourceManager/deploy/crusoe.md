# Generic compute on Crusoe

ResourceManager provides a compute queue, available resource snapshots, bounded allocations, and opaque UTF-8 job I/O. It runs independently of Library and has no paper, LLM, verification-API, or verdict logic.

## Existing execution hosts

Use existing dedicated Crusoe VMs as Docker hosts. Install Docker, the approved workload image, and any stronger runtime isolation before enabling the host's pool. Configure an operator-owned Docker context and bind it to the same registered `node_id` in the sibling [Resources inventory](../../Resources/README.md).

Register node capacity through ResourceManager's own `POST /nodes/register` endpoint. Keep its heartbeat fresher than 120 seconds using `POST /nodes/{id}/heartbeat`. Registration declares capacity; it neither probes the host nor configures Docker credentials. The verification service's node registry is unrelated to this compute registry.

Pools can be labelled `on_demand` or `reserved`; both use the same bounded allocation path on configured hosts. Each job creates a fresh container when a worker claims its resources. These modes do not purchase cloud reservations, create VMs, or provision another cloud service.

## Control plane and network

Run the ResourceManager API and worker with their own local persistent database. The worker needs access to its configured Docker hosts. Clients need only the authenticated ResourceManager API. Protect remote access with HTTPS or a private network, and keep host credentials and private image/context configuration on the manager side.

Containers have direct networking disabled and exchange only bounded process input/output with the compute service. ResourceManager does not proxy external application APIs. A client such as [Library](../../Library/README.md) interprets its own workload protocol and can broker approved application actions through those channels, using credentials retained on the client's trusted host.

Research checked against official documentation on October 17, 2026. We could not confirm a public managed-sandbox lifecycle API. Crusoe documents [VMs](https://docs.crusoecloud.com/compute/virtual-machines/index.html), [managed Kubernetes](https://docs.crusoecloud.com/orchestration/cmk/overview/index.html), and [infrastructure APIs](https://docs.crusoecloud.com/api/).

A [custom VPC starts without firewall rules](https://docs.crusoecloud.com/networking/firewall-rules/overview/index.html), while the default VPC includes broad outbound allowances. Add the flows required by compute clients, the manager control plane, operator access, and configured execution hosts. Crusoe firewall rules filter IP/CIDR, protocol, and port; they complement container isolation and client-owned application permissions.

## Operations

Preserve ResourceManager's database independently of each client's database. Generic jobs and allocation keys survive restarts. Retry an uncertain job submission with the same `Idempotency-Key`; a different allocation requires a new key. Do not share a SQLite file between services, hosts, or network-mounted filesystems.

Use a process supervisor for API and worker restart policies. Reconcile orphaned containers before acknowledging uncertain cleanup or reusing held capacity. ResourceManager reports `cleanup_required` and conservatively retains allocations when removal cannot be confirmed. Docker containers share their VM host's kernel; a microVM or stronger boundary is a separate deployment choice.

No Crusoe infrastructure is created by starting these services. The included backend only creates containers on existing configured Docker hosts.
