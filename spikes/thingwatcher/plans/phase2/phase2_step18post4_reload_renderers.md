# Phase 2 Step 18post4: Reload Renderers from Disk

## Goal

Allow registering renderers using pathlib `Path` objects (in addition to strings), and automatically detect when those files change on disk so the frontend can get updated renderer modules. Add a refresh button to the frontend that invalidates the rendering config cache, triggering a refetch that will pick up any changed files.

## Current State

- `register_renderer(thing_types, tsx_source, imports)` accepts only strings
- Module IDs are content-addressed (hash of main + imports)
- Frontend caches `thingTypeConfig` queries and `rendererModule` queries via urql/graphcache
- No way to detect or reload changed renderer source files

## Design

### Part 1: Backend - Path Support and File Tracking

**registry.py changes:**

Store source specifications (path or string) separately from computed content:

```python
@dataclass
class RendererSourceSpec:
    """Specification for a renderer source - either a path or inline string."""
    path: Path | None = None  # If set, read from disk
    content: str | None = None  # If set, use this content directly

@dataclass
class RendererRegistration:
    """A registration of a renderer bundle with its sources."""
    main_spec: RendererSourceSpec
    import_specs: dict[str, RendererSourceSpec]  # import_path -> spec
    # Note: renderer_key is stored in the mapping, not here, since different
    # thing_types can map to different keys within the same module

    # Cached state (invalidated when files change)
    _cached_module_id: str | None = None
    _cached_mtimes: dict[Path, float] | None = None  # path -> mtime when cached
```

New global registry - maps thing_type to (registration, renderer_key) tuple so multiple thing_types can share the same registration object (and thus share the cache):
```python
# Maps thing_type -> (registration, renderer_key)
_thing_type_registrations: dict[str, tuple[RendererRegistration, str]] = {}
```

**`register_renderer()` changes:**

Accept `str | Path` for main source and import values:

```python
def register_renderer(
    thing_types: str | Mapping[str, str] | Sequence[str],
    tsx_source: str | Path,  # NEW: accept Path
    imports: Mapping[str, str | Path] | None = None,  # NEW: values can be Path
) -> str:
    """Register a renderer module for one or more thing types.

    When paths are provided, files are read lazily on demand and changes
    are detected via mtime. Use the frontend refresh button to pick up changes.
    """
```

Implementation:
1. Create `RendererSourceSpec` for main and each import
2. Normalize `thing_types` to a dict of `{thing_type: renderer_key}`
3. Create ONE `RendererRegistration` for the bundle
4. Store `(registration, renderer_key)` tuple for each thing_type in `_thing_type_registrations`
5. Compute and return the initial module ID (reads files if needed)

**New function `get_current_module_id(registration_id)`:**

```python
def get_current_module_id(registration: RendererRegistration) -> str:
    """Get the current module ID, re-reading files if they changed."""
    # Check if any file mtimes have changed
    if registration._cached_module_id and registration._cached_mtimes:
        changed = False
        for path, cached_mtime in registration._cached_mtimes.items():
            try:
                current_mtime = path.stat().st_mtime
                if current_mtime != cached_mtime:
                    changed = True
                    break
            except OSError:
                changed = True
                break

        if not changed:
            return registration._cached_module_id

    # Read current content from specs
    main_content = _read_spec(registration.main_spec)
    imports_content = {k: _read_spec(v) for k, v in registration.import_specs.items()}

    # Compute module ID
    module_id = _compute_module_id(main_content, imports_content)

    # Store module in _renderer_modules (content-addressed storage)
    _renderer_modules[module_id] = RendererModule(main=main_content, imports=imports_content)

    # Update cache
    registration._cached_module_id = module_id
    registration._cached_mtimes = _collect_mtimes(registration)

    return module_id
```

**`get_renderer_mapping()` changes:**

Instead of returning a pre-computed mapping, compute it on demand:

```python
def get_renderer_mapping(thing_type: str) -> RendererMapping | None:
    """Get the current renderer mapping for a thing type.

    This checks for file changes and returns an up-to-date module ID.
    """
    entry = _thing_type_registrations.get(thing_type)
    if not entry:
        return None

    registration, renderer_key = entry
    module_id = _get_current_module_id(registration)
    return RendererMapping(module_id=module_id, renderer_key=renderer_key)
```

### Part 2: Frontend - Refresh Button

The frontend accesses renderer configs through hooks in the `hooks/` folder. We'll add a refresh mechanism that allows any hook to respond to a "refresh renderers" signal.

**New hook: `useRendererRefresh.ts`**

Create a context + hooks using an event listener pattern:

```tsx
// Context provides subscribe/unsubscribe and trigger
const RendererRefreshContext = createContext<{
  subscribe: (callback: () => void) => void;
  unsubscribe: (callback: () => void) => void;
  triggerRefresh: () => void;
} | null>(null);

// Provider wraps the app
export function RendererRefreshProvider({ children }: { children: React.ReactNode }) {
  // Use ref to hold the Set of callbacks (stable across renders)
  const listenersRef = useRef(new Set<() => void>());

  const subscribe = useCallback((callback: () => void) => {
    listenersRef.current.add(callback);
  }, []);

  const unsubscribe = useCallback((callback: () => void) => {
    listenersRef.current.delete(callback);
  }, []);

  const triggerRefresh = useCallback(() => {
    for (const callback of listenersRef.current) {
      callback();
    }
  }, []);

  const value = useMemo(
    () => ({ subscribe, unsubscribe, triggerRefresh }),
    [subscribe, unsubscribe, triggerRefresh]
  );

  return (
    <RendererRefreshContext.Provider value={value}>
      {children}
    </RendererRefreshContext.Provider>
  );
}

// Hook to get the trigger function (for the refresh button)
export function useRendererRefreshTrigger() {
  const ctx = useContext(RendererRefreshContext);
  if (!ctx) throw new Error('useRendererRefreshTrigger requires RendererRefreshProvider');
  return ctx.triggerRefresh;
}

// Hook to register a callback when refresh is triggered
export function useOnRendererRefresh(callback: () => void) {
  const ctx = useContext(RendererRefreshContext);

  useEffect(() => {
    if (!ctx) return;
    ctx.subscribe(callback);
    return () => ctx.unsubscribe(callback);
  }, [ctx, callback]);
}
```

**Update `useThingTypeConfig` in useRenderingConfig.ts:**

Use `cache-and-network` policy so it refetches on re-render, and listen for refresh signals:

```tsx
export function useThingTypeConfig(thingType: string): UseThingTypeConfigResult {
  const [result, reexecute] = useQuery({
    query: GET_THING_TYPE_CONFIG,
    variables: { thingType },
    pause: !thingType,
    requestPolicy: 'cache-and-network',  // Always refetch, but show cached first
  });

  // Re-execute query when refresh is triggered
  useOnRendererRefresh(() => {
    reexecute({ requestPolicy: 'network-only' });
  });

  return {
    config: (result.data?.thingTypeConfig as ThingTypeConfig | undefined) ?? null,
    loading: result.fetching,
    error: result.error ?? null,
  };
}
```

Similarly update `useColorMapsForGraphOnly`.

**Timeline.tsx changes:**

Add a "Refresh" button next to the existing controls:

```tsx
const triggerRefresh = useRendererRefreshTrigger();

// In JSX, next to Capture Tick button:
<button
  className={styles.refreshButton}
  onClick={triggerRefresh}
  title="Refresh custom renderers (reload from disk)"
>
  ↻
</button>
```

**EmbedApp.tsx changes:**

Add the same refresh button in its controls area:

```tsx
const triggerRefresh = useRendererRefreshTrigger();

// In the header/controls area:
<button onClick={triggerRefresh} title="Refresh renderers">↻</button>
```

**Provider setup:**

Wrap both App and EmbedApp with `RendererRefreshProvider` (in main.tsx and embed.tsx, or inside the components).

## Implementation Steps

### Backend

- [x] 1. Add `RendererSourceSpec` and `RendererRegistration` dataclasses to registry.py
- [x] 2. Add `_thing_type_registrations` registry (thing_type -> RendererRegistration)
- [x] 3. Add `_read_spec()` helper to read content from a RendererSourceSpec
- [x] 4. Add `_get_current_module_id()` with mtime checking logic
- [x] 5. Update `register_renderer()` to accept Path objects and create registrations
- [x] 6. Update `get_renderer_mapping()` to use lazy evaluation via registrations
- [x] 7. Update `get_all_renderer_mappings()` similarly
- [x] 8. Update `clear_global_registries()` to clear new registry
- [x] 9. Add tests for path-based registration and reload detection

### Frontend

- [x] 10. Create `hooks/useRendererRefresh.tsx` with context, provider, and hooks
- [x] 11. Export from hooks/index.ts
- [x] 12. Update `useThingTypeConfig` to use `cache-and-network` and listen for refresh
- [x] 13. Update `useColorMapsForGraphOnly` similarly
- [x] 14. Wrap App with `RendererRefreshProvider` (in App.tsx)
- [x] 15. Add refresh button to Timeline component
- [x] 16. Style the refresh button
- [x] 17. Wrap EmbedApp with `RendererRefreshProvider` (in embed.tsx)
- [x] 18. Add refresh button to EmbedApp
- [x] 19. Test refresh flow end-to-end

### Update CODE_OVERVIEW.md

- [x] 20. Update CODE_OVERVIEW.md with new functionality

## Testing Strategy

1. **Unit test for path registration:**
   - Register renderer with Path
   - Verify module is loaded correctly
   - Modify file on disk
   - Verify `get_renderer_mapping()` returns new module ID

2. **Integration test:**
   - Start server with path-based renderer
   - Query thingTypeConfig, note moduleId
   - Modify renderer file
   - Query thingTypeConfig again, verify new moduleId
   - Query rendererModule with new ID, verify new content

3. **Frontend manual test:**
   - Load page with custom renderer
   - Edit renderer file
   - Click refresh button
   - Verify new renderer is used

## Notes

- Mtime-based change detection is fast but not 100% reliable (files could change within same second). For development workflow this is fine.
- Alternative: could use content hash comparison, but that requires reading files on every check. Mtime is a good balance.
- The refresh button is manual - we don't auto-poll for changes. This keeps things simple and predictable.
