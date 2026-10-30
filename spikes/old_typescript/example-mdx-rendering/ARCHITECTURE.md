# MDX-to-Fxtr Rendering Example — Architecture

This package demonstrates how to use `@fxtr/mdx-transform` to compile MDX into fxtr entities served over the fxtr HTTP protocol, with a React frontend that renders the component tree.

## Package Split

- **`@fxtr/mdx-transform`** — Reusable library: custom JSX runtime, flatten, render, evaluate. Produces `FxtrCbor[]` document nodes from MDX.
- **`@fxtr/example-mdx-rendering`** (this package) — Example app: entity definitions, custom components, server, client.

## How It Works

### Server (`src/server.ts`)

1. Creates a `FxtrScope`.
2. Renders each MDX page via `renderMdxToFxtr()` — returns `FxtrCbor[]` (fragment already unwrapped).
3. Stores as `ComponentTree` entities, creates pins.
4. Serves via `createFxtrApp()` (standard fxtr HTTP protocol).
5. Vite middleware serves the React frontend.

### Entity Definitions (`src/entities.ts`)

- **`ComponentTree`**: `{ document: FxtrCbor[] }` — the list of document-level nodes.
- **`Stats`**: `{ users, revenue, growth }` — example data entity for the fxtr-aware component.

### Custom Components (`src/components.ts`)

Components are server macros that receive props and return `MdxNode` subtrees.

- **`Callout`**: expands to primitive HTML nodes (server-side).
- **`Chart`**: emits a client-side reference node.
- **`DataTable`**: mix of primitives and client references.
- **`FxtrStats`**: receives `fxtr_scope` via explicit prop threading from MDX (`<FxtrStats fxtr_scope={props.fxtr_scope} />`). Stores a `Stats` entity into the scope and embeds a `RawWeakFxtrID` in its output.

### MDX Prop Threading

The fxtr scope is passed as a `fxtr_scope` prop to the MDX content function. MDX files thread it to components explicitly:

```mdx
<FxtrStats timeout={50} fxtr_scope={props.fxtr_scope} />
```

This avoids module-level state and is safe for concurrent renders.

### Client (`src/client/`)

- **`App.tsx`**: `createHttpReadNamespace` + `FxtrProvider`. Dropdown to select pages by pin name. Loads `ComponentTree` via `useFxtrLoad`.
- **`renderer.tsx`**: Recursively walks `FxtrCbor[]` document nodes, renders HTML elements and dispatches custom types to the client component registry.
- **`components.tsx`**: Client-side registry. `Chart` and `DataGrid` are placeholders. `FxtrStats` extracts `RawWeakFxtrID` from props, constructs `EntityRef<Stats>`, loads via `useFxtrLoad`.

### JSX Import Source

- `@mdx-js/rollup` compiles `.mdx` files with `jsxImportSource: "@fxtr/mdx-transform"`.
- `@vitejs/plugin-react` compiles `.tsx` files with React's JSX runtime by default.

## Running

```bash
cd packages/example-mdx-rendering
pnpm dev
```

Visit `http://localhost:3456/`. Debug: `http://localhost:3456/preview/<id>`.
