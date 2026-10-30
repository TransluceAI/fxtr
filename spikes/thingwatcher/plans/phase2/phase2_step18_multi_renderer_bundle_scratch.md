# Step 18 Scratch Notes - Multi-Renderer Bundle

## Current State Analysis

### Python Side (`registry.py`)
- `_renderers: dict[str, RendererBundle]` - keyed by thing_type
- `RendererBundle` = `{ main: str, imports: dict[str, str] }`
- `register_renderer(thing_type: str, tsx_source: str, imports: dict | None)`

### GraphQL (`schema.graphql`)
- `ThingTypeConfig.renderer: JSON` returns full bundle

### Frontend
- `rendererCache.ts`: caches by `thingType + bundleHash`
- `esbuildSetup.ts`: compiles bundle, expects named exports
- `DynamicRenderer.tsx`: gets renderer from `useRenderingConfig`, compiles

### Current Renderer Format
```tsx
export const thingwatcherDialect = "0.1";
export function FullRenderer(props) { ... }
export function RefRenderer(props) { ... }
// etc
```

## New Design

### New Renderer Export Format
```tsx
export default {
  dialect: "0.1",
  renderers: {
    "Container": {
      full: ContainerFullRenderer,
      ref: ContainerRefRenderer,
      inline: ContainerInlineRefRenderer,
      preview: ContainerPreviewRenderer,
    },
    "Item": {
      full: ItemFullRenderer,
      ref: ItemRefRenderer,
      // optional: inline, preview
    },
  }
} as ThingWatcherRenderConfig;
```

### Content-Addressed Module IDs
- Hash the bundle contents (main + sorted imports)
- Use base32 encoding for opaque ID
- Allows deduplication if same bundle registered twice

### Python Storage
```python
# Module ID -> bundle source
_renderer_modules: dict[str, RendererModuleSource] = {}

# Thing type -> (module_id, renderer_key)
_thing_renderer_mappings: dict[str, RendererMapping] = {}
```

### New Python API
```python
def register_renderer(
    thing_types: str | Mapping[str, str] | Sequence[str],
    tsx_source: str,
    imports: Mapping[str, str] | None = None,
) -> str:  # returns module_id
```

- `str`: single thing type (most common case)
- `Sequence[str]`: multiple thing types, identity mapping (renderer_key = thing_type)
- `Mapping[str, str]`: explicit `{ thing_type: renderer_key }` when they differ

### GraphQL Changes

Add new types:
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

Change `ThingTypeConfig.renderer` from `JSON` to `RendererReference`

Add query:
```graphql
rendererModule(id: String!): RendererModule
```

### Frontend Changes

1. **rendererCache.ts**: Cache by module ID, not thing type
2. **esbuildSetup.ts**: Parse new format, validate dialect at module level
3. **DynamicRenderer.tsx**:
   - Fetch module by ID
   - Extract renderers by key
4. **New hook**: `useRendererModule(moduleId: string)` for fetching

### Type Definitions

Add to `thingwatcher-bindings.d.ts`:
```ts
export interface RendererSet {
  full?: FC<FullRendererProps>;
  ref?: FC<RefRendererProps>;
  inline?: FC<RefRendererProps>;
  preview?: FC<PreviewRendererProps>;
}

export interface ThingWatcherRenderConfig {
  dialect: "0.1";
  renderers: Record<string, RendererSet>;
}
```

## Demo Changes

### demo_dynamic_rendering
Keep multiple files (testing different setups), but:
- Combine Container + Item into one bundle with two renderer keys
- Each other file (BrokenCompile, etc.) exports singleton config
- Update demo.py to use new API

### Other demos (embed, multi_chunk, pause_trigger)
- Keep single file
- Change from named exports to default config export
- Update demo.py

## Migration Path

Since this is a breaking change with no other users:
1. Update type definitions first
2. Update frontend compilation/caching
3. Update Python registry + server
4. Update all demos
5. Update clydra (last)

## Hash Function for Module ID

Use CBOR canonical serialization + SHA-256:
```python
import cbor2, hashlib, base64

def _compute_module_id(main: str, imports: dict[str, str]) -> str:
    data = cbor2.dumps({"main": main, "imports": imports}, canonical=True)
    digest = hashlib.sha256(data).digest()
    return base64.b32encode(digest).decode().lower().rstrip("=")[:16]
```

Benefits:
- CBOR canonical mode ensures deterministic ordering
- Already using cbor2 in the project
- Base32 lowercase, 16 chars = 80 bits, collision-resistant enough
