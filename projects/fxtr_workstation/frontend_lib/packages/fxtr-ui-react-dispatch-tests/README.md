# @fxtr-ui/react-dispatch-tests

Tests-only package for `@fxtr-ui/react-dispatch`'s React layer
(`<DispatchProvider>`, `<RenderDispatch>`, the dispatched-component-side
hooks). Same rationale as its sibling `@fxtr-ui/react-tests`: vitest's
`@vitest-environment jsdom` directive doesn't load `.tsx` test files
reliably from `projects/fxtr_workstation/frontend_lib/packages/...`, so
React-rendering tests for the dispatch package live here under
`v2/ts/packages/`.

Non-React dispatch tests (key declaration, registry assembly, state-
machine logic without rendering) continue to live next to the source
in `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-dispatch/tests/`.

See `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-tests/README.md`
for the full backstory on the workaround.
