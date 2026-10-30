import * as esbuild from "esbuild";

const isWatch = process.argv.includes("--watch");

const buildOptions = {
  entryPoints: ["src/index.tsx"],
  bundle: true,
  format: "esm",
  outdir: "dist",
  entryNames: "main",
  // Use automatic JSX runtime (React 17+) - no need for "import React"
  jsx: "automatic",
  external: [
    "react",
    "react-dom",
    "thingwatcher-bindings",
    // Keep esm.sh/CDN URLs external (react-shiki, etc.)
    "https://esm.sh/*",
    "https://cdn.jsdelivr.net/*",
  ],
  sourcemap: true,
  minify: false, // Keep readable for debugging
  // Add CSS import banner so thingwatcher knows to bundle the CSS
  banner: {
    js: 'import "./main.css";',
  },
  logLevel: "info",
};

if (isWatch) {
  const ctx = await esbuild.context(buildOptions);
  await ctx.watch();
  console.log("Watching for changes...");
} else {
  await esbuild.build(buildOptions);
  console.log("Build complete.");
}
