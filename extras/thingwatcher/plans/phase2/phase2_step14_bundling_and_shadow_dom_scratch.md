# Scratch notes for Phase 2 Step 14

## Technical exploration

### esbuild CSS modules behavior

esbuild's CSS modules support (`local-css` loader) transforms class names to be unique:

```css
/* Input: styles.module.css */
.container { background: red; }

/* Output: */
.styles_container_abc123 { background: red; }
```

And the JS module gets:
```js
export var container = "styles_container_abc123";
```

This is exactly what we need - the class names become unique per-module.

### adoptedStyleSheets API

```js
const sheet = new CSSStyleSheet();
sheet.replaceSync(cssText);  // Synchronous
// or
await sheet.replace(cssText);  // Async

shadowRoot.adoptedStyleSheets = [sheet];
```

Benefits:
- Can reuse the same CSSStyleSheet across multiple shadow roots
- More efficient than injecting `<style>` elements
- Supports updates via `replaceSync`

### Shadow DOM slot mechanics

When we have:
```html
<div>  <!-- host -->
  #shadow-root
    <slot name="foo">
  <span slot="foo">content</span>  <!-- light DOM -->
</div>
```

The `<span>` is rendered where the `<slot>` appears in the shadow tree, but:
- It inherits styles from light DOM (the document)
- It's in the light DOM for event bubbling purposes

This is exactly what we want for NodeRef/etc - they should participate in the app's styling and event handling while being positioned within the custom renderer's layout.

### React portals and slots interaction

The portaldemo.html sketch shows the key insight:
1. `ShadowRootDiv` portals its children into the shadow root
2. `ShadowSlot` renders a `<slot>` in place (which ends up in shadow DOM due to parent portal)
3. `ShadowSlot` also portals its children to the host element as light DOM with matching `slot` attribute

This maintains React's component tree while achieving the DOM structure needed for slots.

### Component hierarchy

```
DynamicFullRenderer
└── ShadowRootDiv (creates shadow root, portals children into it)
    └── [Everything below is in shadow DOM]
        └── ErrorBoundary
            └── FullRenderer (user's component)
                ├── <div className={styles.foo}> (styled by shadow CSS)
                ├── NodeRef (shadow-aware, wraps in ShadowSlot)
                │   └── ShadowSlot
                │       ├── <slot name=":r1:"> (in shadow DOM)
                │       └── [portal to light DOM]
                │           └── <span slot=":r1:"> (in light DOM)
                │               └── actual NodeRef UI (app-styled)
                └── ChildNodeView (shadow-aware, wraps in ShadowSlot)
                    └── ShadowSlot
                        └── [another DynamicFullRenderer with its own shadow root]
```

### Where does the slot boundary go?

For ChildNodeView, the entire nested renderer (including its shadow root) should be in light DOM. So:

```tsx
function ChildNodeView({ thingKey }) {
  // If we're in a shadow DOM context, wrap in slot
  const hostElement = useContext(ShadowHostContext);

  const content = <LowLevelNodeView thingKey={thingKey} />;

  if (hostElement) {
    return <ShadowSlot>{content}</ShadowSlot>;
  }
  return content;
}
```

For NodeRef/InlineNodeRef, the entire ref UI should be in light DOM:

```tsx
function NodeRef({ thingKey }) {
  const hostElement = useContext(ShadowHostContext);

  const content = (
    <div className={styles.nodeRef}>
      {/* actual ref rendering */}
    </div>
  );

  if (hostElement) {
    return <ShadowSlot>{content}</ShadowSlot>;
  }
  return content;
}
```

### CSS variables for theme bridging?

If we ever need app theme to affect custom renderers (e.g., background color), we could:
1. Define CSS variables in app
2. Custom renderers use `var(--tw-background, #1a1a2e)` with fallback
3. CSS variables DO cross shadow boundary when set on host or ancestors

For now, full isolation is the requirement, so we won't implement this.

### Order of implementation

Suggested order:
1. Multi-file bundling (backend then frontend) - this is mostly additive
2. CSS modules support - builds on multi-file bundling
3. Shadow DOM - biggest change, affects multiple components
4. Demo - validates everything works together

Actually, better to do shadow DOM earlier since CSS modules need it for isolation. Revised:
1. Multi-file bundling backend
2. Shadow DOM infrastructure
3. Multi-file bundling frontend + CSS modules (together, since CSS needs shadow root)
4. Demo

### Error cases to handle

1. Import file doesn't exist (referenced in TSX but not in imports dict)
2. Circular imports
3. CSS syntax error
4. CSS file referenced but not in imports dict
5. Main TSX references a relative path that looks like CSS but is actually TSX

esbuild should handle most of these with reasonable error messages.

### Cache invalidation

Current cache key: `thingType + hash(mainSource)`

New cache key needs to include all sources:
```typescript
function computeCacheKey(bundle: RendererBundle): string {
  const parts = [bundle.main];
  for (const [name, source] of Object.entries(bundle.imports).sort()) {
    parts.push(`${name}:${source}`);
  }
  return hashString(parts.join('\n'));
}
```

### DraggableIDMarker considerations

DraggableIDMarker is passed as a prop to the renderer. It wraps user-provided content and adds:
- `draggable="true"` attribute
- `onDragStart` handler
- Styling

Since it wraps user content, it should stay in shadow DOM (the user's content is styled by shadow CSS). The drag events should still work because:
- `draggable` and `onDragStart` work fine in shadow DOM
- The drop target (dockview) is in light DOM and receives the drop

Actually, might need to verify drag/drop works across shadow boundary. Should test this early.

### Final decisions from user discussion

1. **All renderers get shadow DOM** - Full, Ref, Inline, Preview all wrapped in ShadowRootDiv for consistent style isolation.

2. **Exact extension paths required** - Import paths must include extensions (e.g., `./utils.tsx` not `./utils`). This matches Deno's behavior. No auto-resolution heuristics.

### Preview cards and shadow DOM

Hover preview cards are rendered by HoverPreview.tsx using base-ui's PreviewCard. These should NOT be in shadow DOM - they're app UI that appears on hover over NodeRef/InlineNodeRef.

Since NodeRef is slotted out to light DOM, its hover handlers work normally and preview cards appear in the document as expected.
