import { defineConfig } from "vitest/config";

export default defineConfig({
	test: {
		include: [
			"v2/ts/packages/*/tests/**/*.test.{ts,tsx}",
			"projects/fxtr_workstation/frontend_lib/packages/*/tests/**/*.test.{ts,tsx}",
			"projects/fxtr_experiment/frontend/tests/**/*.test.{ts,tsx}",
			// Deprecated, archived-but-kept packages (see pnpm-workspace.yaml).
			"spikes/old_typescript/*/tests/**/*.test.{ts,tsx}",
		],
	},
});
