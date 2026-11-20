import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const devHost = process.env.SAFYDO_DEV_HOST ?? "safydo.local";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Point at your Pi: SAFYDO_DEV_HOST=safydo.local npm run dev
    proxy: {
      "/api": { target: `http://${devHost}:8000`, changeOrigin: true },
      "/ws": { target: `ws://${devHost}:8000`, ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 500,
  },
});