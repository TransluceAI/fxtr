# @fxtr/example-mdx-rendering — Code Overview

Demo app showing MDX-to-fxtr rendering. A server compiles MDX pages into `ComponentTree` entities served via fxtr HTTP; a React client renders the component trees.

## Server

- Defines two entities: `ComponentTree` (stores `document: FxtrCbor[]`) and `Stats` (stores `users`, `revenue`, `growth`).
- Registers custom server-side MDX components (`Callout`, `Chart`, `DataTable`, `FxtrStats`) that expand during rendering. `FxtrStats` is async — it creates a `Stats` entity in the scope and embeds a weak reference.
- The fxtr scope is passed as the `fxtr_scope` prop to MDX content, supporting concurrent renders without module-level state.
- Serves both fxtr HTTP routes (`/pin/`, `/data/`, `/preview/`, `/pins`) and Vite dev middleware on port 3456.

## Client

- Connects via `createHttpReadNamespace`, wrapped in `FxtrProvider`.
- Dropdown selector for choosing between pages (pre-compiled vs dynamic).
- `FxtrTreeRenderer` walks `FxtrCbor[]` document nodes and renders them to HTML/React elements.
- Client component registry dispatches custom types (`Chart`, `DataGrid`, `FxtrStats`) to client-side handlers.

## Files

```
src/
├── entities.ts           # ComponentTree, Stats entity definitions
├── components.ts         # Server-side custom MDX components
├── server.ts             # Server: renders MDX, creates pins, serves HTTP + Vite
└── client/
    ├── main.tsx          # React entry point
    └── App.tsx           # HTTP namespace setup, page selector, tree renderer
vite.config.ts            # Vite config (dual JSX runtimes: mdx→fxtr, tsx→react)
index.html                # Web client entry point
```

Also see `ARCHITECTURE.md` in this directory for additional design notes.

## Running

```bash
pnpm dev    # starts server on port 3456
```

## Dependencies

- `@fxtr/core`, `@fxtr/http-server`, `@fxtr/http-client`, `@fxtr/react`, `@fxtr/mdx-transform`
