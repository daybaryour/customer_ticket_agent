import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind IPv4 explicitly: the browser (and the app's fetch calls) use
    // 127.0.0.1, which fails when Vite only listens on IPv6 ::1.
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    hmr: {
      protocol: 'ws',
      host: '127.0.0.1',
      port: 5173,
    },
    // Mirror the nginx proxy so `npm run dev` and the containerised build both
    // use the same relative /api URLs. The rewrite strips the prefix, matching
    // `proxy_pass http://backend:8000/;` in frontend/nginx.conf.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
