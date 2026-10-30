# @fxtr/mdx-transform — Code Overview

Custom MDX-to-FxtrCbor transformation library. Compiles MDX (Markdown with JSX) into serializable FxtrCbor tuple trees instead of React elements. Supports both build-time (Vite `jsxImportSource`) and runtime (dynamic `compile`+`run`) paths.

## How It Works

Two-stage pipeline:

1. **JSX Runtime** (`jsx-runtime.ts`): MDX compilation invokes the custom `jsx()`/`jsxs()` factories, which produce `MdxNode` objects instead of React elements. String element types become validated HTML nodes; function types are invoked as server-side components (can be async). All props are validated to be FxtrCbor-serializable.

2. **Flatten** (`flatten.ts`): Converts the `MdxNode` tree into FxtrCbor tuples of the form `[type, props, children]`. Resolves async component promises during flattening. `extractFragmentChildren()` unwraps the top-level MDX fragment to produce a document node array.

## Files

```
src/
├── index.ts           # Public API re-exports
├── jsx-runtime.ts     # Custom JSX factory → MdxNode tree (stage 1)
├── flatten.ts         # MdxNode → FxtrCbor tuples (stage 2)
├── evaluate.ts        # Runtime MDX evaluation: compile + run + flatten
└── render.ts          # Pre-compiled MDX rendering: content fn + flatten
```

- **jsx-runtime.ts**: Implements `jsx()`, `jsxs()`, `jsxDEV()`, `Fragment`. Also exports `passthroughCustomComponent()` for creating custom component wrappers that emit named nodes into the tree (consumed client-side).
- **flatten.ts**: `flatten(node)` recursively converts `MdxNode` → tuples. `extractFragmentChildren()` unwraps the root fragment.
- **evaluate.ts**: `evaluateMdx(source, opts)` — compiles MDX source string at runtime via `@mdx-js/mdx`, executes with the custom JSX runtime, returns flattened FxtrCbor. Supports custom components and additional props.
- **render.ts**: `renderMdxToFxtr(contentFn, opts)` — takes a pre-compiled MDX content function (the default export from a `.mdx` module) and renders it through the custom runtime + flattening pipeline.

## Usage Paths

- **Build-time**: Configure `jsxImportSource: "@fxtr/mdx-transform"` in Vite/Rollup for `.mdx` files. The JSX runtime is injected automatically.
- **Runtime**: Call `evaluateMdx(mdxString, { components, props })` for dynamic MDX compilation.
- **Pre-compiled**: Call `renderMdxToFxtr(mdxContentFn, { components, props })` for already-compiled MDX modules.

## Dependencies

- `@fxtr/core` — FxtrCbor type and ID marker classes
- `@mdx-js/mdx` — MDX compiler and runtime
