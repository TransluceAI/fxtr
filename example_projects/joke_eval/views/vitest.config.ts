import { defineConfig } from "vitest/config";

export default defineConfig({
  // Linked fxtr source must use this environment's React and shared context instances.
  resolve: { dedupe: ["react", "react-dom", "jotai", "fxtr-view"] },
  test: { include: ["test/**/*.test.ts"] },
});
