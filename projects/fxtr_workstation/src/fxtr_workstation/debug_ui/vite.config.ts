import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes into dist/, which the Python package's
// `WorkstationServer` mounts at /debug via Starlette `StaticFiles`.
// Use a relative `base` so the bundle's asset URLs work regardless of
// what URL prefix the workstation mounts under.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
