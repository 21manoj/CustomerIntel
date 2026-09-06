import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev proxy: the backend issues an httponly session cookie tied to its own
// origin, so the frontend must hit /app/api under the SAME origin the browser
// sees. Point PROXY_TARGET at whichever backend you're running against.
const PROXY_TARGET = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8101'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/app/api': { target: PROXY_TARGET, changeOrigin: true },
    },
  },
})
