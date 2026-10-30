import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [react()],
	server: {
		// Distinct from sibling demos (`@fxtr-ui/demo` on 3461,
		// `@fxtr-ui/dispatch-demo` on 3462) so all three can run
		// against different workstation backends side-by-side.
		port: 3463,
	},
});
