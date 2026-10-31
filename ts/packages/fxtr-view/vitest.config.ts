import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true, // lets @testing-library/react clean up after each test
    include: ["test/**/*.test.ts", "test/**/*.test.tsx"],
  },
});
