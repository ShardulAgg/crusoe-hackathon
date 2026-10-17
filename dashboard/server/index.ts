import { fileURLToPath } from 'node:url';
import { createApp } from './app.ts';
import { ResourceStore } from './store.ts';
import { BenchmarkStore } from './benchmark-store.ts';
import { VerificationWorker } from './verification-worker.ts';

const store = new ResourceStore(process.env.DASHBOARD_DB_PATH
  ?? fileURLToPath(new URL('../data/dashboard.sqlite', import.meta.url)));
const port = Number(process.env.DASHBOARD_API_PORT || 3001);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid DASHBOARD_API_PORT');
const benchmarks = new BenchmarkStore(store);
const verifier = new VerificationWorker(benchmarks);
const server = createApp(store, benchmarks).listen(port, '127.0.0.1', () => {
  console.log(`Dashboard API: http://127.0.0.1:${port}`);
  verifier.start();
});
function shutdown() {
  server.close(() => {
    void verifier.stop().finally(() => { store.close(); process.exit(0); });
  });
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
