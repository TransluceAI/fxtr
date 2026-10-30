import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react()],
	server: {
		// Distinct from the sibling frontend_lib demos (3461-3464) and
		// the assembly-stage workstation demo (3470).
		port: 3465,
	},
});
