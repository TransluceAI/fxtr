// Public API of @fxtr-ui/state-util.
//
// Framework-free path projection helpers for session-bound atoms. The
// React-layer hook `useSubAtom` (in `@fxtr-ui/react`) memoizes
// `subAtom(parent, key)` for stable identity across renders; this
// package's surface is the underlying jotai-level primitive.
//
// Also exports `stringifyForDebug` — a `JSON.stringify` wrapper that
// summarizes stub classes as short tagged strings, useful for
// inspecting session state in debug UIs / log lines without tripping
// on the stubs' circular session backrefs.

export { atomWithDefault } from "./atom_with_default";
export { makeInvokeResultAtom } from "./invoke_result_atom";
export { type LoadResult, makeLoadResultAtom } from "./load_result_atom";
export { subAtom } from "./sub_atom";
export { debugReplacer, stringifyForDebug } from "./stringify_for_debug";
