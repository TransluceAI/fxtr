# Phase 2 Step 18post3: Module-Level State and Twind Integration

## Goal

Add `usePersistentGlobalState` hook for module-scoped state (shared across all things in a dynamic renderer module), update state setters to support updater functions, and build a demo showing Tailwind CSS support via twind.

## Background

Currently `usePersistentThingState(handle, key)` stores state keyed by `shortId:key`, so each thing instance has its own state. We want a new hook `usePersistentGlobalState(handle, key)` that stores state keyed by `moduleId:key`, shared across all things rendered by the same module.

The primary use case is storing shared resources like a twind instance + observer that processes Tailwind classes. Since renderers run inside shadow DOM, we can safely use twind's observe mode without interference.

## Design

### 1. Module ID in Handles

The handle factory needs to include `moduleId` in the metadata so hooks can access it:

```ts
// handleFactory.ts
export interface HandleMetadata {
  key: string;
  keyJSON: unknown;
  shortId: number;
  thingType: string;
  moduleId: string;  // NEW: content-addressed module ID
}
```

The `moduleId` will be passed down from DynamicRenderer through the component that creates handles.

### 2. Updater Function Support

Both `usePersistentThingState` and `usePersistentGlobalState` should accept updater functions:

```ts
// Current API
const [value, setValue] = usePersistentThingState<T>(handle, key);
setValue(newValue);  // direct value

// Enhanced API (same signature, but setValue accepts updater)
setValue(newValue);           // direct value
setValue(prev => prev + 1);   // updater function
```

This requires updating `persistentStateStore.set()` to handle the updater pattern:

```ts
// persistentStateStore.ts
set<T>(key: string, valueOrUpdater: T | undefined | ((prev: T | undefined) => T | undefined)): void {
  const newValue = typeof valueOrUpdater === 'function'
    ? (valueOrUpdater as (prev: T | undefined) => T | undefined)(this.state.get(key) as T | undefined)
    : valueOrUpdater;
  // ... rest unchanged
}
```

### 3. usePersistentGlobalState Hook

New hook in `rendererHooks.ts`:

```ts
export function usePersistentGlobalState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined | ((prev: T | undefined) => T | undefined)) => void] {
  const h = handle as unknown as InternalHandle;
  if (!h[fullRendererBrand]) {
    throw new Error('usePersistentGlobalState requires a FullRendererHandle');
  }
  return h._usePersistentGlobalState!<T>(key);
}
```

Implementation in `handleFactory.ts`:

```ts
function createUsePersistentGlobalState(moduleId: string) {
  return <T,>(key: string): [T | undefined, (v: T | undefined | ((prev: T | undefined) => T | undefined)) => void] => {
    const storageKey = `module:${moduleId}:${key}`;
    // Same pattern as usePersistentThingState but different key prefix
    // ...
  };
}
```

### 4. Type Definitions Update

Update `thingwatcher-bindings.d.ts`:

```ts
export declare function usePersistentGlobalState<T>(
  handle: FullRendererHandle,
  key: string
): [T | undefined, (value: T | undefined | ((prev: T | undefined) => T | undefined)) => void];
```

Also update the `usePersistentThingState` signature to show updater support.

### 5. Twind Demo

New file `examples/demo_dynamic_rendering/TwindDemo.tsx`:

```tsx
import { useRef, useCallback } from 'react';
import { twind, cssom, observe } from 'https://esm.sh/@twind/core';
import presetTailwind from 'https://esm.sh/@twind/preset-tailwind';
import { usePayload, useDraggableIdMarker, usePersistentGlobalState } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps } from 'thingwatcher-bindings';

// Types for our global state (shared across module, not per-shadow-root)
interface TwindState {
  tw: ReturnType<typeof twind>;  // twind instance
  sheet: CSSStyleSheet;          // the stylesheet to attach
}

function TwindDemoFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  // Store observer cleanup function
  const observerCleanupRef = useRef<(() => void) | null>(null);

  // Get or initialize twind state (shared across all TwindDemo instances in this module)
  const [twindState, setTwindState] = usePersistentGlobalState<TwindState>(handle, 'twind');

  // Initialize twind on first use (synchronous since imports are top-level)
  if (!twindState) {
    setTwindState(prev => {
      if (prev) return prev; // Race condition guard

      const sheet = new CSSStyleSheet();
      const tw = twind({
        presets: [presetTailwind()],
      }, cssom(sheet));

      return { tw, sheet };
    });
  }

  // Callback ref: attach stylesheet and observer when element mounts
  const containerRef = useCallback((node: HTMLDivElement | null) => {
    // Cleanup previous observer if any
    if (observerCleanupRef.current) {
      observerCleanupRef.current();
      observerCleanupRef.current = null;
    }

    if (!node || !twindState) return;

    const shadowRoot = node.getRootNode() as ShadowRoot;
    if (!(shadowRoot instanceof ShadowRoot)) return;

    // Attach stylesheet if not already attached
    if (!shadowRoot.adoptedStyleSheets.includes(twindState.sheet)) {
      shadowRoot.adoptedStyleSheets = [...shadowRoot.adoptedStyleSheets, twindState.sheet];
    }

    // Create observer for this shadow root
    const obs = observe(twindState.tw, shadowRoot);
    observerCleanupRef.current = () => obs.disconnect();
  }, [twindState]);

  const data = payload as { title?: string; items?: string[] } | null;

  return (
    <div ref={containerRef} className="p-4 bg-slate-100 rounded-lg">
      <DraggableIDMarker>
        <h3 className="text-xl font-bold text-blue-600">{data?.title}</h3>
      </DraggableIDMarker>
      <ul className="mt-2 space-y-1">
        {data?.items?.map((item, i) => (
          <li key={i} className="px-2 py-1 bg-white rounded shadow-sm hover:bg-blue-50">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "TwindDemo": { full: TwindDemoFull },
  }
} as ThingWatcherRenderConfig;
```

### 6. Demo Registration

Update `demo.py` to register the TwindDemo thing type and create some sample things.

## Implementation Steps

- [x] 1. Update `HandleMetadata` interface to include `moduleId`
- [x] 2. Update handle factory functions to accept and store `moduleId`
- [x] 3. Update DynamicRenderer to pass `moduleId` when creating handles
- [x] 4. Update `persistentStateStore.set()` to support updater functions
- [x] 5. Update `createUsePersistentThingState` to pass updater to store
- [x] 6. Add `createUsePersistentGlobalState` in handleFactory.ts
- [x] 7. Add `usePersistentGlobalState` hook in rendererHooks.ts
- [x] 8. Export `usePersistentGlobalState` from ThingWatcherBindingsExternal.tsx
- [x] 9. Update `thingwatcher-bindings.d.ts` with new hook and updater signatures
- [x] 10. Create `TwindDemo.tsx` renderer
- [x] 11. Update `demo.py` to register and create TwindDemo things
- [ ] 12. Test that twind classes render correctly
- [x] 13. Update CODE_OVERVIEW.md

## Open Questions / Risks

1. **Multiple shadow roots**: If the same module renders things in multiple shadow roots, each will attach the same stylesheet and create its own observer. The shared stylesheet ensures classes generated by any observer are available to all shadow roots. Each observer disconnects on unmount.

2. **Preflight styles**: twind's preflight (CSS reset) may or may not be desired. The demo uses the default preset which includes preflight. Users can configure via the twind config if needed.

3. **First-render initialization**: The twind state is initialized synchronously on first render (not in a useEffect), which is slightly unusual but ensures the state is ready before the callback ref fires. This works because usePersistentGlobalState with an updater is safe to call during render.

4. **Callback ref cleanup**: The callback ref stores its cleanup function in a regular ref. When the callback fires with `null` (unmount) or a new node, we run the previous cleanup. This ensures proper observer disconnection.

---

## Follow-up: AdoptStyleSheet Component

### Problem

The current approach of imperatively modifying `shadowRoot.adoptedStyleSheets` in user code has a race condition - multiple sources (ShadowRootDiv's prop stylesheets and user code like TwindDemo) can both mutate the array, leading to lost stylesheets.

### Solution

Add a React component `<AdoptStyleSheet sheet={} />` that declaratively adopts a stylesheet into the containing shadow root. This uses context to pass the stylesheet reactively back to `ShadowRootDiv`, which combines it with its own prop stylesheets.

### Design

**New context in ShadowDOM.tsx:**

```tsx
// Context for child components to register stylesheets
interface AdoptedStyleSheetContextValue {
  registerSheet: (id: string, sheet: CSSStyleSheet) => void;
  unregisterSheet: (id: string) => void;
}

const AdoptedStyleSheetContext = createContext<AdoptedStyleSheetContextValue | null>(null);
```

**AdoptStyleSheet component:**

```tsx
export interface AdoptStyleSheetProps {
  /** The CSSStyleSheet to adopt into the shadow root */
  sheet: CSSStyleSheet;
}

/**
 * AdoptStyleSheet - Declaratively adopts a stylesheet into the containing shadow root.
 *
 * Use this inside a ShadowRootDiv to add additional stylesheets (e.g., from twind)
 * without imperatively modifying adoptedStyleSheets.
 *
 * @example
 * ```tsx
 * const [twindState] = usePersistentGlobalState<TwindState>(handle, 'twind');
 * return (
 *   <div>
 *     {twindState && <AdoptStyleSheet sheet={twindState.sheet} />}
 *     <div className="p-4 bg-slate-800">...</div>
 *   </div>
 * );
 * ```
 */
export function AdoptStyleSheet({ sheet }: AdoptStyleSheetProps) {
  const id = useId();
  const ctx = useContext(AdoptedStyleSheetContext);

  useEffect(() => {
    if (!ctx) {
      console.warn('AdoptStyleSheet used outside a ShadowRootDiv');
      return;
    }
    ctx.registerSheet(id, sheet);
    return () => ctx.unregisterSheet(id);
  }, [ctx, id, sheet]);

  return null; // Renders nothing
}
```

**Updated ShadowRootDiv:**

```tsx
export function ShadowRootDiv({
  children,
  stylesheets,
  className,
  style,
}: ShadowRootDivProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [shadowRoot, setShadowRoot] = useState<ShadowRoot | null>(null);

  // Track child-adopted stylesheets
  const [childSheets, setChildSheets] = useState<Map<string, CSSStyleSheet>>(new Map());

  // Context value for children to register/unregister sheets
  const adoptedCtxValue = useMemo<AdoptedStyleSheetContextValue>(() => ({
    registerSheet: (id, sheet) => {
      setChildSheets(prev => new Map(prev).set(id, sheet));
    },
    unregisterSheet: (id) => {
      setChildSheets(prev => {
        const next = new Map(prev);
        next.delete(id);
        return next;
      });
    },
  }), []);

  // Attach shadow root on mount
  useEffect(() => {
    if (hostRef.current && !hostRef.current.shadowRoot) {
      const shadow = hostRef.current.attachShadow({ mode: 'open' });
      setShadowRoot(shadow);
    }
  }, []);

  // Combine prop stylesheets with child-adopted stylesheets
  useEffect(() => {
    if (shadowRoot) {
      const allSheets = [
        ...(stylesheets ?? []),
        ...childSheets.values(),
      ];
      shadowRoot.adoptedStyleSheets = allSheets;
    }
  }, [shadowRoot, stylesheets, childSheets]);

  return (
    <AdoptedStyleSheetContext.Provider value={adoptedCtxValue}>
      <ShadowHostContext.Provider value={hostRef.current}>
        <div ref={hostRef} className={className} style={style}>
          {shadowRoot && createPortal(children, shadowRoot)}
        </div>
      </ShadowHostContext.Provider>
    </AdoptedStyleSheetContext.Provider>
  );
}
```

**Export from ThingWatcherBindingsExternal.tsx:**

```tsx
// Wrap in ShadowSlot? No - AdoptStyleSheet renders nothing, just uses context
export { AdoptStyleSheet } from './ShadowDOM';
```

**Type definition in thingwatcher-bindings.d.ts:**

```ts
export interface AdoptStyleSheetProps {
  /** The CSSStyleSheet to adopt into the shadow root */
  sheet: CSSStyleSheet;
}

/**
 * AdoptStyleSheet - Declaratively adopts a stylesheet into the containing shadow root.
 */
export declare const AdoptStyleSheet: FC<AdoptStyleSheetProps>;
```

### Updated TwindDemo usage

```tsx
function TwindDemoFull({ handle }: FullRendererProps) {
  // ... twind initialization ...

  // Callback ref for observer only (no stylesheet manipulation)
  const containerRef = useCallback((node: HTMLDivElement | null) => {
    if (!node || !twindState) return;
    twindState.obs.observe(node);
  }, [twindState]);

  return (
    <>
      {twindState && <AdoptStyleSheet sheet={twindState.sheet} />}
      <div ref={containerRef} className="p-4 bg-slate-800 rounded-lg">
        {/* ... */}
      </div>
    </>
  );
}
```

### Implementation Steps

- [x] 1. Add `AdoptedStyleSheetContext` and `AdoptStyleSheet` component to ShadowDOM.tsx
- [x] 2. Update `ShadowRootDiv` to track child-adopted stylesheets and combine with prop stylesheets
- [x] 3. Export `AdoptStyleSheet` from ThingWatcherBindingsExternal.tsx
- [x] 4. Add type definition to thingwatcher-bindings.d.ts
- [x] 5. Update TwindDemo.tsx to use `<AdoptStyleSheet>` instead of imperative mutation
- [ ] 6. Test that stylesheets from both sources (prop and AdoptStyleSheet) are applied
