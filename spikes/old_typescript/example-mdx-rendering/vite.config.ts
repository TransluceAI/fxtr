import mdx from "@mdx-js/rollup";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
	plugins: [
		// MDX files use our custom JSX runtime (server-side JSON output)
		mdx({
			jsxImportSource: "@fxtr/mdx-transform",
		}),
		// React .tsx files use the standard React JSX runtime (client-side)
		react({
			// Only apply to .tsx/.jsx files, not .mdx
			include: /\.(tsx|jsx)$/,
		}),
	],
});
