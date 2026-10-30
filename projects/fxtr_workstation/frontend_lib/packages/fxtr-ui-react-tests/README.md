# @fxtr-ui/react-tests

A tests-only package for `@fxtr-ui/react`. Holds the component tests that exercise the React provider + hooks layer; no published surface of its own.

## Why is this a separate package?

The tests need vitest's `@vitest-environment jsdom` directive to render React hooks. When the test files were colocated inside `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react/tests/`, vitest reproducibly failed to load them with `Cannot find module / Failed to load url ... Does the file exist?` — but **only** for `.tsx` files **and only** when the `@vitest-environment jsdom` directive was present.

The same directive works fine for `.tsx` test files under `v2/ts/packages/fxtr-react/tests/`. The failure surfaced for every package we tried under `projects/fxtr_workstation/frontend_lib/packages/` — so it correlates with directory tree, not package config. We didn't fully root-cause it (likely a vitest workspace / project-root resolution interaction with the deep path from the workspace root), but moving the tests to a sibling package under `v2/ts/packages/` makes them work reliably.

A proper investigation would either fix the vitest config or document the constraint. Until then, this package is the workaround. See `projects/fxtr_workstation/plans/phase0/part3_frontend_ui/stage4_chunk9_plan.md` for the running note.

## Layout

```
fxtr-ui-react-tests/
├── package.json
├── tsconfig.json
├── README.md
└── tests/
    ├── _mocks.ts                  # Mock Session + TestOpResult helpers
    ├── session_context.test.tsx   # <FxtrSessionProvider> + useSession
    └── hooks.test.tsx             # useSessionState / useSetSessionState / useSubAtom / useResult
```

When the React layer gains new public surface in `@fxtr-ui/react`, its tests come here, not back into `fxtr-ui-react/`.
