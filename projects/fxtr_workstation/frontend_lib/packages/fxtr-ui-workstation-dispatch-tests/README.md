# @fxtr-ui/workstation-dispatch-tests

React-rendering tests for `@fxtr-ui/workstation-dispatch`'s
`RefLink` + `RefLinkImplContext`. Same rationale as
`@fxtr-ui/react-dispatch-tests`: vitest's `@vitest-environment
jsdom` directive doesn't reliably load `.tsx` test files from
`projects/fxtr_workstation/frontend_lib/packages/...`, so React-render
tests live here under `v2/ts/packages/`.

Non-React tests (UUID stability snapshots, registry shape) live
alongside the source in
`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-workstation-dispatch/tests/`.

See `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-tests/README.md`
for the full backstory on the workaround.
