# Phase 2 Step 18: Multi-Renderer Bundle System

## Overview

Change the custom renderer system to support multiple renderers exported from a single built package. Currently, each thing type requires its own renderer file registered individually. This refactor allows a single bundle to export renderers for multiple thing types, identified by renderer keys within the bundle.

Key changes:
1. **New export format**: Renderers export a config object instead of individual named exports
2. **Content-addressed module IDs**: Renderer bundles identified by hash of their contents
3. **Separate GraphQL query**: Fetch renderer modules by ID, cache by module not thing type
4. **Updated registration API**: Register multiple thing types to a single bundle

This is a breaking change to the renderer format. There are no external users.

## New Renderer Export Format

**Before (current):**
```tsx
export const thingwatcherDialect = "0.1";
export function FullRenderer(props: FullRendererProps) { ... }
export function RefRenderer(props: RefRendererProps) { ... }
export function InlineRefRenderer(props: RefRendererProps) { ... }
export function PreviewRenderer(props: PreviewRendererProps) { ... }
```

**After (new):**
```tsx
import type { ThingWatcherRenderConfig } from 'thingwatcher-bindings';

function ContainerFull(props: FullRendererProps) { ... }
function ContainerRef(props: RefRendererProps) { ... }
function ItemFull(props: FullRendererProps) { ... }
function ItemRef(props: RefRendererProps) { ... }

export default {
  dialect: "0.1",
  renderers: {
    "Container": { full: ContainerFull, ref: ContainerRef },
    "Item": { full: ItemFull, ref: ItemRef },
  }
} as ThingWatcherRenderConfig;
```

For single-type renderers:
```tsx
import type { ThingWatcherRenderConfig } from 'thingwatcher-bindings';

function WidgetFull(props: FullRendererProps) { ... }
function WidgetRef(props: RefRendererProps) { ... }

export default {
  dialect: "0.1",
  renderers: {
    "Widget": { full: WidgetFull, ref: WidgetRef },
  }
} as ThingWatcherRenderConfig;
```

## Python API Changes

### Registration API

```python
def register_renderer(
    thing_types: str | Mapping[str, str] | Sequence[str],
    tsx_source: str,
    imports: Mapping[str, str] | None = None,
) -> str:
    """Register a renderer bundle for one or more thing types.

    Args:
        thing_types: One of:
            - Single string: one thing type (renderer_key = thing_type)
            - Sequence of strings: multiple thing types (renderer_key = thing_type for each)
            - Mapping from thing_type to renderer_key (for when they differ)
        tsx_source: Main TSX source code exporting ThingWatcherRenderConfig
        imports: Optional dict of additional module sources

    Returns:
        The content-addressed module ID (for debugging/inspection)
    """
```

**Usage examples:**

```python
# Single type (most common case)
register_renderer("Widget", load_file("Widget.tsx"))

# Multiple types in one bundle (identity mapping)
register_renderer(
    ["Container", "Item"],
    load_file("container_item.tsx"),
)

# Multiple types with explicit renderer keys (when they differ from thing type)
register_renderer(
    {"MyContainer": "Container", "MyItem": "Item"},
    load_file("container_item.tsx"),
)

# With imports
register_renderer(
    "StyledThing",
    load_file("StyledThing.tsx"),
    imports={"./styles.module.css": load_file("styles.module.css")},
)
```

This is a breaking change - the old named-exports format is no longer supported. All demos will be updated.

### Internal Storage

```python
@dataclass
class RendererModule:
    """A renderer module with its source bundle."""
    main: str
    imports: dict[str, str]

@dataclass
class RendererMapping:
    """Maps a thing type to a renderer within a module."""
    module_id: str
    renderer_key: str

# Module ID -> source bundle
_renderer_modules: dict[str, RendererModule] = {}

# Thing type -> renderer mapping
_thing_renderer_mappings: dict[str, RendererMapping] = {}
```

### Content-Addressed Module IDs

Generate module ID from content hash using CBOR canonical serialization:

```python
import base64
import hashlib
import cbor2

def _compute_module_id(main: str, imports: dict[str, str]) -> str:
    """Compute content-addressed module ID using CBOR canonical form."""
    data = cbor2.dumps({"main": main, "imports": imports}, canonical=True)
    digest = hashlib.sha256(data).digest()
    # Base32 encode, lowercase, no padding, truncate to 16 chars
    return base64.b32encode(digest).decode().lower().rstrip("=")[:16]
```

Using CBOR canonical mode ensures deterministic serialization regardless of dict ordering.

## GraphQL Schema Changes

### New Types

```graphql
type RendererReference {
  moduleId: String!
  rendererKey: String!
}

type RendererModule {
  id: String!
  main: String!
  imports: JSON!
}
```

### Updated ThingTypeConfig

```graphql
type ThingTypeConfig {
  thingType: String!
  color: String!
  linkColors: [LinkColorConfig!]!
  renderer: RendererReference  # Changed from JSON
}
```

### New Query

```graphql
type Query {
  # ... existing queries ...

  # Fetch a renderer module by ID
  rendererModule(id: String!): RendererModule
}
```

## Frontend Changes

### Type Definitions (`thingwatcher-bindings.d.ts`)

Add new types for the config format:

```ts
/**
 * Renderer set for a single thing type within a module.
 */
export interface RendererSet {
  full?: FC<FullRendererProps>;
  ref?: FC<RefRendererProps>;
  inline?: FC<RefRendererProps>;
  preview?: FC<PreviewRendererProps>;
}

/**
 * The default export format for renderer modules.
 * All renderer modules must export this as their default export.
 */
export interface ThingWatcherRenderConfig {
  dialect: "0.1";
  renderers: Record<string, RendererSet>;
}
```

### Renderer Cache (`rendererCache.ts`)

Change caching strategy:

**Before:** Cache by `thingType + bundleHash`
**After:** Cache by `moduleId` only (module ID already incorporates content hash)

```ts
// Cache: moduleId -> CompiledModule
const moduleCache = new Map<string, CompiledModule>();

export async function getRendererForType(
  moduleId: string,
  rendererKey: string,
  bundle: RendererBundle
): Promise<{ renderers: CompiledRendererSet; stylesheet: CSSStyleSheet | null }> {
  // 1. Check cache by moduleId
  // 2. If miss, compile entire module (produces shared stylesheet + renderer map)
  // 3. Extract rendererKey from compiled module
  // 4. Return specific renderer set + shared stylesheet
}
```

The stylesheet is returned alongside the renderer set so the shadow DOM wrapper can attach it.

### Compilation (`esbuildSetup.ts`)

Update to handle new format:

```ts
interface CompiledModule {
  stylesheet: CSSStyleSheet | null;  // Hoisted: shared across all renderers in module
  renderers: Map<string, CompiledRendererSet>;
}

interface CompiledRendererSet {
  full: FC<FullRendererProps> | null;
  ref: FC<RefRendererProps> | null;
  inline: FC<RefRendererProps> | null;
  preview: FC<PreviewRendererProps> | null;
  // No stylesheet here - it's at module level
}

async function compileModule(bundle: RendererBundle): Promise<CompiledModule> {
  // 1. Compile with esbuild (produces JS + CSS)
  // 2. Create CSSStyleSheet from compiled CSS (once per module)
  // 3. Execute JS to get default export
  // 4. Validate dialect
  // 5. Extract all renderer sets from config.renderers
  // 6. Return module with shared stylesheet + renderer map
}
```

The stylesheet is hoisted to module level since CSS modules are compiled once for the entire bundle. All renderers in the module share the same stylesheet, which gets attached to each shadow root that uses any renderer from this module.

### GraphQL Queries (`queries.ts`)

Add new query:

```ts
export const GET_RENDERER_MODULE = gql`
  query GetRendererModule($id: String!) {
    rendererModule(id: $id) {
      id
      main
      imports
    }
  }
`;
```

### Dynamic Renderer Components

Update `DynamicRenderer.tsx` to:
1. Get `moduleId` and `rendererKey` from thing type config
2. Fetch module by ID (with caching)
3. Extract specific renderer by key

```tsx
function useDynamicRenderer(thingType: string) {
  const config = useThingTypeConfig(thingType);

  // If no renderer configured, use fallback
  if (!config?.renderer) {
    return { renderers: null, loading: false };
  }

  const { moduleId, rendererKey } = config.renderer;

  // Fetch and compile module
  const { module, loading, error } = useRendererModule(moduleId);

  if (loading || error || !module) {
    return { renderers: null, loading, error };
  }

  // Extract renderers for this key
  const renderers = module.renderers.get(rendererKey);
  return { renderers, loading: false };
}
```

## Demo Updates

### demo_dynamic_rendering

This demo tests various rendering setups, so it needs special handling:

1. **Container + Item**: Combine into single bundle with two renderer keys
2. **Broken* types**: Each stays as separate single-type bundle
3. **Styled* types**: Each stays separate to test CSS module scoping
4. **External/Bare imports**: Each stays separate to test import handling

**New structure:**
```
demo_dynamic_rendering/
├── demo.py
├── container_item.tsx      # NEW: Combined Container + Item renderers
├── BrokenCompile.tsx       # Single-type, new format
├── BrokenRuntime.tsx       # Single-type, new format
├── BrokenDialect.tsx       # Single-type, new format (for testing)
├── BrokenImport.tsx        # Single-type, new format
├── StyledThing.tsx         # Single-type, new format
├── StyledThing.module.css
├── StyledThingLib.tsx
├── ExternalImport.tsx      # Single-type, new format
├── BareImport.tsx          # Single-type, new format
└── deno.json
```

**demo.py changes:**
```python
# Combined Container + Item bundle
register_renderer(
    ["Container", "Item"],
    load_renderer("container_item"),
)

# Individual broken/styled/import tests (single string form)
register_renderer("BrokenCompile", load_renderer("BrokenCompile"))
register_renderer("BrokenRuntime", load_renderer("BrokenRuntime"))
# ... etc
```

### demo_embed

Single type (Widget), keep one file but update format:

**Widget.tsx:**
```tsx
import type { ThingWatcherRenderConfig, ... } from 'thingwatcher-bindings';

function WidgetFull(...) { ... }
function WidgetRef(...) { ... }

export default {
  dialect: "0.1",
  renderers: { "Widget": { full: WidgetFull, ref: WidgetRef } }
} as ThingWatcherRenderConfig;
```

**demo.py:**
```python
register_renderer("Widget", load_renderer("Widget"))
```

### demo_multi_chunk

Similar to demo_embed - single type, update format.

### demo_pause_trigger

If it has renderers, update similarly.

## Implementation Steps

### Phase 1: Type Definitions and Documentation

- [x] 1.1 Add `ThingWatcherRenderConfig` and `RendererSet` types to `thingwatcher-bindings.d.ts`
- [x] 1.2 Update documentation in `thingwatcher-bindings.d.ts` explaining new format

### Phase 2: Python Backend

- [x] 2.1 Add new data classes to `registry.py`: `RendererModule`, `RendererMapping`
- [x] 2.2 Add content-hash function for module IDs (using cbor2 canonical mode)
- [x] 2.3 Update `register_renderer()` to accept new signature: `str | Mapping | Sequence`
- [x] 2.4 Add getter functions: `get_renderer_module(id)`, `get_renderer_mapping(thing_type)`
- [x] 2.5 Update internal storage to use new data structures

### Phase 3: GraphQL Schema and Server

- [x] 3.1 Add `RendererReference` and `RendererModule` types to `schema.graphql`
- [x] 3.2 Update `ThingTypeConfig.renderer` field type
- [x] 3.3 Add `rendererModule(id)` query to schema
- [x] 3.4 Update `server.py` resolvers for new types
- [x] 3.5 Add resolver for `rendererModule` query

### Phase 4: Frontend Compilation

- [x] 4.1 Update `esbuildSetup.ts` to handle new export format (default export with dialect + renderers)
- [x] 4.2 Update `CompiledRenderers` type to hold map of renderer sets by key
- [x] 4.3 Update `rendererCache.ts` to cache by module ID

### Phase 5: Frontend Data Fetching

- [x] 5.1 Add `GET_RENDERER_MODULE` query to `queries.ts`
- [x] 5.2 Add `useRendererModule(moduleId)` hook (or inline in DynamicRenderer)
- [x] 5.3 Update `DynamicRenderer.tsx` to use new module fetching/caching

### Phase 6: Demo Updates

- [x] 6.1 Create `container_item.tsx` combining Container + Item renderers
- [x] 6.2 Update all other demo_dynamic_rendering TSX files to new export format
- [x] 6.3 Update `demo_dynamic_rendering/demo.py` to use new `register_renderer()` signature
- [x] 6.4 Update demo_embed TSX and demo.py
- [x] 6.5 Update demo_multi_chunk TSX and demo.py (if applicable)
- [x] 6.6 Update demo_pause_trigger TSX and demo.py (if applicable)
- [ ] 6.7 Test all demos work correctly

### Phase 7: Documentation

- [x] 7.1 Update CODE_OVERVIEW.md with new renderer format
- [x] 7.2 Update frontend/LOCAL_CODE_OVERVIEW.md
- [x] 7.3 Update demo README files if needed (skipped - no changes needed)

### Phase 8: Clydra

- [x] 8.1 Update clydra renderers to new format (kept individual files, added index.tsx wrapper)
- [x] 8.2 Create index.tsx that imports from individual files and exports combined config

## Notes

- Module IDs are opaque to the frontend - just strings used for caching and fetching
- Content-addressing (via CBOR canonical hash) means registering the same bundle twice reuses the same module ID
- The compiled module is cached once, then individual renderer sets are extracted by key
- CSS stylesheet is hoisted to module level - compiled once and shared across all renderers in the module
- When rendering a thing, the shared stylesheet is attached to that thing's shadow root

## Testing Considerations

- Test that same bundle registered for different types shares compilation
- Test that different bundles get different module IDs
- Test fallback behavior when renderer key not found in module
- Test CSS module scoping still works with combined bundles
- Test error display for:
  - Missing dialect
  - Wrong dialect version
  - Missing renderer key
  - Compile errors
  - Runtime errors
