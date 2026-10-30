# @fxtr-ui/react-dispatch-dynamic-shim-tests

Tests-only package for `@fxtr-ui/react-dispatch-dynamic-shim`. Holds
the install-externals tests; no published surface of its own.

## Why is this a separate package?

The tests need vitest's `@vitest-environment jsdom` directive to
exercise the document-side install path (import-map element
injection, blob URLs). When the test files were colocated inside
`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-dispatch-dynamic-shim/tests/`,
vitest reproducibly failed to load them with
`Cannot find module / Failed to load url ... Does the file exist?`
— matching the known `frontend_lib/`-tree issue documented in
`@fxtr-ui/react-tests`'s README.

Same workaround: move the tests under `v2/ts/packages/` and depend on
the real package via `workspace:*`.

## Layout

```
fxtr-ui-react-dispatch-dynamic-shim-tests/
├── package.json
├── tsconfig.json
├── README.md
└── tests/
    └── install_externals.test.ts   # buildShimSource + installFxtrExternals
```
