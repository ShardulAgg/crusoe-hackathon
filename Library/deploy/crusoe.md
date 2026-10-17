# Library and generic compute on Crusoe

Library owns paper reading, the LLM judge, verification-agent orchestration, restricted verification API calls, and the final report. The separate [ResourceManager](../../ResourceManager/README.md) provides generic resource discovery, compute jobs, and opaque job input/output channels.

## Service placement

1. Run the Library API and verification worker with a local persistent Library database. Expose the submission API through an authenticated HTTPS gateway for external agents.
2. Configure Library's LLM provider and verification API credentials on that trusted Library host. Library reads available resources and requests bounded compute allocations over the ResourceManager API; it needs no Docker access.
3. Run ResourceManager with its own persistent database. Keep operator-owned images, Docker contexts, registered-node access, and compute quotas there. It accepts resource requests rather than papers and does not generate requirements or verdicts.
4. Configure existing execution hosts and fresh on-demand job environments using [ResourceManager's deployment instructions](../../ResourceManager/deploy/crusoe.md) and the separate [Resources inventory](../../Resources/README.md). Library sends its agent task and receives results through generic job channels.

Preserve both service databases across restarts. Library retains paper state, plans, and compute job IDs; ResourceManager retains compute jobs, allocation keys, and execution state. Do not share a SQLite database between services or machines.

## Network boundary

The trusted Library worker needs its configured LLM endpoint, ResourceManager API, and verification API. ResourceManager owns node registration in its own database and needs access to its execution hosts; it does not need the verification API's registry or the LLM provider. Verification-agent containers have no direct network access; Library brokers verification API requests over the job's opaque input/output channel. An agent does not receive the host's model, cloud, compute-service, or verification API credentials.

Crusoe [custom VPCs start without firewall rules](https://docs.crusoecloud.com/networking/firewall-rules/overview/index.html), while the default VPC includes broad outbound allowances. Add only the required service flows. Firewall rules filter IP/CIDR, protocol, and port, so they complement the application-level verification route restrictions enforced by Library. A controlled gateway can provide a stable destination for a changing or shared endpoint.

## Infrastructure availability

Research checked against official documentation on October 17, 2026. We could not confirm a public managed sandbox lifecycle API. Crusoe documents [VMs](https://docs.crusoecloud.com/compute/virtual-machines/index.html), [managed Kubernetes](https://docs.crusoecloud.com/orchestration/cmk/overview/index.html), and [infrastructure APIs](https://docs.crusoecloud.com/api/). ResourceManager can launch fresh compute containers on an existing dedicated Crusoe VM through its configured Docker context.

The included configuration allocates environments on configured hosts. It does not create Crusoe VMs, configure a cloud account, or install Docker.
