import { defineConfig } from "vitest/config";

export default defineConfig({
  // Linked fxtr source must use this environment's React and shared context instances.
  resolve: { dedupe: ["react", "react-dom", "jotai", "fxtr-view"] },
  test: {
    environment: "jsdom",
    globals: true, // lets @testing-library/react clean up after each test
    include: ["test/**/*.test.ts", "test/**/*.test.tsx"],
    // CSS Modules keep their local class names in tests, so assertions can name them.
    css: { include: [/.+/], modules: { classNameStrategy: "non-scoped" } },
  },
});
