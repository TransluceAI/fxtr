import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react()],
	server: {
		// Fixed port — doesn't collide with the sibling
		// frontend_lib demos (3461-3463 / 3470). Operator
		// opens http://localhost:3471 once start_demo.sh has
		// brought up both halves.
		port: 3471,
	},
});
