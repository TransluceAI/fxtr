# Phase 0 Step 4 Misc 1: Pre-bundling Renderers with npm + esbuild

## Problem Statement

The current renderer setup imports packages directly from esm.sh with version pins (e.g., `https://esm.sh/@uiw/react-codemirror@4.25.4?deps=@codemirror/state@6.5.4...`). Despite careful version pinning, CodeMirror package version incompatibilities cause runtime errors due to how esm.sh resolves transitive dependencies.

## Solution Overview

Pre-bundle all renderer code with its dependencies into a single self-contained module using npm + esbuild. This gives us full control over dependency resolution. The bundled output is then registered with thingwatcher, which will re-compile it but shouldn't need to resolve any new dependencies since everything is already bundled.

## Design Decisions

### Bundler: esbuild
- Fast, simple, good ESM support
- Can output CSS separately
- Easy watch mode

### Package Management: npm
- Drop deno.json, use package.json + tsconfig.json
- Cleaner than mixing deno and npm toolchains
- Type checking via `tsc --noEmit`

### External Packages
These are provided by thingwatcher's runtime and should NOT be bundled:
- `react`
- `react-dom`
- `thingwatcher-bindings`
- `https://esm.sh/*` and `https://cdn.jsdelivr.net/*` URLs (keep react-shiki loading from CDN)

### Output Structure
```
clydra/renderers/
├── dist/
│   ├── main.js      # Bundled ESM module (imports main.css)
│   └── main.css     # Extracted CSS
├── src/             # Source files (moved from root)
│   ├── index.tsx
│   ├── ClydraRoot.tsx
│   ├── formatting.tsx
│   └── ...
├── package.json
├── tsconfig.json
├── build.mjs        # esbuild build script
└── .gitignore       # Ignore node_modules, dist
```

### CSS Handling
- esbuild extracts CSS to `dist/main.css`
- `dist/main.js` includes `import "./main.css"` at the top
- Both files registered with thingwatcher so it knows to bundle them together

## Implementation Steps

1. [x] Create `package.json` with all dependencies
2. [x] Create `tsconfig.json` for type checking (removed - not needed, esbuild compiles TS)
3. [x] Create `build.mjs` esbuild script with build and watch modes
4. [x] Move source files to `src/` subdirectory
5. [x] Convert all esm.sh/CDN imports to npm package imports in source files
6. [x] Remove `deno.json` and `deno.lock`
7. [x] Update `main.py` to register `dist/main.js` and `dist/main.css`
8. [x] Add `.gitignore` for node_modules and dist
9. [x] Test build and watch modes
10. [x] Update CODE_OVERVIEW.md

## Technical Details

### package.json Dependencies

Based on current esm.sh imports in ClydraRoot.tsx and formatting.tsx:

```json
{
  "name": "clydra-renderers",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "node build.mjs",
    "watch": "node build.mjs --watch",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "@codemirror/autocomplete": "^6.18.6",
    "@codemirror/lang-markdown": "^6.5.0",
    "@codemirror/state": "^6.5.4",
    "@codemirror/view": "^6.39.11",
    "@twind/core": "^1.1.3",
    "@twind/preset-tailwind": "^1.1.3",
    "@uiw/react-codemirror": "^4.25.4",
    "diff": "^7.0.0",
    "react-json-view": "^1.21.3",
    "streamdown": "^2.0.1"
  },
  "devDependencies": {
    "@types/diff": "^5.0.0",
    "@types/react": "^18",
    "@types/react-dom": "^18",
    "esbuild": "^0.24.0",
    "typescript": "^5.7.0"
  },
  "peerDependencies": {
    "react": "^18",
    "react-dom": "^18"
  }
}
```

Note: react-shiki is NOT in dependencies - it stays as esm.sh URL imports (external).

### build.mjs Script

```javascript
import * as esbuild from "esbuild";

const isWatch = process.argv.includes("--watch");

const buildOptions = {
  entryPoints: ["src/index.tsx"],
  bundle: true,
  format: "esm",
  outdir: "dist",
  entryNames: "main",
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
  // CSS extraction handled automatically when importing .css files
};

if (isWatch) {
  const ctx = await esbuild.context(buildOptions);
  await ctx.watch();
  console.log("Watching for changes...");
} else {
  await esbuild.build(buildOptions);
  console.log("Build complete.");
}
```

### CSS Import in main.js

esbuild will automatically extract CSS when we import CSS files. We need to ensure the CSS import is preserved. One approach is to add a banner:

```javascript
banner: {
  js: 'import "./main.css";'
}
```

### main.py Changes

```python
# Before:
register_renderer(
    ["ClydraRoot", ...],
    RENDERERS_DIR / "index.tsx",
    imports={
        "./formatting.tsx": RENDERERS_DIR / "formatting.tsx",
        "./diff.module.css": RENDERERS_DIR / "diff.module.css",
        ...
    },
)

# After:
register_renderer(
    ["ClydraRoot", ...],
    RENDERERS_DIR / "dist" / "main.js",
    imports={
        # CSS extracted by esbuild (from diff.module.css)
        "./main.css": RENDERERS_DIR / "dist" / "main.css",
    },
)
```

Note: esm.sh/CDN URLs in the bundled main.js will remain as-is. Thingwatcher's esbuild should handle them as external (same as it did before).

### Import Conversions

**Convert to npm imports** (will be bundled):
```typescript
// Before (esm.sh)
import CodeMirror from "https://esm.sh/@uiw/react-codemirror@4.25.4?external=react,react-dom&deps=...";
import { markdown } from "https://esm.sh/@codemirror/lang-markdown@6.5.0?deps=...";
import ReactJson from "https://esm.sh/react-json-view@1.21.3?external=react";
import { Streamdown } from "https://esm.sh/streamdown@2.0.1?external=react,react-dom&bundle-deps";
import { diffLines } from "https://esm.sh/diff@7.0.0";

// After (npm)
import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import ReactJson from "react-json-view";
import { Streamdown } from "streamdown";
import { diffLines } from "diff";
```

**Keep as esm.sh URLs** (external, unchanged):
```typescript
// These stay exactly as-is - esbuild passes them through
import { ShikiHighlighter } from "https://esm.sh/gh/AVGVSTVS96/react-shiki@react-shiki@0.9.1/package?external=react,react-dom";
import "https://cdn.jsdelivr.net/gh/AVGVSTVS96/react-shiki@react-shiki@0.9.1/package/src/lib/styles.css";
import type { ShikiTransformer } from "https://esm.sh/shiki@1.24.0";
```

### tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "noEmit": true,
    "lib": ["DOM", "DOM.Iterable", "ESNext"],
    "types": ["react", "react-dom"]
  },
  "include": ["src/**/*"]
}
```

## Notes

- After this change, developers need to run `npm run watch` in the renderers directory during development
- The dist/ folder could be gitignored (rebuild on deploy) or committed (simpler CI)

## react-shiki Handling

The `react-shiki` npm package uses tsup with `injectStyles` which won't work with thingwatcher's expectations (needs top-level CSS imports that esbuild can process).

**Approach:** Keep react-shiki loading from esm.sh/CDN URLs unchanged. Mark these URLs as external in esbuild so they pass through unbundled. Thingwatcher will handle them as it did before.

This is simpler than trying to bundle react-shiki - the main problem we're solving is CodeMirror version conflicts, not react-shiki.

## Implementation Notes (2026-01-16)

- **Dropped tsconfig.json**: Not needed since esbuild compiles TypeScript. Removed to avoid peer dependency conflict with twind's optional typescript@^4.8.4 peer dep.
- **react-json-view → react18-json-view**: The original react-json-view package only supports React 17 and below. Switched to react18-json-view which has a compatible API and supports React 18+.
- **Bundle size**: The bundled main.js is ~4.3MB (includes all CodeMirror packages, twind, streamdown, etc.). This is expected and acceptable since it's loaded once.
