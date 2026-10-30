import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react()],
	server: {
		// Dev server (HMR) for the viewer, pointed at a separately-running
		// `fxtr_nix serve` via VITE_PUBLISHED_BACKEND_URL. Distinct port from the
		// other frontend_lib demos (3461-3465, 3470).
		port: 3466,
	},
});
