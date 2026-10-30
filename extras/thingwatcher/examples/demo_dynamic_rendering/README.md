# Dynamic Rendering Demo

This demo tests all of thingwatcher's dynamic rendering features:

1. **ChildNodeView** - Recursive rendering of things inline within other things
2. **NodeRef / InlineNodeRef** - Clickable references to other things (block and inline styles)
3. **Compile error display** - ErrorBoundary catches and displays TSX syntax errors
4. **Runtime error display** - ErrorBoundary catches errors thrown during render
5. **JSON fallback** - Things without custom renderers display as formatted JSON
6. **Named exports** - FullRenderer, RefRenderer, InlineRefRenderer, PreviewRenderer
7. **CSS Modules + Shadow DOM** - Style isolation between nested renderers (StyledOne/StyledTwo)

## Running the demo

```bash
python demo.py
```

Then open http://localhost:8765 and click on each root in the left panel to see the different behaviors.

## Project structure

```
demo.py                   # Python demo server - reads .tsx files at runtime
deno.json                 # Deno config for development-time typechecking
Container.tsx             # Renderer for Container type
Item.tsx                  # Renderer for Item type
BrokenCompile.tsx         # Intentionally broken TSX (syntax error)
BrokenRuntime.tsx         # Valid TSX that throws during render
StyledThing.tsx           # CSS modules demo - used for StyledOne and StyledTwo
StyledThing.module.css    # CSS module - Python replaces colors per type
```

## TypeScript typechecking with Deno

The `.tsx` renderer files can be typechecked using [Deno](https://deno.land/). This is **optional and only for development** - Deno is not required at runtime.

At runtime, Python reads the `.tsx` files as strings and passes them to esbuild-wasm running in the browser, which compiles them on the fly. The `thingwatcher-bindings` module is injected by esbuild as a virtual module.

For development, Deno provides:
- Type checking for your TSX code
- Editor support via the [Deno VSCode extension](https://docs.deno.com/runtime/reference/vscode/)
- Catching errors before runtime

### Editor setup

Install the [Deno extension for VSCode](https://docs.deno.com/runtime/reference/vscode/) to get inline type checking, autocomplete, and error highlighting for the `.tsx` files. The extension will automatically detect the `deno.json` and use it for configuration.

### Typechecking

```bash
# Check specific files
deno check Container.tsx Item.tsx

# Check all valid renderers
deno check Container.tsx Item.tsx BrokenRuntime.tsx
```

BrokenCompile.tsx will fail typechecking (intentionally) due to syntax errors.

### How it works

The `deno.json` configures an import map that resolves `thingwatcher-bindings` to the type definitions file at `../../thingwatcher/thingwatcher-bindings.ts`. This provides the same types that the runtime esbuild setup provides, enabling full type checking without needing the actual runtime environment.

In the future, the types file could be hosted at a URL, allowing any renderer project to reference it without needing a local copy of thingwatcher.

## CSS Modules and Shadow DOM

Custom renderers run inside shadow DOM for style isolation. You can import CSS modules which get compiled and attached to the shadow root.

### Using CSS modules

```tsx
import styles from './MyStyles.module.css';

export function FullRenderer({ payload }) {
  return <div className={styles.container}>{payload.name}</div>;
}
```

Register the renderer with its CSS imports:

```python
register_renderer("MyType", tsx_source, imports={
    "./MyStyles.module.css": css_source,
})
```

### Python-side customization

CSS can be modified at registration time using Python string operations. In the demo, `StyledOne` and `StyledTwo` use the same TSX but different CSS:

```python
# StyledOne uses original CSS (teal)
register_renderer("StyledOne", tsx_source, imports={
    "./StyledThing.module.css": css_source,
})

# StyledTwo uses modified CSS (teal -> red)
css_modified = css_source.replace("#4ecdc4", "#ff6b6b")
register_renderer("StyledTwo", tsx_source, imports={
    "./StyledThing.module.css": css_modified,
})
```

### Style isolation

Each custom renderer gets its own shadow DOM, so styles don't leak between nested renderers. In the demo, `StyledOne` (teal) renders `StyledTwo` (red) as a child - they use the same class names but display different colors.

Components like `NodeRef`, `InlineNodeRef`, and `ChildNodeView` are "slotted" out to light DOM, so they inherit the app's styles and can show hover preview cards correctly.
