import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react()],
	server: {
		// Distinct from `@fxtr-ui/demo` (3461) and
		// `@fxtr-ui/dispatch-demo` (3462) so the trio can run
		// side-by-side against different workstation backends.
		port: 3464,
	},
});
