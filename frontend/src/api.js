// All requests use the relative /api prefix: nginx proxies /api/ to
// http://backend:8000/ in Docker, and the Vite dev server proxies /api to
// http://127.0.0.1:8000 during `npm run dev` (see vite.config.js).
// VITE_API_BASE remains available for pointing the UI at another origin.
export const API_BASE = import.meta.env.VITE_API_BASE || '/api'

export function errorMessage(data, fallback) {
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return detail.message || JSON.stringify(detail)
  return fallback
}
