# Phase 2 Step 18 Post 2: CSS Import Support

## Goal

Allow custom renderers to import CSS from:
1. **Remote URLs** - e.g., `import 'https://cdn.example.com/styles.css'`
2. **Virtual filesystem** - e.g., `import './styles.css'` (currently only `.module.css` works)

Both should support:
- `.css` files → bundled with `css` loader (global styles)
- `.module.css` files → bundled with `local-css` loader (scoped class names)

## Current State

In `esbuildSetup.ts`:

1. **`createVirtualFsPlugin`** (lines 166-201): Handles relative imports like `./styles.module.css`
   - Currently treats ALL `.css` files as `local-css` (line 186-187)
   - Need to differentiate: `.module.css` → `local-css`, `.css` → `css`

2. **`createInjectedDepsPlugin`** (lines 102-160): Handles bare imports and full URLs
   - Full URLs are marked as `external: true` (lines 144-149)
   - Need to intercept `.css`/`.module.css` URLs and fetch + bundle instead

## Changes

### 1. Fix `createVirtualFsPlugin` loader selection

```typescript
// Before:
if (args.path.endsWith('.css')) {
  loader = 'local-css';  // CSS modules
}

// After:
if (args.path.endsWith('.module.css')) {
  loader = 'local-css';  // CSS modules (scoped)
} else if (args.path.endsWith('.css')) {
  loader = 'css';  // Global CSS
}
```

### 2. Add remote CSS handling to `createInjectedDepsPlugin`

In the `onResolve` handler for full URLs, check for `.css` extension and route to a new namespace instead of marking external:

```typescript
// In the full URL handling block:
if (args.path.startsWith('http://') || args.path.startsWith('https://')) {
  // Check for CSS files - fetch and bundle instead of external
  if (args.path.endsWith('.module.css') || args.path.endsWith('.css')) {
    return { path: args.path, namespace: 'remote-css' };
  }
  // ... existing external handling for non-CSS URLs
}
```

Add a new `onLoad` handler to fetch the CSS:

```typescript
build.onLoad({ filter: /.*/, namespace: 'remote-css' }, async (args) => {
  const response = await fetch(args.path);
  if (!response.ok) {
    throw new Error(`Failed to fetch CSS: ${args.path} (${response.status})`);
  }
  const contents = await response.text();
  const loader = args.path.endsWith('.module.css') ? 'local-css' : 'css';
  return { contents, loader };
});
```

## Implementation Steps

- [x] Update `createVirtualFsPlugin` to use `css` loader for `.css` and `local-css` for `.module.css`
- [x] Add `remote-css` namespace handling in `createInjectedDepsPlugin`:
  - [x] Modify `onResolve` to detect CSS URLs
  - [x] Add `onLoad` handler to fetch and return CSS content
- [x] Add test case in `examples/demo_dynamic_rendering/`:
  - [x] Create `GlobalCss.css` with a `.global-box` class (non-module CSS)
  - [x] Create `GlobalCss.tsx` that imports `./GlobalCss.css` and uses bare `className="global-box"`
  - [x] Also imports remote CSS from jsdelivr CDN and uses `.languageLabel` class
  - [x] Add `GlobalCss` class, snapshotter, anchor capturer to `demo.py`
  - [x] Register the renderer and create test instance
- [x] Update CODE_OVERVIEW.md

## Test Case: GlobalCss

A minimal example demonstrating global (non-module) CSS:

**GlobalCss.css:**
```css
.global-box {
  background: #2d5a27;
  border: 2px solid #4a9e3f;
  padding: 12px;
  border-radius: 8px;
  color: white;
}
```

**GlobalCss.tsx:**
```tsx
import './GlobalCss.css';  // Global CSS, no module import
import { usePayload, useDraggableIdMarker } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps } from 'thingwatcher-bindings';

function GlobalCssFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);
  const data = payload as { name?: string } | null;

  return (
    <div className="global-box">  {/* Bare classname, not styles.xyz */}
      <DraggableIDMarker>
        <h3>{data?.name}</h3>
      </DraggableIDMarker>
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: { "GlobalCss": { full: GlobalCssFull } }
} as ThingWatcherRenderConfig;
```

## Notes

- The fetched CSS goes through esbuild's normal CSS processing, so `@import` statements within the fetched CSS that use relative URLs may not resolve correctly (they'd be relative to the esbuild virtual context, not the original URL). This is a known limitation - we're not recursively resolving remote `@import`s.
- Caching of fetched CSS is not implemented - each build will re-fetch. This is fine for now since builds are infrequent.
