import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import type { CrusoeInventory, ResourceNode } from '../shared/models';

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Unable to load resources');
  return body as T;
}

const statusLabels = {
  available: 'Available', busy: 'Busy', offline: 'Offline', cleanup_required: 'Cleanup required',
};
const accessLabels = { on_demand: 'On demand', reservation: 'Reservation', contact_sales: 'Contact sales' };
const memory = (mb: number) => `${Number((mb / 1024).toFixed(2))} GiB`;

function RegisterNode({ onClose, onRegistered }: { onClose: () => void; onRegistered: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => { dialog.current?.showModal(); }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setSaving(true);
    setError('');
    try {
      await api('/nodes/register', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: form.get('id'), name: form.get('name'), provider: form.get('provider'),
          region: form.get('region'), allocation_mode: form.get('allocation_mode'),
          cpus: Number(form.get('cpus')), memory_mb: Number(form.get('memory')) * 1024,
          gpus: Number(form.get('gpus')), gpu_model: form.get('gpu_model'),
          gpu_memory_mb: Number(form.get('gpu_memory')) * 1024,
          max_concurrent_jobs: Number(form.get('max_concurrent_jobs')),
        }),
      });
      onRegistered();
      onClose();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Unable to register node');
    } finally { setSaving(false); }
  }

  return (
    <dialog ref={dialog} className="node-dialog" onCancel={onClose} aria-labelledby="register-title">
      <form onSubmit={submit}>
        <div className="section-heading">
          <h2 id="register-title">Register node</h2>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="form-grid">
          <label>Node ID<input name="id" required pattern={'[a-zA-Z0-9][a-zA-Z0-9._\\-]{0,99}'} autoFocus /></label>
          <label>Name<input name="name" required maxLength={200} /></label>
          <label>Provider<input name="provider" defaultValue="local" required maxLength={100} /></label>
          <label>Region<input name="region" maxLength={100} /></label>
          <label>CPU cores<input name="cpus" type="number" min="0.001" step="0.001" required /></label>
          <label>Memory (GiB)<input name="memory" type="number" min="0.0625" step="0.0625" required /></label>
          <label>GPU count<input name="gpus" type="number" min="0" max="1024" defaultValue="0" required /></label>
          <label>GPU model<input name="gpu_model" maxLength={200} /></label>
          <label>VRAM per GPU (GiB)<input name="gpu_memory" type="number" min="0" defaultValue="0" /></label>
          <label>Concurrent job limit<input name="max_concurrent_jobs" type="number" min="1" max="1000" defaultValue="1" required /></label>
          <label className="full-width">Availability
            <select name="allocation_mode"><option value="always_on">Always on</option><option value="on_demand">On demand</option></select>
          </label>
        </div>
        {error && <p className="error-text" role="alert">{error}</p>}
        <p className="muted form-note">Nodes stay online by reporting a heartbeat at least every two minutes.</p>
        <div className="dialog-actions">
          <button className="button" type="button" onClick={onClose}>Cancel</button>
          <button className="button button-primary" disabled={saving}>{saving ? 'Registering…' : 'Register node'}</button>
        </div>
      </form>
    </dialog>
  );
}

function NodeRow({ node }: { node: ResourceNode }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <>
      <tr>
        <th scope="row" className="node-name">
          <button className="node-expand" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
            <span aria-hidden="true">{expanded ? '⌄' : '›'}</span> {node.name}
          </button>
          <span className="cell-secondary">{node.provider}{node.region && ` · ${node.region}`}</span>
        </th>
        <td><span className={`node-status node-status-${node.status}`}><span className="status-dot" aria-hidden="true" />{statusLabels[node.status]}</span></td>
        <td className="numeric">{node.available.cpus} <span className="muted">/ {node.cpus}</span></td>
        <td className="numeric">{memory(node.available.memory_mb)} <span className="muted">/ {memory(node.memory_mb)}</span></td>
        <td className="numeric">{node.available.gpus} <span className="muted">/ {node.gpus}</span><span className="cell-secondary">{node.gpu_model || (node.gpus === 0 ? 'CPU only' : 'GPU')}</span></td>
        <td>{node.jobs.filter((job) => job.status === 'running').length} <span className="muted">/ {node.max_concurrent_jobs}</span></td>
      </tr>
      {expanded && <tr><td colSpan={6} className="node-details">
        <div className="node-detail-meta">
          <span>ID <strong>{node.id}</strong></span>
          <span>{node.allocation_mode === 'on_demand' ? 'On demand' : 'Always on'}</span>
          <span>Last seen {new Date(node.last_seen).toLocaleString()}</span>
        </div>
        <h3>Assigned jobs</h3>
        {node.jobs.length === 0 ? <p className="muted">No jobs assigned to this node.</p> : <ul className="assigned-jobs">
          {node.jobs.map((job) => <li key={job.id}>
            <div><strong>{job.title}</strong><span className="cell-secondary">{job.id}</span></div>
            <span>{job.status}{job.allocation_held && job.status !== 'running' ? ' · awaiting cleanup' : ''}</span>
            <span className="muted">{job.cpus} CPUs · {memory(job.memory_mb)} · {job.gpus} GPUs</span>
          </li>)}
        </ul>}
        {Object.keys(node.specifications).length > 0 && <details><summary>Additional specifications</summary><pre>{JSON.stringify(node.specifications, null, 2)}</pre></details>}
      </td></tr>}
    </>
  );
}

export default function Resources({ active }: { active: boolean }) {
  const [nodes, setNodes] = useState<ResourceNode[]>([]);
  const [cloud, setCloud] = useState<CrusoeInventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [syncing, setSyncing] = useState(false);
  const [registering, setRegistering] = useState(false);
  const [access, setAccess] = useState('on_demand');

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [nodeData, cloudData] = await Promise.all([
        api<{ nodes: ResourceNode[] }>('/nodes', { signal }),
        api<CrusoeInventory>('/providers/crusoe', { signal }),
      ]);
      if (signal?.aborted) return;
      setNodes(nodeData.nodes);
      setCloud(cloudData);
      setError('');
    } catch (failure) {
      if (signal?.aborted) return;
      setNodes([]);
      setError(failure instanceof Error ? failure.message : 'Unable to load resources');
    } finally { if (!signal?.aborted) setLoading(false); }
  }, []);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(controller.signal);
    const interval = window.setInterval(() => { void load(controller.signal); }, 15000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [active, load]);

  async function refreshCapacity() {
    setSyncing(true);
    try {
      setCloud(await api<CrusoeInventory>('/providers/crusoe/sync', { method: 'POST' }));
      setError('');
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Unable to refresh Crusoe capacity');
      await load();
    } finally { setSyncing(false); }
  }

  const offerings = cloud?.offerings.filter((offering) => access === 'all' || offering.access === access) ?? [];

  return (
    <div className="resources">
      <div className="resources-heading">
        <h1>Resources</h1>
        <button className="button" onClick={() => { void load(); }} disabled={loading}>Refresh</button>
      </div>
      {error && <p className="error-banner" role="alert">{error}</p>}

      <section className="resource-section" aria-labelledby="nodes-title">
        <div className="section-heading">
          <div><h2 id="nodes-title">Registered nodes <span className="node-count">{nodes.length}</span></h2><p className="muted">Available capacity / total capacity</p></div>
          <button className="button button-primary" onClick={() => setRegistering(true)}>Register node</button>
        </div>
        <div className="nodes-table-container">
          <table className="nodes-table" aria-label="Registered nodes">
            <thead><tr><th scope="col">Node</th><th scope="col">Status</th><th scope="col">CPU</th><th scope="col">Memory</th><th scope="col">GPU</th><th scope="col">Jobs</th></tr></thead>
            <tbody>{loading ? <tr><td className="nodes-empty" colSpan={6}>Loading nodes…</td></tr>
              : error && nodes.length === 0 ? <tr><td className="nodes-empty" colSpan={6}>Node inventory could not be loaded.</td></tr>
                : nodes.length === 0 ? <tr><td className="nodes-empty" colSpan={6}>No nodes registered yet.</td></tr>
                  : nodes.map((node) => <NodeRow key={node.id} node={node} />)}</tbody>
          </table>
        </div>
      </section>

      <section className="resource-section" aria-labelledby="crusoe-title">
        <div className="section-heading">
          <div><h2 id="crusoe-title">Crusoe GPU servers</h2><p className="muted">Published server specifications and live capacity</p></div>
          <button className="button" onClick={() => { void refreshCapacity(); }} disabled={!cloud?.configured || syncing}>{syncing ? 'Checking…' : 'Check live capacity'}</button>
        </div>
        <div className="cloud-toolbar">
          <select aria-label="Crusoe access model" value={access} onChange={(event) => setAccess(event.target.value)}>
            <option value="on_demand">On demand</option><option value="all">All GPU servers</option><option value="reservation">Reservation required</option><option value="contact_sales">Contact sales</option>
          </select>
          <span className="muted" role="status">{!cloud ? 'Loading catalog…' : !cloud.configured ? 'Live capacity not connected'
            : cloud.status === 'error' ? cloud.error : cloud.last_success_at == null ? 'Live capacity has not been checked'
              : `${cloud.stale ? 'Snapshot is stale · ' : 'Checked '}${new Date(cloud.last_success_at).toLocaleTimeString()}`}</span>
        </div>
        <div className="nodes-table-container">
          <table className="nodes-table catalog-table" aria-label="Crusoe GPU servers">
            <thead><tr><th scope="col">Server type</th><th scope="col">GPU</th><th scope="col">vCPU</th><th scope="col">Memory</th><th scope="col">Access</th><th scope="col">Capacity</th></tr></thead>
            <tbody>{offerings.length === 0 ? <tr><td className="nodes-empty" colSpan={6}>{!cloud ? 'Loading server catalog…' : 'No server types match this filter.'}</td></tr>
              : offerings.map((offering) => {
                const capacity = cloud!.capacities.filter((item) => item.product_name === offering.id);
                return <tr key={offering.id}>
                  <th scope="row"><a className="server-type" href={offering.source_url} target="_blank" rel="noreferrer">{offering.id}</a><span className="cell-secondary">{offering.regions.join(', ')}</span></th>
                  <td>{offering.gpus} × {offering.gpu_model}<span className="cell-secondary">{offering.gpu_memory_gb ?? '—'} GB per GPU</span><span className="cell-secondary mobile-server-specs">{offering.cpus ?? '—'} vCPU · {offering.memory_gb ?? '—'} GB RAM</span></td>
                  <td>{offering.cpus ?? '—'}</td><td className="numeric">{offering.memory_gb != null ? `${offering.memory_gb} GB` : '—'}</td>
                  <td><span className={`access-badge access-${offering.access}`}>{accessLabels[offering.access]}</span></td>
                  <td>{!cloud!.configured || cloud!.last_success_at == null ? <span className="muted">Not checked</span>
                    : capacity.length === 0 ? <span className="muted">Not reported</span>
                      : capacity.map((item) => <span className="capacity-line" key={item.location}>{item.quantity} units <span className="muted">· {item.location}{cloud!.stale ? ' · stale' : ''}</span></span>)}</td>
                </tr>;
              })}</tbody>
          </table>
        </div>
        <p className="catalog-note muted">Capacity is a snapshot and depends on account access and quota. Server types are not registered worker nodes.</p>
        {cloud && cloud.capacities.length > 0 && <details className="reported-capacity">
          <summary>All reported capacity by region{cloud.stale ? ' · stale snapshot' : ''}</summary>
          <div className="nodes-table-container"><table className="nodes-table" aria-label="Crusoe reported capacity">
            <thead><tr><th scope="col">Server type</th><th scope="col">Region</th><th scope="col">Reported units</th><th scope="col">Slices per type</th></tr></thead>
            <tbody>{cloud.capacities.map((item) => <tr key={`${item.product_name}:${item.location}`}>
              <th scope="row">{item.product_name}</th><td>{item.location}</td><td>{item.quantity}</td><td>{item.num_slices ?? '—'}</td>
            </tr>)}</tbody>
          </table></div>
        </details>}
        <p className="catalog-note"><a href="https://docs.crusoecloud.com/compute/virtual-machines/overview/" target="_blank" rel="noreferrer">Crusoe specifications</a><span className="muted"> · </span><a href="https://support.crusoecloud.com/hc/en-us/articles/51755978765467-GPU-Quota-vs-Capacity-Understanding-On-Demand-and-Reserved-Instances-RIPA" target="_blank" rel="noreferrer">Access and reservations</a></p>
      </section>
      {registering && <RegisterNode onClose={() => setRegistering(false)} onRegistered={() => { void load(); }} />}
    </div>
  );
}
