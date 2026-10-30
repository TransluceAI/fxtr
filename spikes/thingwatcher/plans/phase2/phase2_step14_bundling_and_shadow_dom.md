# Phase 2 Step 14: Custom Renderer Bundling and Shadow DOM

This plan covers four related features for custom renderers:
1. Multi-file bundling (allow renderers to import submodules)
2. Shadow DOM isolation for custom renderer content
3. CSS modules support with adoptedStyleSheets
4. Demo demonstrating scoped styles in nested renderers

## Background

Currently, custom renderers are single TSX files compiled by esbuild-wasm in the browser. They can only import from `react`, `react/jsx-runtime`, and `thingwatcher-bindings`. This limits reusability and prevents using CSS modules.

The goal is to:
- Allow renderers to import additional modules (TSX, CSS) bundled together
- Wrap renderer output in shadow DOM for style isolation
- Use slots to allow thingwatcher-provided components (NodeRef, InlineNodeRef, etc.) to render outside the shadow DOM
- Support CSS modules with styles scoped to each renderer's shadow root

## 1. Multi-file Bundling

### API Design

Extend `register_renderer()` to accept an optional `imports` dict:

```python
# Current API (still works):
register_renderer("MyType", main_tsx_source)

# Extended API with imports:
register_renderer(
    "MyType",
    main_tsx_source,
    imports={
        "./utils": utils_tsx_source,          # TSX module
        "./styles.module.css": css_source,    # CSS module
    }
)
```

The main module can then import these:

```tsx
// MyType.tsx
import { formatData } from './utils.tsx';  // Exact extension required (matches Deno)
import styles from './styles.module.css';

export function FullRenderer({ payload }) {
  return <div className={styles.container}>{formatData(payload)}</div>;
}
```

### Python Changes

**registry.py:**
- Change `_renderers` from `dict[str, str]` to `dict[str, RendererBundle]`
- `RendererBundle` is a dataclass with `main: str` and `imports: dict[str, str]`
- `register_renderer()` accepts optional `imports` parameter
- `get_renderer()` returns the bundle (or wraps legacy string-only registrations)

```python
@dataclass
class RendererBundle:
    main: str  # Main TSX source
    imports: dict[str, str]  # Module name -> source code

def register_renderer(
    thing_type: str,
    tsx_source: str,
    imports: dict[str, str] | None = None
) -> None:
    _renderers[thing_type] = RendererBundle(
        main=tsx_source,
        imports=imports or {}
    )
```

### GraphQL Schema Changes

**schema.graphql:**
- Change `ThingTypeConfig.renderer` from `String` to `JSON`
- The JSON value will be `{ main: string, imports: { [name: string]: string } }` or `null`

```graphql
type ThingTypeConfig {
  thingType: String!
  color: String!
  linkColors: [LinkColorConfig!]!
  renderer: JSON  # { main: string, imports: { [name]: string } } or null
}
```

**server.py:**
- Update resolver to return the bundle structure

### Frontend Changes

**esbuildSetup.ts:**
- `compileRenderer()` now takes a `RendererBundle` (main + imports dict)
- Create a virtual filesystem plugin that resolves relative imports from the imports dict
- CSS files (`.css` or `.module.css`) are handled specially (see section 3)

**rendererCache.ts:**
- Cache key now includes hash of all sources (main + imports)
- Cache entry includes compiled JS module + compiled CSS stylesheet

## 2. Shadow DOM Isolation

### Approach

Based on the sketch in `/Users/daniel/tmp/portaldemo.html`:

1. Each DynamicRenderer wraps its content in a `ShadowRootDiv` component
2. The shadow root isolates styles from the parent document
3. Components from thingwatcher-bindings (NodeRef, InlineNodeRef, ChildNodeView, DraggableIDMarker) are "slotted" out to the light DOM so they can interact with the app's context and styles

### New Components (lib/)

**ShadowDOM.tsx:**

```tsx
// Context for shadow host element reference
const ShadowHostContext = createContext<HTMLElement | null>(null);

// Slot component - renders children in light DOM with matching slot
function ShadowSlot({ children }: { children: ReactNode }) {
  const slotId = useId();
  const hostElement = useContext(ShadowHostContext);

  return (
    <>
      <slot name={slotId} />
      {hostElement && createPortal(
        <ShadowHostContext.Provider value={null}>
          <span slot={slotId} style={{ display: 'contents' }}>{children}</span>
        </ShadowHostContext.Provider>,
        hostElement
      )}
    </>
  );
}

// Shadow root container
interface ShadowRootDivProps {
  children: ReactNode;
  stylesheets?: CSSStyleSheet[];  // For adoptedStyleSheets
}

function ShadowRootDiv({ children, stylesheets }: ShadowRootDivProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [shadowRoot, setShadowRoot] = useState<ShadowRoot | null>(null);

  useEffect(() => {
    if (hostRef.current && !hostRef.current.shadowRoot) {
      const shadow = hostRef.current.attachShadow({ mode: 'open' });
      setShadowRoot(shadow);

      if (stylesheets?.length) {
        shadow.adoptedStyleSheets = stylesheets;
      }
    }
  }, []);

  // Update stylesheets if they change
  useEffect(() => {
    if (shadowRoot && stylesheets) {
      shadowRoot.adoptedStyleSheets = stylesheets;
    }
  }, [shadowRoot, stylesheets]);

  return (
    <ShadowHostContext.Provider value={hostRef.current}>
      <div ref={hostRef}>
        {shadowRoot && createPortal(children, shadowRoot)}
      </div>
    </ShadowHostContext.Provider>
  );
}
```

### Wrapping thingwatcher-bindings Components

The challenge: when a custom renderer uses `<NodeRef thingKey={...} />`, the NodeRef needs to render outside the shadow DOM so it can:
- Inherit app styles (like hover effects)
- Participate in the app's context (selection, panel opening)
- Show hover preview cards (which need to be outside the shadow DOM)

**Solution:** Maintain strict separation between internal and external component versions.

**File structure:**

```
lib/
├── ThingRenderLib.tsx              # Internal implementations (used by our app)
│   ├── NodeRef
│   ├── InlineNodeRef
│   ├── ChildNodeView
│   ├── usePersistentThingState
│   └── (prop types exported here)
│
└── ThingWatcherBindingsExternal.tsx  # External wrappers (ONLY for dynamic renderers)
    ├── NodeRef        (imports from ThingRenderLib, wraps in ShadowSlot)
    ├── InlineNodeRef  (imports from ThingRenderLib, wraps in ShadowSlot)
    ├── ChildNodeView  (imports from ThingRenderLib, wraps in ShadowSlot)
    └── (re-exports prop types and usePersistentThingState from ThingRenderLib)
```

**ThingRenderLib.tsx:**
- Contains the actual implementations of `NodeRef`, `InlineNodeRef`, `ChildNodeView`
- Used directly by our own app code (FallbackRenderers, RootsPanel, ThingsList, HoverPreview, etc.)
- Exports prop types (`FullRendererProps`, `RefRendererProps`, etc.)
- Never wraps in ShadowSlot

**ThingWatcherBindingsExternal.tsx:**
- ONLY used by dynamic renderers (injected via esbuild as `thingwatcher-bindings`)
- Imports components from `ThingRenderLib` and wraps them in `ShadowSlot`
- Re-exports prop types and `usePersistentThingState` from `ThingRenderLib`
- Never imported by our own app code

```tsx
// ThingWatcherBindingsExternal.tsx
import {
  NodeRef as NodeRefInternal,
  InlineNodeRef as InlineNodeRefInternal,
  ChildNodeView as ChildNodeViewInternal,
  usePersistentThingState,
} from './ThingRenderLib';
import { ShadowSlot } from './ShadowDOM';

// Re-export types
export type { FullRendererProps, RefRendererProps, PreviewRendererProps } from './ThingRenderLib';

// Re-export hook (no wrapping needed)
export { usePersistentThingState };

// Wrapped versions for external use
export function NodeRef(props: NodeRefProps) {
  return (
    <ShadowSlot>
      <NodeRefInternal {...props} />
    </ShadowSlot>
  );
}

// Similar for InlineNodeRef, ChildNodeView...
```

**esbuildSetup.ts:**
- Inject `ThingWatcherBindingsExternal` as the `thingwatcher-bindings` module

## 3. CSS Modules Support

### esbuild Configuration

esbuild supports CSS modules via the `css: 'local'` option. However, esbuild-wasm has some limitations with CSS modules.

**Approach:**
1. When bundling, enable CSS support in esbuild
2. Intercept the CSS output file
3. Create a `CSSStyleSheet` object from the CSS text
4. Pass this stylesheet to the ShadowRootDiv for adoptedStyleSheets

**esbuildSetup.ts changes:**

```typescript
interface CompiledRenderers {
  full: React.ComponentType<FullRendererProps> | null;
  ref: React.ComponentType<RefRendererProps> | null;
  inline: React.ComponentType<RefRendererProps> | null;
  preview: React.ComponentType<PreviewRendererProps> | null;
  stylesheet: CSSStyleSheet | null;  // NEW
}

async function compileRenderer(bundle: RendererBundle): Promise<CompiledRenderers> {
  // ... existing setup ...

  const result = await esbuild.build({
    stdin: {
      contents: bundle.main,
      loader: 'tsx',
      sourcefile: 'main.tsx',  // Explicit name for output file naming
    },
    bundle: true,
    format: 'esm',
    write: false,
    outdir: 'out',  // Required for multiple outputs
    plugins: [
      createInjectedDepsPlugin(),
      createVirtualFsPlugin(bundle.imports),  // NEW
    ],
    // CSS modules configuration
    loader: {
      '.css': 'local-css',  // Enable CSS modules
    },
  });

  // Strict output file validation - expect exactly these files:
  // - out/main.js (always)
  // - out/main.css (only if CSS was imported)
  const expectedJs = 'out/main.js';
  const expectedCss = 'out/main.css';

  let jsCode: string | null = null;
  let cssCode: string | null = null;

  for (const file of result.outputFiles) {
    if (file.path.endsWith(expectedJs)) {
      jsCode = file.text;
    } else if (file.path.endsWith(expectedCss)) {
      cssCode = file.text;
    } else {
      // Unexpected file - treat as compilation error
      throw new Error(`Unexpected output file from esbuild: ${file.path}`);
    }
  }

  if (!jsCode) {
    throw new Error('esbuild did not produce expected JS output (out/main.js)');
  }

  // Create stylesheet from CSS
  let stylesheet: CSSStyleSheet | null = null;
  if (cssCode) {
    stylesheet = new CSSStyleSheet();
    stylesheet.replaceSync(cssCode);
  }

  // ... load JS module as before ...

  return {
    full: module.FullRenderer ?? null,
    ref: module.RefRenderer ?? null,
    inline: module.InlineRefRenderer ?? null,
    preview: module.PreviewRenderer ?? null,
    stylesheet,
  };
}
```

**Virtual Filesystem Plugin:**

```typescript
function createVirtualFsPlugin(imports: Record<string, string>): esbuild.Plugin {
  return {
    name: 'virtual-fs',
    setup(build) {
      // Resolve relative imports to virtual namespace
      build.onResolve({ filter: /^\./ }, (args) => {
        const resolved = args.path;  // e.g., './utils' or './styles.module.css'
        if (imports[resolved]) {
          return { path: resolved, namespace: 'virtual' };
        }
        return null;
      });

      // Load virtual modules
      build.onLoad({ filter: /.*/, namespace: 'virtual' }, (args) => {
        const source = imports[args.path];
        if (source) {
          const loader = args.path.endsWith('.css') ? 'local-css'
                       : args.path.endsWith('.tsx') ? 'tsx'
                       : 'ts';
          return { contents: source, loader };
        }
        return null;
      });
    },
  };
}
```

### Stylesheet Handling in DynamicRenderer

**DynamicRenderer.tsx:**

All four renderer types (Full, Ref, Inline, Preview) wrap their custom component in `ShadowRootDiv` when a custom renderer is available. Fallback renderers don't need shadow DOM since they use app styles.

```tsx
function DynamicFullRenderer({
  thingType,
  tsxCode,
  rendererProps,
  fallbackExtra,
  imports,  // NEW
}: DynamicFullRendererProps) {
  const { renderers, error, loading } = useCompiledRenderers(thingType, tsxCode, imports);

  // ... loading/error handling ...

  const FullRenderer = renderers?.full;
  if (FullRenderer) {
    return (
      <ShadowRootDiv stylesheets={renderers.stylesheet ? [renderers.stylesheet] : undefined}>
        <ErrorBoundary fallback={...}>
          <FullRenderer {...rendererProps} />
        </ErrorBoundary>
      </ShadowRootDiv>
    );
  }

  // Fallback (no shadow DOM needed for fallback)
  return <FallbackFullRenderer ... />;
}

// Similar pattern for DynamicRefRenderer, DynamicInlineRefRenderer, DynamicPreviewRenderer
// All wrap custom renderers in ShadowRootDiv for consistent style isolation
```

### Cache Memoization

**rendererCache.ts:**
- Cache key includes hash of main + all imports
- Cache stores `CompiledRenderers` which includes the `CSSStyleSheet`
- `CSSStyleSheet` objects are reused across instances (no need to recreate)

## 4. Demo: CSS Module Scoping

### New Files in examples/demo_dynamic_rendering/

A single TSX/CSS file pair is used for both thing types. Python-side string replacement creates the second variant with different colors. This cleanly demonstrates that the same code with different CSS produces isolated visual results.

**StyledThing.tsx:**
```tsx
import styles from './StyledThing.module.css';
import { ChildNodeView } from 'thingwatcher-bindings';
import type { FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

export function FullRenderer({ payload, links, DraggableIDMarker }: FullRendererProps) {
  const childLink = links.find(l => l.linkType === 'child');

  return (
    <div className={styles.container}>
      <DraggableIDMarker>
        <h3 className={styles.title}>{payload?.name}</h3>
      </DraggableIDMarker>
      <p className={styles.description}>{payload?.description}</p>
      {childLink && (
        <div className={styles.childContainer}>
          <ChildNodeView thingKey={childLink.toThingKey} />
        </div>
      )}
    </div>
  );
}

export function RefRenderer({ payload }: RefRendererProps) {
  return <span className={styles.ref}>{payload?.name}</span>;
}
```

**StyledThing.module.css:**
```css
.container {
  padding: 16px;
  background: #1a3a3a;
  border-radius: 8px;
}

.title {
  color: #4ecdc4;  /* Teal - will be replaced to #ff6b6b (red) for StyledTwo */
  margin: 0 0 8px 0;
}

.description {
  color: #aaa;
  margin: 0;
}

.childContainer {
  margin-top: 16px;
  border: 1px solid #4ecdc4;
  border-radius: 4px;
  overflow: hidden;
}

.ref {
  color: #4ecdc4;
}
```

**Python-side registration (in demo.py):**

```python
def load_css(name: str) -> str:
    """Load a CSS file from the same directory as this script."""
    path = SCRIPT_DIR / f"{name}.css"
    return path.read_text()

# Load the single TSX/CSS pair
styled_thing_tsx = load_renderer("StyledThing")
styled_thing_css = load_css("StyledThing.module")

# StyledOne: uses original teal color
register_renderer("StyledOne", styled_thing_tsx, imports={
    "./StyledThing.module.css": styled_thing_css,
})

# StyledTwo: uses modified red color (Python-side string replace)
styled_thing_css_red = styled_thing_css.replace("#4ecdc4", "#ff6b6b")
register_renderer("StyledTwo", styled_thing_tsx, imports={
    "./StyledThing.module.css": styled_thing_css_red,
})
```

### Demo Behavior

- StyledOne uses teal (#4ecdc4) styling
- StyledTwo uses red (#ff6b6b) styling (same TSX, different CSS via Python string replace)
- StyledOne renders StyledTwo as a child via ChildNodeView
- Even though both use the same class names (`.container`, `.title`, etc.), they don't conflict
- Each thing's shadow DOM keeps its styles isolated
- Expected visual: teal parent containing red child, demonstrating scoped CSS

## Implementation Steps

### Phase A: Multi-file Bundling (Backend)
- [x] Update `registry.py`: add `RendererBundle` dataclass, update `_renderers` type
- [x] Update `register_renderer()` to accept `imports` dict
- [x] Update `get_renderer()` and `get_all_renderers()` to return bundle format
- [x] Update GraphQL schema: `ThingTypeConfig.renderer` becomes `JSON`
- [x] Update `server.py` resolver for renderingConfig

### Phase B: Multi-file Bundling (Frontend)
- [x] Update `esbuildSetup.ts`: add virtual filesystem plugin for imports
- [x] Update `compileRenderer()` to accept bundle structure
- [x] Update cache key computation to include imports

### Phase C: Shadow DOM Infrastructure
- [x] Create `lib/ShadowDOM.tsx` with `ShadowRootDiv`, `ShadowSlot`, `ShadowHostContext`
- [x] Rename `ThingWatcherBindings.tsx` to `ThingRenderLib.tsx` (keep component names as-is)
- [x] Create `ThingWatcherBindingsExternal.tsx`: imports from `ThingRenderLib`, wraps in `ShadowSlot`, re-exports prop types
- [x] Update `esbuildSetup.ts` to inject `ThingWatcherBindingsExternal` as `thingwatcher-bindings`
- [x] Update all app code to import from `ThingRenderLib` instead of `ThingWatcherBindings`
- [x] Update `DynamicFullRenderer` to wrap custom renderers in `ShadowRootDiv` (ErrorBoundary outside)
- [x] Update `DynamicRefRenderer` to wrap custom renderers in `ShadowRootDiv` (ErrorBoundary outside)
- [x] Update `DynamicInlineRefRenderer` to wrap custom renderers in `ShadowRootDiv` (ErrorBoundary outside)
- [x] Update `DynamicPreviewRenderer` to wrap custom renderers in `ShadowRootDiv` (ErrorBoundary outside)

### Phase D: CSS Modules Support
- [x] Update esbuild config: use `outdir`, `sourcefile: 'main.tsx'` for predictable output names
- [x] Update esbuild config to handle `.css` files with `local-css` loader
- [x] Add strict output validation: expect only `out/main.js` and optionally `out/main.css`
- [x] Error on unexpected output files (treat as compilation failure)
- [x] Extract CSS from build output and create `CSSStyleSheet`
- [x] Pass stylesheet to `ShadowRootDiv` via `adoptedStyleSheets`
- [x] Update `CompiledRenderers` type to include `stylesheet`
- [x] Update cache to store and retrieve stylesheets

### Phase E: Demo
- [x] Create `StyledThing.tsx` (single TSX file for both thing types)
- [x] Create `StyledThing.module.css` (with teal color that Python replaces)
- [x] Add helper `load_css()` function in demo.py
- [x] Register `StyledOne` with original CSS (teal)
- [x] Register `StyledTwo` with Python-modified CSS (teal -> red via string replace)
- [x] Add demo objects where StyledOne renders StyledTwo as a child
- [x] Verify style isolation in nested renderers (teal parent, red child)

### Phase F: Cleanup
- [x] Update `thingwatcher/thingwatcher-bindings.ts` type definitions for Deno (must match ThingWatcherBindingsExternal exports)
- [x] Update demo_dynamic_rendering README.md to document CSS modules usage
- [x] Update frontend LOCAL_CODE_OVERVIEW.md with new file structure
- [x] Update CODE_OVERVIEW.md

## Testing Plan

1. **Multi-file bundling**: Register a renderer with an imports dict, verify it can import from those modules
2. **Shadow DOM isolation**: Verify custom renderer styles don't affect app styles and vice versa
3. **Slot behavior**: Verify NodeRef/InlineNodeRef/ChildNodeView work correctly inside shadow DOM
4. **CSS modules**: Verify class names are scoped and don't conflict between renderers
5. **Nested scoping**: Verify parent and child renderers maintain separate style scopes
6. **Error handling**: Verify compilation errors in imported modules are displayed correctly
7. **Caching**: Verify renderers with identical source but different imports are cached separately
8. **Drag-drop across shadow DOM**: Verify DraggableIDMarker drag events work correctly from inside shadow DOM to dockview drop targets
9. **Hover previews**: Verify NodeRef/InlineNodeRef hover previews appear correctly when slotted out of shadow DOM
10. **Exact extension paths**: Verify imports without extensions fail with clear error message
11. **Unexpected output files**: Verify esbuild producing unexpected files results in compilation error
12. **Internal/external separation**: Verify app code only uses ThingRenderLib, never ThingWatcherBindingsExternal

## Design Decisions

1. **All renderer types get shadow DOM**: Full, Ref, Inline, and Preview renderers all get wrapped in shadow DOM for consistent style isolation.

2. **Exact import paths required**: Import paths must include extensions (e.g., `./utils.tsx` not `./utils`). This matches Deno's behavior and avoids custom resolution heuristics.

3. **Full style isolation**: No parent styles leak into shadow DOM. Renderers control all their own styling.

4. **DraggableIDMarker stays in shadow DOM**: It wraps user content that should be styled by shadow CSS. Drag events work across shadow boundary.

## Notes

- The portaldemo.html sketch provides the core pattern for shadow DOM + slots
- esbuild-wasm has good CSS modules support via `local-css` loader
- `adoptedStyleSheets` is well-supported in modern browsers and efficient for reuse
- Import paths require explicit extensions (e.g., `./utils.tsx` not `./utils`) to match Deno conventions
- All four renderer types (Full, Ref, Inline, Preview) get shadow DOM for consistency
- Strict separation: `ThingRenderLib` for internal use, `ThingWatcherBindingsExternal` only for dynamic renderers
- Strict esbuild output validation: only accept expected files (`out/main.js`, `out/main.css`)
