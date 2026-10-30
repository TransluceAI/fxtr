# Plan: Unify React Instances for Dynamic Renderers

## Problem Statement

We have **two conflicting React instances**:

| Instance | Source | Used by |
|----------|--------|---------|
| **Bundled React** | esbuild bundles React into main.js | Main app, dynamic renderers (via virtual module shim in esbuildSetup.ts) |
| **esm.sh React** | Import map points `react` → esm.sh | esm.sh dependencies with `external=react` that resolve via browser import map |

The conflict occurs when:
1. Dynamic renderer imports a library from esm.sh (e.g., a charting library)
2. esbuildSetup.ts rewrites that import to `https://esm.sh/some-lib?external=react`
3. The esm.sh module contains `import React from 'react'` (externalized)
4. Browser resolves `react` via import map → fetches from esm.sh
5. Now the library uses esm.sh React while the renderer uses bundled React → **two instances**

This breaks React because hooks, context, and reconciliation fail when components from different React instances interact.

## Approaches Comparison

### Approach 1: Load React from Import Map Everywhere

**Idea:** Make the main app also load React from esm.sh via import map, so everyone uses the same CDN instance.

**How it would work:**
- Mark `react`, `react-dom` as external in server.py's esbuild
- Import map already points to esm.sh
- Main app's `import React from 'react'` resolves via import map → esm.sh
- esm.sh dependencies also resolve via import map → same esm.sh instance
- Remove virtual module injection in esbuildSetup.ts (no longer needed)

**Pros:**
- Conceptually simple - one source of truth (esm.sh)
- Smaller main.js bundle (React not included)
- Dynamic renderers don't need special handling

**Cons:**
- "Dynamic require of 'react' is not supported" errors from CommonJS dependencies
- Requires CDN availability at runtime
- Version pinning complexity (esm.sh version must match expectations)
- Any dependency using `require('react')` breaks the build

**Verdict:** Fragile due to CommonJS interop. Would need to audit/replace dependencies.

---

### Approach 2: Global Scope + Shims

**Idea:** Keep bundling React in main app, but expose it globally and redirect import map to shims that re-export from globals.

**How it would work:**
- Main app bundles React normally (no esbuild changes)
- Early in main.tsx, expose: `window.__REACT_SHARED__ = React`
- Create shim files that re-export from globals: `export const useState = window.__REACT_SHARED__.useState`
- Update import map to point `react` → `/shims/react-shim.js` instead of esm.sh
- esm.sh dependencies resolve via import map → shim → bundled instance

**Pros:**
- No changes to main app's build process
- No CommonJS interop issues
- Works offline (no CDN dependency)
- Timing is safe (shims only load after main app sets up globals)

**Cons:**
- Shim files must enumerate all React exports
- Slightly more moving parts (globals + shims + import map)

**Verdict:** More robust, recommended approach.

---

## Chosen Approach: Global Scope + Auto-Generated Shims

The main idea:
1. Main app continues bundling React normally (no changes to server.py esbuild)
2. Main app populates `window.__THINGWATCHER_INJECTED__` (same global as esbuildSetup.ts)
3. Auto-generate shim modules at build time by introspecting actual package exports
4. Serve shims that re-export from the globals
5. Update import map to point to local shims instead of esm.sh
6. Dynamic renderer esm.sh dependencies resolve via import map → shims → same instance

### Why This Works

The key insight is that **dynamic renderers are only compiled after the main app has fully loaded**. So by the time any import map resolution happens for dynamic content, the globals are guaranteed to be set up.

Loading order:
1. HTML loads, import map is parsed (but not executed yet)
2. `main.js` loads and executes, populating `window.__THINGWATCHER_INJECTED__`
3. User interaction triggers dynamic renderer compilation
4. Compiled renderer imports esm.sh module
5. esm.sh module has `import React from 'react'` (externalized)
6. Browser resolves via import map → `/shims/react.js`
7. Shim reads from `window.__THINGWATCHER_INJECTED__["react"]` → same instance!

## Design

### 1. Globals Setup in main.tsx

Add to very top of `main.tsx` (must run before any React usage):

```typescript
// Shared module injection - MUST be at top before any usage
// This populates the same global used by esbuildSetup.ts for dynamic renderers
import * as React from 'react';
import * as ReactJsxRuntime from 'react/jsx-runtime';
import * as ReactDom from 'react-dom';
import * as ReactDomClient from 'react-dom/client';

declare global {
  interface Window {
    __THINGWATCHER_INJECTED__: Record<string, unknown>;
  }
}

window.__THINGWATCHER_INJECTED__ = {
  ...window.__THINGWATCHER_INJECTED__,  // Preserve any existing entries
  'react': React,
  'react/jsx-runtime': ReactJsxRuntime,
  'react-dom': ReactDom,
  'react-dom/client': ReactDomClient,
};
```

### 2. Auto-Generated Shim Modules

Instead of manually maintaining shim files, we generate them at build time by introspecting the actual packages. This ensures shims always have the correct exports without manual maintenance.

**Node script (`frontend/scripts/generate-shim.mjs`):**

```javascript
// Usage: node generate-shim.mjs <module-name>
// Outputs shim code that re-exports from window.__THINGWATCHER_INJECTED__[moduleName]

const moduleName = process.argv[2];

const mod = await import(moduleName);
const exports = Object.keys(mod).filter(k => k !== '__esModule');

let code = `const __mod = window.__THINGWATCHER_INJECTED__["${moduleName}"];\n`;

for (const exp of exports) {
  if (exp === 'default') {
    code += `export default __mod;\n`;
  } else {
    code += `export const ${exp} = __mod.${exp};\n`;
  }
}

// If no explicit default export, export the module object as default
if (!exports.includes('default')) {
  code += `export default __mod;\n`;
}

process.stdout.write(code);
```

**Example generated output for `react`:**

```javascript
const __mod = window.__THINGWATCHER_INJECTED__["react"];
export const Children = __mod.Children;
export const Component = __mod.Component;
export const Fragment = __mod.Fragment;
// ... all other exports auto-discovered from the actual package
export default __mod;
```

### 3. Python Integration in FrontendHandler

```python
# Modules to generate shims for (must match what main.tsx populates)
SHIM_MODULES = [
    "react",
    "react/jsx-runtime",
    "react-dom",
    "react-dom/client",
]

class FrontendHandler:
    def __init__(self, dev_mode: bool = False):
        # ... existing init ...
        self.shims: dict[str, str] = {}  # module_name -> generated shim code

        if not dev_mode:
            self._generate_shims()

    def _generate_shims(self) -> None:
        """Generate shim code for each injected module by introspecting actual packages."""
        for module_name in SHIM_MODULES:
            result = subprocess.run(
                ["node", "scripts/generate-shim.mjs", module_name],
                cwd=self.frontend_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            self.shims[module_name] = result.stdout

    def _get_shim(self, module_name: str) -> str | None:
        """Get shim code, regenerating if needed in dev mode."""
        if self.dev_mode or module_name not in self.shims:
            self._generate_shims()
        return self.shims.get(module_name)

    async def serve_shim(self, request: Request) -> Response:
        """Serve a generated shim module."""
        # Route: /shims/react.js -> module_name = "react"
        # Route: /shims/react/jsx-runtime.js -> module_name = "react/jsx-runtime"
        path = request.path_params["path"]
        module_name = path.removesuffix(".js")

        shim_code = self._get_shim(module_name)
        if shim_code is None:
            return Response(f"Unknown shim: {module_name}", status_code=404)
        return Response(shim_code, media_type="application/javascript")
```

### 4. Update Import Map

Change `index.html` (and embed.html, debug-embed.html):

```html
<script type="importmap">
{
    "imports": {
        "react": "/shims/react.js",
        "react/jsx-runtime": "/shims/react/jsx-runtime.js",
        "react-dom": "/shims/react-dom.js",
        "react-dom/client": "/shims/react-dom/client.js",
        "thingwatcher-bindings": "/thingwatcher-bindings.js"
    }
}
</script>
```

### 5. Server Routes

Add shim route in `create_app()`:

```python
routes = [
    # ... existing routes ...
    Route("/shims/{path:path}", frontend.serve_shim, methods=["GET"]),
]
```

### 6. esbuildSetup.ts Alignment

The existing code in esbuildSetup.ts already uses `window.__THINGWATCHER_INJECTED__` with module names as keys. The `generateVirtualModuleCode` function generates essentially the same code as our shims. **No changes needed** - direct React imports in dynamic renderers will continue to use the virtual module injection, while esm.sh dependencies will use the import map → shims path. Both read from the same global.

## Implementation Steps

1. [x] Create `frontend/scripts/generate-shim.mjs`
2. [x] Update `main.tsx` to populate `window.__THINGWATCHER_INJECTED__` at the top
3. [x] Update `embed.tsx` similarly (also `debug-embed.tsx`)
4. [x] Add `_generate_shims()` and `serve_shim()` to FrontendHandler in `server.py`
5. [x] Add shim route in `create_app()`
6. [x] Update `index.html` import map to use local shims
7. [x] Update `embed.html` import map
8. [x] Update `debug-embed.html` import map
9. [ ] Test: main app loads correctly
10. [ ] Test: dynamic renderer with direct React import works
11. [ ] Test: dynamic renderer using esm.sh library (e.g., a charting lib) works
12. [ ] Update CODE_OVERVIEW.md

## Risks and Mitigations

**Risk:** Node not available when generating shims
- **Mitigation:** We already require pnpm (and thus Node) for frontend builds

**Risk:** Timing issue where shim loads before global is set
- **Mitigation:** This shouldn't happen given loading order. Could add runtime check:
  ```javascript
  if (!window.__THINGWATCHER_INJECTED__?.["react"]) {
    throw new Error("React not initialized in __THINGWATCHER_INJECTED__");
  }
  ```

**Risk:** Some library imports React in a way that bypasses import map
- **Mitigation:** esm.sh normalizes imports; this shouldn't happen in practice

## Open Questions

1. ~~Do embed.tsx/debug-embed.tsx need the same globals setup?~~ Yes, done.
2. ~~Should we add runtime validation in shims that globals exist?~~ Yes, done.
3. ~~Should shim generation happen during `_compile_all()` or lazily on first request?~~ During `_compile_all()`.

## Follow-up: thingwatcher-bindings Unified with Shims

After the initial implementation, we also moved `thingwatcher-bindings` to the same shim pattern:

- Previously: compiled as separate bundle via `_compile_bindings()`, served at `/thingwatcher-bindings.js`
- Now: bundled inside main.js, injected into `__THINGWATCHER_INJECTED__['thingwatcher-bindings']`, served via shim at `/shims/thingwatcher-bindings.js`

**Shim generation for local modules:**
- Created `generate-local-shim.mjs` which compiles TypeScript files with esbuild and uses the metafile to enumerate exports (without needing to import the result)
- Uses `packages: 'external'` to avoid bundling npm deps, and a CSS stub plugin to handle CSS module imports
- This avoids hardcoding exports and keeps the shim in sync with the actual module

This simplifies the build process (one less separate compilation step) and makes all external imports consistent.
