# Phase 2 Step 17: External Packages Fix for Dynamic Renderers

## Problem Summary

Dynamic renderers can import external packages from esm.sh (e.g., `react-json-view`). However, this causes React version conflicts because:

1. **Double-prefix bug** (fixed): Import paths like `https://esm.sh/react-json-view@1.21.3` were getting transformed to `https://esm.sh/https://esm.sh/...` because the esbuild plugin's regex for "bare imports" also matched full URLs.

2. **React version conflict** (partially addressed): External packages from esm.sh bundle their own copy of React, but we inject our own React instance. This causes the "A React Element from an older version of React was rendered" error.

3. **Import map missing** (current blocker): To fix #2, we added `?external=react,react/jsx-runtime` to esm.sh URLs, which tells esm.sh not to bundle React. However, this makes esm.sh emit bare `import 'react'` statements, expecting the browser to resolve them via an import map. We don't have an import map - we were using virtual module injection via `window.__THINGWATCHER_INJECTED__`.

## What Has Been Done

### 1. Fixed double-prefix bug (`esbuildSetup.ts` lines 121-127)
Added check for existing URL prefixes before prepending `https://esm.sh/`:
```typescript
if (args.path.startsWith('http://') || args.path.startsWith('https://')) {
  // Handle as full URL, don't add esm.sh prefix
}
```

### 2. Added esm.sh externals (`esbuildSetup.ts` lines 118-119, 129-137)
Added `?external=react,react/jsx-runtime` to esm.sh URLs to prevent React bundling:
```typescript
const esmShExternals = 'external=' + encodeURIComponent('react,react/jsx-runtime');
// Added to both auto-generated and user-provided esm.sh URLs
```

### 3. Improved esbuild error handling (`esbuildSetup.ts` lines 219-226)
Check `result.errors` after build and format error messages with file/line info:
```typescript
if (result.errors.length > 0) {
  const errorMessages = result.errors.map(e => {
    const loc = e.location ? `${e.location.file}:${e.location.line}:${e.location.column}: ` : '';
    return `${loc}${e.text}`;
  }).join('\n');
  throw new Error(`Build failed:\n${errorMessages}`);
}
```

### 4. Added better fallback error for module import failures (`esbuildSetup.ts` lines 281-289)
When dynamic import fails, provide context about likely cause (external import failure).

### 5. Added demo test cases
- `ExternalImport.tsx` - imports react-json-view via full esm.sh URL
- `BareImport.tsx` - imports react-json-view via bare module name
- `BrokenImport.tsx` - intentionally bad import to test error display
- Updated `demo.py` with corresponding test types

## Current State

The changes above are in place but external imports still don't work because when esm.sh externalizes React, it emits `import 'react'` which the browser can't resolve without an import map.

---

## Proposed Solution: Import Maps

### Approach

Replace the virtual module injection system with browser-native import maps. The import maps would be added to the HTML entry points (`index.html`, `embed.html`) and would map:

- `react` → esm.sh or bundled version
- `react/jsx-runtime` → esm.sh or bundled version
- `thingwatcher-bindings` → a pre-compiled bundle served by the Python backend at a fixed path

This way, when esbuild compiles a renderer and marks these as external, the browser can resolve them via the import map.

### Benefits

1. Uses browser-native module resolution - no virtual module hacks
2. External packages from esm.sh will use the same React instance as our app
3. Simpler esbuild plugin - just mark modules as external, no virtual code generation

---

## Questions Before Planning

### Q1: React source - esm.sh or bundled?

For `react` and `react/jsx-runtime`, should the import map point to:

**Option A: esm.sh**
```html
<script type="importmap">
{
  "imports": {
    "react": "https://esm.sh/react@18.2.0",
    "react/jsx-runtime": "https://esm.sh/react@18.2.0/jsx-runtime"
  }
}
</script>
```
- Pro: Matches what external packages expect
- Con: Requires network request, version must be pinned carefully

**Option B: Bundled and served locally**
- Pre-compile React into our frontend build
- Serve at fixed paths like `/vendor/react.js`
- Pro: No network dependency, full control
- Con: More build complexity

### Q2: thingwatcher-bindings compilation

For `thingwatcher-bindings`, you mentioned compiling it in the Python backend. Currently `ThingWatcherBindingsExternal.tsx` imports from other modules (`ShadowSlot`, `rendererHooks`, `ThingRenderLib`).

**Option A: Pre-compile during frontend build**
- Add a separate Vite build target that bundles `ThingWatcherBindingsExternal.tsx` with all its dependencies into a standalone module
- Output to a known location (e.g., `dist/thingwatcher-bindings.js`)
- Python backend just serves this static file

**Option B: Compile at Python server startup**
- Python backend runs esbuild/some bundler on startup to compile the bindings
- More dynamic but adds startup complexity

Which approach do you prefer?

### Q3: Import map generation

Should the import map be:

**Option A: Static in HTML files**
- Hardcode the import map in `index.html` and `embed.html`
- Simple, but requires manual updates for version changes

**Option B: Dynamically injected by Python backend**
- Python backend injects the import map into the HTML response
- More flexible, can include dynamic paths

### Q4: Main app React usage

Currently the main app (not dynamic renderers) imports React normally via Vite bundling. With import maps:

- Should the main app also use the import map for React?
- Or should only dynamic renderers use the import map, with the main app continuing to bundle React?

(If both use the same import-mapped React, they'll definitely share the same instance.)

---

## Decisions

- **Q1**: Use esm.sh for React
- **Q2**: Compile thingwatcher-bindings at runtime (same pattern as current Python server)
- **Q3**: Static import map in HTML files
- **Q4**: Main app should use import-mapped React (no longer bundle)

---

## Implementation Plan

### Overview

1. Add import maps to HTML entry points (`index.html`, `embed.html`, `debug-embed.html`)
2. Modify `FrontendHandler` to compile `thingwatcher-bindings` as a separate bundle
3. Update esbuild commands to externalize `react`, `react/jsx-runtime`, and `thingwatcher-bindings`
4. Simplify `esbuildSetup.ts` - remove virtual module injection, just mark modules as external
5. Add route to serve `/thingwatcher-bindings.js`

### Step 1: Add Import Maps to HTML Files ✅

Update `index.html`, `embed.html`, and `debug-embed.html` to include:

```html
<script type="importmap">
{
  "imports": {
    "react": "https://esm.sh/react@19",
    "react/": "https://esm.sh/react@19/",
    "react-dom": "https://esm.sh/react-dom@19",
    "react-dom/": "https://esm.sh/react-dom@19/",
    "thingwatcher-bindings": "/thingwatcher-bindings.js"
  }
}
</script>
```

Notes:
- Trailing slash entries (`react/`, `react-dom/`) handle subpaths like `react/jsx-runtime`, `react-dom/client`
- Using React 19 to match `package.json` (`"react": "^19.0.0"`)
- Must include `react-dom` - it depends on `react` internally
- Import map must appear before any `<script type="module">` tags

### Step 2: Add thingwatcher-bindings Compilation to FrontendHandler ✅

In `server.py`, extend `FrontendHandler`:

```python
# Add to ENTRY_POINTS or create separate handling
BINDINGS_ENTRY = "lib/ThingWatcherBindingsExternal.tsx"
BINDINGS_OUTPUT = "thingwatcher-bindings.js"

def _compile_bindings(self) -> None:
    """Compile thingwatcher-bindings module."""
    entry_path = self.frontend_dir / self.BINDINGS_ENTRY
    dist_dir = self.frontend_dir / "dist"
    dist_dir.mkdir(exist_ok=True)
    out_path = dist_dir / self.BINDINGS_OUTPUT

    result = subprocess.run(
        [
            "pnpm", "exec", "esbuild",
            str(entry_path),
            "--bundle",
            "--format=esm",
            f"--outfile={out_path}",
            # Externalize react - will be resolved via import map
            "--external:react",
            "--external:react/*",
        ],
        cwd=self.frontend_dir,
        capture_output=True,
        text=True,
    )
    # ... error handling, store in self.compiled
```

Add route:
```python
async def serve_bindings(self, _request: Request) -> Response:
    """Serve the thingwatcher-bindings module."""
    content = self._get_compiled("bindings", "js")
    if content is None:
        return Response("Bindings not compiled", status_code=500)
    return Response(content, media_type="application/javascript")
```

### Step 3: Update Main App Compilation to Externalize React ✅

Modify `_compile_entry` in `FrontendHandler` to add external flags:

```python
result = subprocess.run(
    [
        "pnpm", "exec", "esbuild",
        entry_file,
        "--bundle",
        "--format=esm",
        f"--outfile={js_out}",
        # Externalize - resolved via import map
        "--external:react",
        "--external:react/*",
        "--external:react-dom",
        "--external:react-dom/*",
        "--external:thingwatcher-bindings",
    ],
    ...
)
```

### Step 4: Update esbuildSetup.ts ✅ (revised)

Remove virtual module injection. The plugin now just needs to:
1. Mark `react`, `react/jsx-runtime`, `thingwatcher-bindings` as external
2. Handle virtual filesystem for renderer's local imports (e.g., `./utils.tsx`)
3. Rewrite bare imports to esm.sh with `?external=react,react/jsx-runtime`

```typescript
// Remove:
// - INJECTED_MODULES
// - window.__THINGWATCHER_INJECTED__
// - generateVirtualModuleCode()
// - Virtual module onLoad handler

// Simplify createInjectedDepsPlugin to just mark as external:
function createExternalsPlugin(): esbuild.Plugin {
  return {
    name: 'thingwatcher-externals',
    setup(build) {
      // These are resolved via browser import map
      const externalModules = ['react', 'react/jsx-runtime', 'thingwatcher-bindings'];

      build.onResolve({ filter: /^(react|thingwatcher-bindings)/ }, (args) => {
        if (externalModules.includes(args.path) || args.path.startsWith('react/')) {
          return { path: args.path, external: true };
        }
        return null;
      });

      // Handle bare imports - rewrite to esm.sh
      const esmShExternals = 'external=' + encodeURIComponent('react,react/jsx-runtime');

      build.onResolve({ filter: /^[^./]/ }, (args) => {
        // Skip already-handled modules
        if (externalModules.some(m => args.path === m || args.path.startsWith(m + '/'))) {
          return null;
        }

        // Full URLs - add external param if esm.sh
        if (args.path.startsWith('http://') || args.path.startsWith('https://')) {
          let url = args.path;
          if (url.includes('esm.sh/') && !url.includes('external=')) {
            url += (url.includes('?') ? '&' : '?') + esmShExternals;
          }
          return { path: url, external: true };
        }

        // Bare imports - rewrite to esm.sh
        return {
          path: `https://esm.sh/${args.path}?${esmShExternals}`,
          external: true,
        };
      });
    },
  };
}
```

### Step 5: Wire Up Routes ✅

In `create_server()` or wherever routes are registered, add:

```python
app.add_route("/thingwatcher-bindings.js", frontend_handler.serve_bindings)
```

### Step 6: Verify React Version ✅

Check `package.json` for current React version and ensure the import map uses the same version. The esm.sh URL should match exactly.

### Files to Modify

1. `thingwatcher/frontend/index.html` - add import map
2. `thingwatcher/frontend/embed.html` - add import map
3. `thingwatcher/frontend/debug-embed.html` - add import map
4. `thingwatcher/server.py` - add bindings compilation and route, add externals to main compile
5. `thingwatcher/frontend/components/DynamicRenderer/esbuildSetup.ts` - simplify plugin

### Testing

1. Run demo: `python examples/demo_dynamic_rendering/demo.py`
2. Verify main app loads
3. Verify `external_import` and `bare_import` roots render react-json-view correctly
4. Verify `broken_import` shows meaningful error

---

## Final Working Solution (Revised)

The initial plan of using import maps for everything didn't work because of CommonJS/ESM interop issues. The final working solution:

### Architecture

1. **Main app**: Bundles React normally (no externalization)
2. **thingwatcher-bindings**: Compiled by Python server, bundles React (no externalization)
3. **Dynamic renderers (esbuild-wasm)**:
   - `react`, `react/jsx-runtime`, `thingwatcher-bindings` → virtual module injection (same instances as main app)
   - Bare imports → rewritten to esm.sh with `?external=react,react/jsx-runtime`
4. **esm.sh packages**: React externalized → resolved via browser import map

### Key Insight

Everything in the thingwatcher ecosystem (main app, bindings, dynamic renderers) shares the same React instance via virtual module injection. Only third-party esm.sh packages use the import map for React.

This means there are potentially two React instances:
- Bundled React (used by main app, bindings, dynamic renderers)
- esm.sh React (used by external packages like react-json-view)

But this is fine because the external packages don't need to share state with our app - they just need a compatible React version.

### Changes from Original Plan

- `server.py`: bindings compilation does NOT externalize React (removed `--external:react*` flags)
- `server.py`: main app compilation does NOT externalize React
- `esbuildSetup.ts`: `thingwatcher-bindings` added back to `INJECTED_MODULES` (not external)
- Import maps remain in HTML files, but only used by esm.sh packages
