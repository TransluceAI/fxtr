# MDX Transform

We built a system for compiling MDX documents into fxtr entities. The core library (`@fxtr/mdx-transform`) provides a custom JSX runtime that produces an intermediate `MdxNode` tree, which is then flattened into FxtrCbor tuples `[type, props, children]`. Props are validated as `FxtrCbor` at the JSX boundary, so they can contain `RawWeakFxtrID` values referencing other entities. Custom "server macro" components execute during render and can store entities into the fxtr scope. The library supports both a vite/rollup build path (`jsxImportSource`) and a dynamic `compile`+`run` path.

An example app (`@fxtr/example-mdx-rendering`) demonstrates the full round-trip: MDX → `ComponentTree` entity (served via `createFxtrApp`) → React frontend that renders the tree using `useFxtrLoad` for entity-backed components. See `v2/ts/packages/example-mdx-rendering/ARCHITECTURE.md` for the full architecture.
