import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'DASHBOARD_');
  const key = process.env.DASHBOARD_API_KEY || env.DASHBOARD_API_KEY;
  const apiPort = process.env.DASHBOARD_API_PORT || env.DASHBOARD_API_PORT || '3001';
  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': {
          target: `http://127.0.0.1:${apiPort}`,
          headers: key ? { Authorization: `Bearer ${key}` } : {},
        },
      },
    },
  };
});
