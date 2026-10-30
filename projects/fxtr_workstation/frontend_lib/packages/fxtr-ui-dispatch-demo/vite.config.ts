import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react()],
	server: {
		// Distinct from `@fxtr-ui/demo` (3461) so both can run side-by-
		// side against different workstation backends.
		port: 3462,
	},
});
