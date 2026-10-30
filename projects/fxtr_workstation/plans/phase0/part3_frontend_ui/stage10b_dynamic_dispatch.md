# Stage 10b — Dynamic dispatch

Builds on the `Dispatcher` abstraction landed in
[stage 10a](./stage10a_dispatch_refactor.md). Adds a
`DynamicDispatcher` that resolves an entity / service type to a renderer
component through:

1. **Type discovery** — load the entity to read `$fxtr.type`, or invoke
   `$fxtr.type()` on the service, yielding an `EntityRefStub` for the
   type.
2. **Renderer resolution** — invoke a configured backend dispatcher
   service with that type ref. The service returns
   `(subtype_key, rendererRef)`.
3. **Module load** — bytes-load the renderer entity, dynamic-import its
   compiled ESM bytes (via blob URL), and look up
   `module[subtype_key][dispatch_key_uuid]` for the rendered component.

The motivation: agents (and any user code) should be able to register
new renderers at runtime by uploading a renderer entity to the
workstation; later panels rendering entities or services of that type
pick the new renderer up dynamically.

## High-level shape

```ts
const dispatcher: Dispatcher = createDynamicDispatcher({
  session,
  dispatchServiceRef,   // a ServiceRefStub<WorkstationDispatcherProto>
});
```

The consumer is responsible for obtaining `dispatchServiceRef` — typically by
naming a workstation external service and looking it up via
`lookup_short_id_by_external_name` or `get_service_by_external_name`.
The dispatcher does not know about external names directly.

The dispatcher's per-entry state machine has six transition phases plus
two terminal phases:

```
                   ┌── (entity) ──┐
   init ───────────┤              ├── resolving-renderer
                   └── (service) ─┘            │
                                               ▼
                              loading-renderer-bytes
                                               │
                                               ▼
                                            resolved
init                              ──► resolved-builtin
resolving-renderer on null result ──► no-match
any phase on failure              ──► errored
```

- **(entity branch)** `init → loading-entity → resolving-renderer`
- **(service branch)** `init → loading-service-type → resolving-renderer`
- **(shared tail)** `resolving-renderer → loading-renderer-bytes → resolved`
- **(no-match)** `resolving-renderer → no-match` when the dispatcher
  service returns `null` for the type ref. Publishes the key's
  `NoMatchFallback`; terminal.

### Phase 1: type discovery (per-entry)

**Entity stub:**
- `init` → check the builtin fast-path (`.` / `!`-prefixed shortIds);
  hit → `resolved-builtin`; miss → `ctx.allocateLoad(stub)`; transition
  to `loading-entity`.
- `loading-entity` → wait for the load. On filled, extract
  `value["$fxtr.type"]` as `EntityRefStub` (errored on missing /
  wrong-typed field). Transition to `resolving-renderer` with the type
  ref.

**Service stub:**
- `init` → check `userState.service_type_cache[stub.shortId]`:
  - hit → transition to `resolving-renderer` with the cached type ref
    (no wire traffic).
  - miss → check `userState.service_type_active_lookups[stub.shortId]`:
    - hit → use that `OpResultStub`; transition to
      `loading-service-type` holding it.
    - miss → `ctx.allocateInvoke(stub, "$fxtr.type", [])`, write the
      result stub into `service_type_active_lookups[stub.shortId]` in
      the same updater, transition to `loading-service-type`.
- `loading-service-type` → wait for the invoke. On filled, extract the
  `EntityRefStub` from the result (errored on wrong shape). In one
  updater: promote into `service_type_cache[stub.shortId] = typeRef`
  and delete from `service_type_active_lookups[stub.shortId]` — this
  drops the slot's wire footprint. Transition to `resolving-renderer`.

The asymmetry exists because `allocateLoad` already dedups at the
session layer (two entries for the same entity converge on one slot),
while `allocateInvoke` mints fresh slots per call — so we need an
explicit dispatcher-level dedup for service type discovery.

### Phase 2: renderer resolution (shared)

`resolving-renderer` holds the type ref. The dispatcher service's
`resolve()` returns `RendererResolution | None`; `None` is the
backend's "no renderer registered for this type" signal.

- check `userState.type_renderer_cache[typeRef.shortId]` (presence
  check uses `hasOwnProperty` since a cached `null` is meaningful):
  - hit with `RendererResolution` → transition to
    `loading-renderer-bytes` with the cached `(subtype_key, rendererRef)`.
  - hit with `null` → transition to `no-match` directly (no wire,
    no re-invoke).
  - miss → check `userState.type_renderer_active_lookups[typeRef.shortId]`:
    - hit → use that `OpResultStub`.
    - miss → `ctx.allocateInvoke(dispatchServiceRef, "resolve", [typeRef])`,
      write into `type_renderer_active_lookups[typeRef.shortId]` in the
      same updater.
  - either way: wait for the invoke's `result` atom. On filled, in one
    updater: promote into
    `type_renderer_cache[typeRef.shortId] = {subtype_key, renderer} | null`
    and delete from `type_renderer_active_lookups[typeRef.shortId]`.
    Transition to `loading-renderer-bytes` on a resolution or to
    `no-match` on `null`.

### Phase 3: bytes load + compile + map lookup (shared)

`loading-renderer-bytes` holds `(subtype_key, rendererRef)`:
- Read the dispatcher's in-memory
  `compiledModulesAtom: Atom<Map<rendererShortId, CompiledModuleStatus>>`.
  - `{status: "ready", module}` → look up `module[subtype_key][key.uuid]`.
    - hit → publish that component, transition to `resolved`.
    - miss → publish `NoMatchFallback`, `console.warn`, transition to
      `resolved` (we still consider it terminal; the warning surfaces
      the backend/frontend disagreement).
  - `{status: "errored", message}` → transition to `errored`.
  - `{status: "loading"}` → wait (the effect subscribes to the atom and
    will re-run when it flips).
  - **absent** → kick off the compile-and-load (see below), seed entry
    with `{status: "loading"}`, wait.

The bytes-load itself is owned by the dispatcher (not the entry):

```ts
async function ensureCompiled(rendererRef): Promise<void> {
  // dedup by in-flight promise
  const existing = compiledLoadInFlight.get(rendererRef.shortId);
  if (existing) return existing;
  const p = (async () => {
    const data = await connector.loadEntityData<RendererEntityData>(rendererRef);
    const blob = new Blob([data.module], {type: "application/javascript"});
    const url = URL.createObjectURL(blob);
    try {
      const mod = await import(/* @vite-ignore */ url);
      const exported = mod.default; // 2-level map
      // commit
      writeCompiledModule(rendererRef.shortId, {status: "ready", module: exported});
    } catch (e) {
      writeCompiledModule(rendererRef.shortId, {status: "errored", message: String(e)});
    } finally {
      URL.revokeObjectURL(url);
    }
  })();
  compiledLoadInFlight.set(rendererRef.shortId, p);
  return p;
}
```

`writeCompiledModule` writes through `session.store.set(compiledModulesAtom, ...)` so subscribers re-run.

### Terminal re-sync

`resolved`, `resolved-builtin`, and `errored` all re-run their
component-pick on every effect rerun (same pattern as the static
dispatcher) — this handles the reattach-to-existing-session case where
`userState` carries a terminal phase but the in-memory `componentAtom`
and `compiledModulesAtom` are fresh. For `resolved`, the re-run checks
`compiledModulesAtom`:
- if not present → kick a fresh `ensureCompiled`, revert to
  `loading-renderer-bytes`; the userState `(subtype_key, rendererRef)`
  is still there so this is just a phase relabel.
- otherwise → publish the component as before.

## userState shape

```ts
DynamicDispatcherUserState = {
  // Per-entry state-machine state (shared shape with the static dispatcher).
  entries: Record<keyUuid, Record<shortId, EntryUserStateSlot>>;

  // Service phase-1 caches (entity phase-1 uses allocateLoad's wire dedup).
  service_type_cache: Record<serviceShortId, EntityRefStub>;
  service_type_active_lookups: Record<serviceShortId, OpResultStub<EntityRef>>;

  // Phase-2 caches (shared across stub kinds).
  // The wire-decoded RendererResolution value lands as
  // {subtype_key: string, renderer: EntityRefStub} on the TS side.
  // A cached `null` records a no-match answer from the dispatcher
  // service; subsequent dispatches for the same type short-circuit
  // straight to the `no-match` terminal without re-invoking.
  type_renderer_cache:
    Record<typeShortId, {subtype_key: string, renderer: EntityRefStub} | null>;
  type_renderer_active_lookups:
    Record<typeShortId, OpResultStub<{subtype_key: string, renderer: EntityRefStub} | null>>;
};
```

The four sibling fields survive reattach. `*_cache` mappings persist
indefinitely — the workstation guarantees entity short_ids are stable
for the server's lifetime, so we never have to invalidate.
`*_active_lookups` slots are promoted into the cache as soon as they
fill and removed in the same updater, so the slot's wire footprint
drops immediately.

## In-memory state (dispatcher-scope)

```ts
type CompiledModuleStatus =
  | { status: "loading" }
  | { status: "ready"; module: CompiledRendererModule }
  | { status: "errored"; message: string };

type CompiledRendererModule =
  Record<string /* subtype_key */, Record<string /* key.uuid */, Component>>;

// Single primitive atom holding the full map; writes go through
// `session.store.set` and notify all entries subscribed to it.
const compiledModulesAtom: PrimitiveAtom<Map<rendererShortId, CompiledModuleStatus>>;
// Dedup for concurrent compile + load on the same renderer.
const compiledLoadInFlight: Map<rendererShortId, Promise<void>>;
```

Lost on page reload; rebuilt on first reference. The session-backed
`type_renderer_cache` short-circuits the dispatch-service round-trip
on cold reload, so recovery jumps straight to bytes-load.

`dispatcher.detach()` drops both — pending in-flight promises are not
awaited (they resolve into a closed dispatcher and become inert; the
URL revoke in `finally` still fires).

## Wire interactions

- **Bytes load.** The connector's existing
  `loadEntityData<T>(stub): Promise<T>` (out-of-band `LoadEntityRequest`,
  no session linkage, no refcounts) is what we use. No new connector
  method needed; the proposed `loadEntityBytes` in the original sketch
  is just `loadEntityData<RendererEntityData>`.
- **Dispatch service invoke.** Standard session-level `ctx.allocateInvoke`,
  same machinery as any user-driven invoke.
- **Service type discovery invoke.** Same; the method name is
  `"$fxtr.type"` — the protocol-side guarantee is that every service
  exposes this method returning an `EntityRefStub` for the service's
  type entity.

## Externalization (frontend shim infrastructure)

Renderer modules are compiled on the backend with a fixed external list
and dynamic-imported on the frontend through an import-map that
resolves those externals to the frontend's bundled instances.

### V1 external set

```
react
react/jsx-runtime
jotai
jotai-effect
@fxtr-ui/core
@fxtr-ui/state-util
@fxtr-ui/react
@fxtr-ui/react-dispatch
```

Rationale: anything where renderer code needs identity equality with
the host's instance (React for hooks, jotai for atom semantics, the
stub classes for `instanceof`, the dispatch package for context
identity behind `useDispatchSharedAtom` etc.). Everything else
(JS-side helpers, types, etc.) bundles.

### Wiring

A new package `@fxtr-ui/react-dispatch-dynamic-shim` owns this
infrastructure. It's deliberately separate from
`@fxtr-ui/react-dispatch` so the dispatch package itself stays free of
runtime-injection / import-map plumbing, and so a consumer that only
uses static dispatch doesn't pay for the shim machinery.

In the consumer app's entry point (e.g. `main.tsx`):

```ts
import * as React from "react";
import * as ReactJsxRuntime from "react/jsx-runtime";
// ... other externals ...
import { installFxtrExternals } from "@fxtr-ui/react-dispatch-dynamic-shim";

installFxtrExternals({
  "react": React,
  "react/jsx-runtime": ReactJsxRuntime,
  // ... etc.
});
```

`installFxtrExternals`:
1. Populates `window.__FXTR_INJECTED__` with the supplied modules.
2. Generates a blob-URL shim per module that re-exports from
   `window.__FXTR_INJECTED__[name]` (using a small static-shape
   template, not full export enumeration — see below).
3. Injects a `<script type="importmap">` mapping each external name to
   its blob-URL shim.

The shim shape:

```js
const __mod = window.__FXTR_INJECTED__["${name}"];
if (!__mod) throw new Error("Module ${name} not in __FXTR_INJECTED__");
export default __mod.default ?? __mod;
// re-export everything that was on __mod at injection time
export const { ${names.join(", ")} } = __mod;
```

We enumerate `Object.keys(__mod)` at install time so the shim's named
exports are concrete. (This is the approach thingwatcher's
`generate-local-shim.mjs` uses for local TS modules; it works fine at
runtime via `Object.keys`.)

Must be called *before* any dynamic dispatcher entry runs — that's a
documented precondition; the dispatcher does not enforce it. In
practice, the demo's `main.tsx` calls it before constructing the
dispatcher.

### Why import-map vs. inline injection

We could also bundle a custom module loader that intercepts `import`
strings — but browser import-maps are a standardized mechanism with
direct browser support; the blob-URL trick keeps the whole setup
runtime-only (no checked-in shim files); and the contract for renderer
authors stays "write code that imports React the normal way."

### Content Security Policy

Browsers reject `import(blobUrl)` under a default-strict CSP unless
`script-src` includes `blob:`. Vite dev mode is permissive so this
doesn't bite during demo development, but anyone deploying the
dispatcher in a CSP-restricted environment must include `blob:` in
their `script-src` directive. The longer-term plan (see "Trust /
sandboxing" below) replaces this with a more constrained model.

### Open questions on externals (deferred)

- Hot-swap support if the host updates an external (it can't today
  since `__FXTR_INJECTED__` is frozen-at-install).

## Backend layout

All backend dynamic-dispatch code nests under a single submodule:

```
fxtr_workstation/
└── dynamic_dispatch/
    ├── __init__.py              # re-exports the protocol, entity type, and impl
    ├── dynamic_dispatch_types.py    # @service_protocol + DataclassEntity definitions
    └── basic_dynamic_dispatch/      # one specific implementation
        ├── __init__.py              # re-exports BasicDynamicDispatchImpl
        ├── service_impl.py          # WorkstationDispatcherProto impl
        └── compile.py               # esbuild wrapper
```

### `dynamic_dispatch/dynamic_dispatch_types.py`

Typed protocol + entity definitions. The wire shape rides through the
existing `@service_protocol` / `DataclassEntity` machinery — see
`v2/fxtr2/services/LOCAL_CODE_OVERVIEW.md` for the typed-service
dispatch contract and `v2/CODE_OVERVIEW.md` for the DataclassEntity
pattern.

```python
import uuid
from dataclasses import dataclass
from typing import Protocol

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_ref import EntityRef
from fxtr2.data.system_entities import define_type
from fxtr2.services.typed_service import service_protocol

DYNAMIC_DISPATCH_NAMESPACE = uuid.UUID("...stable-uuid...")

@dataclass(frozen=True)
class RendererEntity(
    DataclassEntity,
    fxtr_type=define_type(DYNAMIC_DISPATCH_NAMESPACE, "renderer"),
):
    """A compiled ESM renderer module, content-addressed by its own
    entity ID. The frontend uses the entity's short_id (server-stable
    for the workstation's lifetime) as the in-memory module cache key.
    """
    module: bytes

@dataclass(frozen=True)
class RendererResolution:
    """Result of a single dispatcher `resolve()` call."""
    subtype_key: str
    renderer: EntityRef[RendererEntity]

@service_protocol
class WorkstationDispatcherProto(Protocol):
    """Service the dynamic dispatcher invokes to find a renderer for a
    type. Returns `None` when no renderer is registered — the
    frontend surfaces that as the dispatch key's `NoMatchFallback`."""
    async def resolve(self, type_ref: EntityRef) -> RendererResolution | None: ...
```

The typed-service codec handles the conversion automatically — the
`EntityRef` argument and the `RendererResolution` return value are
both expressed in the typed vocabulary; the wire ships through
`@service_protocol`'s generated wrapper machinery.

The renderer entity's `fxtr_type` is what the renderer-entity's own
`$fxtr.type` returns when the frontend introspects — not relevant to
the dispatch path (the cache key is the entity's short_id, not its
type), but useful for consistency / future use.

### `dynamic_dispatch/basic_dynamic_dispatch/`

- `compile.py` — `compile_renderer(directory: Path) -> bytes`. Shells
  out to `esbuild` on `$PATH` (matches thingwatcher) with `--bundle
  --format=esm --external:<each of the v1 externals>`. Returns the
  compiled ESM bytes. The entry-point convention is
  `<directory>/index.tsx`.
- `service_impl.py` — `BasicDynamicDispatchImpl`:

  ```python
  class BasicDynamicDispatchImpl(TypedServiceImpl[WorkstationDispatcherProto]):
      def __init__(self, scope: FxtrScope, renderer_dirs: Sequence[Path]):
          self._scope = scope
          self._renderer_dirs = renderer_dirs
          self._table: dict[str, RendererResolution] = {}  # type_id_string → resolution

      async def compile_and_register(self) -> None:
          """Walks renderer_dirs, compiles each subdirectory, creates
          a RendererEntity in self._scope, populates the resolution
          table. Multiple dirs are walked in order; later entries
          override earlier ones for the same type_id_string."""
          ...

      async def resolve(self, type_ref: EntityRef) -> RendererResolution:
          type_id_str = type_ref.id_string()  # bare-id string form
          return self._table[type_id_str]    # KeyError → IRException
  ```

  Mount + use pattern (mirrors
  `projects/fxtr_workstation/tests/test_external_services_7post1.py`):

  ```python
  async with FxtrScope(isolated=True, mountable=True, name="dispatcher_owner") as user_scope:
      impl = BasicDynamicDispatchImpl(scope=user_scope, renderer_dirs=[...])
      await impl.compile_and_register()
      ref = user_scope.mount(impl).replace_service_type(WorkstationDispatcherProto)
      cfg = ExternalServiceConfig(
          external_name="renderer-dispatcher",
          service=ref,
          scope=user_scope,
      )
      async with WorkstationServer(external_services=[cfg]) as ws:
          ...
  ```

  No `BasicDynamicDispatchConfig`, no `worker.py` — the user already
  owns the scope and the external-service config. The impl just
  exposes a constructor + an async `compile_and_register` setup hook;
  the user's startup script orchestrates the rest.

  Renderer entities created inside `user_scope` stay alive for as long
  as that scope, which the workstation pins by virtue of its
  `bridge_service_from_scope` call against the external-service ref.
  The `resolve()` return inherits via the user_scope → workstation
  scope parent chain, so the returned ref is reachable in the call
  scope without any explicit bridging from the impl side.

  v1 `subtype_key` is hardcoded `"default"` — the frontend treats it
  opaquely so the backend is free to change this later (e.g. one
  module serves multiple subtype labels).

Convention for renderer directories: each subdirectory's name matches a
type ID string; the directory contains an `index.tsx` that
default-exports the 2-level `[subtype_key][key_uuid] → Component` map.
The `key_uuid`s come from the `DispatchKey<>` declarations on the
frontend — the renderer author needs to know them statically. (For the
demo we'll define the dispatch keys in a shared `.ts` file and import
the uuids; for general use it's the renderer author's responsibility
to keep them in sync, the same way Python and TS protocol shapes are
hand-mirrored elsewhere in this codebase.)

## Public API sketch

```ts
// @fxtr-ui/react-dispatch-dynamic-shim (new package)
function installFxtrExternals(modules: Record<string, unknown>): void;

// @fxtr-ui/react-dispatch
function createDynamicDispatcher(opts: {
  session: Session;
  dispatchServiceRef: ServiceRefStub<WorkstationDispatcherProto>;
}): Dispatcher;
```

```py
# fxtr_workstation/dynamic_dispatch/ (new submodule)
from fxtr_workstation.dynamic_dispatch import (
    WorkstationDispatcherProto,
    RendererEntity,
    RendererResolution,
    BasicDynamicDispatchImpl,
)
```

## Demo

A new `@fxtr-ui/dynamic-dispatch-demo` package (fork of
`@fxtr-ui/dispatch-demo`):

- **Python backend** exposes the same `Note` / `Photo` entity types as
  the static demo, but the renderers are uploaded as renderer entities
  rather than hardcoded in TS. The orchestrator creates a
  `BasicDynamicDispatchImpl` with a single renderer dir containing
  `<note_type_id>/index.tsx` and `<photo_type_id>/index.tsx`, each
  exporting the `default` subtype map; mounts it on a user-owned
  scope; wraps in an `ExternalServiceConfig(external_name="renderer-dispatcher", ...)`;
  hands that to `WorkstationServer(external_services=[...])`.
- **TS side** uses one `DynamicDispatcher` instance. `App.tsx`:
  1. calls `installFxtrExternals(...)` early,
  2. constructs the workstation connector,
  3. acquires a session and looks up the dispatcher service ref by
     external name,
  4. constructs the dispatcher,
  5. renders the grid the same way `dispatch-demo` does.

Stretch: a "fake agent" UI button that uploads a fresh renderer entity
and re-registers it with the dispatch service.

## Chunk plan

Chunks are organizational only — they're how the work splits into
review-sized pieces, not independently-mergeable units. CODE_OVERVIEW
updates happen at the end of the stage.

### Chunk 1 — Backend protocol + types

- `dynamic_dispatch/dynamic_dispatch_types.py` with
  `DYNAMIC_DISPATCH_NAMESPACE`, `RendererEntity` (DataclassEntity),
  `RendererResolution`, and the `@service_protocol`-decorated
  `WorkstationDispatcherProto`.
- `dynamic_dispatch/__init__.py` re-exports.
- Tests: smoke test that mounts a hand-rolled impl of
  `WorkstationDispatcherProto` against an `ExternalServiceConfig`
  pattern (matching `test_external_services_7post1.py`), confirms an
  RPC peer can resolve and invoke `resolve()` with a typed
  `RendererResolution` return.

### Chunk 2 — Backend `basic_dynamic_dispatch`

- `compile.py` shells out to ``npx esbuild`` (preferred over a bare
  ``esbuild`` binary on PATH, because ``npx`` works wherever Node.js
  is installed and picks up a project-local install if present).
  Tests gate on a ``npx_available()`` skip helper. Round-trip test:
  TSX in, ESM bytes out, the externalized imports survive as bare
  specifiers.
- `service_impl.py` — `BasicDynamicDispatchImpl` walks renderer dirs
  in its `compile_and_register()` setup method, compiles each,
  creates `RendererEntity` instances in the user-supplied scope,
  populates the type_id_string → `RendererResolution` table. Exposes
  `resolve()`.
- Tests against a fixture renderer dir with two type-id buckets;
  confirms `resolve()` returns the right `RendererResolution` and the
  loaded entity's bytes decode to ESM with the expected default
  export.

### Chunk 3 — Frontend external shim infrastructure

- New package `@fxtr-ui/react-dispatch-dynamic-shim` with
  `installFxtrExternals` + the blob-URL shim generator + import-map
  injection.
- Unit test: install a fake module, `import()` a hand-written blob URL
  whose code does `import { foo } from "fake-pkg"`, verify it resolves
  to the injected fake.

### Chunk 4 — Frontend `createDynamicDispatcher` skeleton

- `src/dynamic_dispatcher.ts` — userState shape (the four sibling
  caches + `entries`), `compiledModulesAtom`, `compiledLoadInFlight`,
  the dispatcher `status` / `getEntry` / `detach` shape.
- State machine wired only through phase 1's terminal — i.e. extracts
  the type ref and stops with `NoMatchFallback`. Validates that
  `createBaseEntry` is general enough.
- Tests with mocked sessions covering the entity and service phase-1
  paths, including service phase-1 dedup.

### Interlude — Dispatch-key fallback refactor

Between chunks 4 and 5 we pause to land
[`stage10aside1_dispatch_key_fallback_refactor.md`](./stage10aside1_dispatch_key_fallback_refactor.md):
move `LoadingFallback` / `NoMatchFallback` / `ErrorFallback` /
`Builtin?` from per-dispatcher registries onto the `DispatchKey`
declaration itself. After this the dynamic dispatcher needs no
registry at all — it can handle any key — and the static dispatcher
renders `NoMatchFallback` for unknown keys instead of throwing,
making the two dispatchers interchangeable.

This re-shapes the surface chunks 5+ build on. Doing it now means
chunks 5/6 are written against the simpler API; deferring would
have us refactor everything we build below.

### Chunk 5 — Frontend phase 2 (renderer resolution)

- Implement `resolving-renderer` with cache hit / active-lookup-share /
  fresh-invoke / fill-and-promote semantics.
- Tests: dispatcher-level dedup (two entries → one invoke); fill
  promotes into cache and drops the slot; reject path transitions to
  `errored`.

### Chunk 6 — Frontend phase 3 (bytes + compile + lookup)

- `ensureCompiled` plumbing; `loading-renderer-bytes` reads
  `compiledModulesAtom`; terminal `resolved` re-sync handles missing
  in-memory state.
- Outer-subtype-key miss + inner-key-uuid miss both render
  `NoMatchFallback` with `console.warn`.
- Tests: round-trip compile (uses a hand-written ESM bytes fixture,
  not a real esbuild call), terminal re-sync, compile failure
  transitions to `errored`.

### Chunk 7 — End-to-end demo + integration tests

- New `@fxtr-ui/dynamic-dispatch-demo` package: orchestrator script,
  demo backend, demo renderer dirs, App.tsx with the full wiring.
- Live integration test: spawn the demo, render Note + Photo,
  assert the right components show.
- (Stretch) runtime-registration demo: a button uploads a fresh
  renderer; reload picks it up.

**Landed:**
- Demo package scaffold: `package.json`, `tsconfig.json`,
  `vite.config.ts`, `index.html`.
- Python backend `dynamic_dispatch_demo_workstation.py` —
  mounts `BasicDynamicDispatchImpl` as the `renderer-dispatcher`
  external service, stages renderer templates into a temp dir keyed
  by computed type-id strings, seeds Note/Photo entities. Emits
  `TYPE_IDS` / `RENDERER_DISPATCHER_NAME` / `READY` / `DEBUG_UI`
  lines.
- Renderer templates `src/renderers/{note,photo}/index.tsx`.
- TS client (`main.tsx` installs externals, `App.tsx` builds the
  dynamic dispatcher, `DispatchGrid.tsx`, four fallback Card
  components, `dispatch_keys.ts`).
- Orchestrator `src/server.ts` (parses the four protocol lines,
  sets env vars, starts Vite).
- Integration harness `_dynamic_integration_harness.ts` and the
  live test `dynamic_integration.test.ts` (uses a mock
  `compileRendererBytes` so vitest doesn't need a real ESM loader).
  The test waits for `session.lifecycle === "connected"` before
  driving the dispatcher (`createSession` returns in `"connecting"`
  state; any `allocate*` before the first snapshot throws).
- `compile.py` shells out to `npx esbuild`; `esbuild` was added as
  a workspace dev dependency in `package.json` so it
  reliably resolves through `npx`.

**Bytes round-trip fix.** `RendererEntity.module: bytes` was
arriving on the frontend as a base64 `str` rather than a
`Uint8Array`. Root cause: `mashumaro`'s default serialization of
`bytes` is base64-encoded `str`, and the workstation's
`wire_escape` only tags `bytes`-typed values. Fix landed in
`fxtr2.conversion.converters.mashumaro_entities.DataclassEntity`:
the `Config.serialization_strategy` now maps `bytes` to
`mashumaro.helper.pass_through`, so raw bytes survive both CBOR
(which has a native byte-string type) and `wire_escape` (which
tags them as `["bytes", "<base64>"]`). One test in
`test_mashumaro_entities.py` was updated to reflect the new
default; the wider suite is unaffected.

**`resolve()` no-match.** The dispatcher service's `resolve()`
method now returns `RendererResolution | None`. `None` is the
backend's "no renderer registered for this type" signal — the
frontend caches the null in `type_renderer_cache`, transitions to
a new `no-match` terminal phase, and publishes the dispatch key's
`NoMatchFallback` (parallel to the existing
`resolved-builtin` → `Builtin ?? NoMatch` path). Previously this
case raised `IRException` and routed to the `Error` fallback,
which is wrong: missing renderers are an expected case, not a
fault. The cached null short-circuits subsequent dispatches for
the same type without re-invoking `resolve`.

**Stretch goal (runtime-registration demo) — deferred.** Mentioned
in the original chunk-7 plan as optional; not in scope for closing
out this stage.

### Chunk 8 — CODE_OVERVIEW + LOCAL_CODE_OVERVIEW updates

- `projects/fxtr_workstation/CODE_OVERVIEW.md` — note the new module +
  subpackage in the structure section, describe the protocol.
- `projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md` — describe
  `createDynamicDispatcher`, the userState shape, the external shim
  infrastructure, and the demo package.

**Note:** the aside-1 refactor (dispatch-key fallbacks) has
already been propagated into both CODE_OVERVIEW files. Chunk 8's
remaining scope is the *dynamic-dispatcher-specific* additions —
new userState fields, new state-machine phases, in-memory module
cache, `installFxtrExternals`, the new demo package, etc.

## Open questions (deferred)

- **Renderer-entity wire shape evolution.** Today's
  `{"$fxtr.type": EntityRef, "module": bytes}` is minimal. Later we
  may want a `module_metadata` field carrying expected dispatch_key
  uuids / subtype_keys for fail-fast at compile time. Defer.
- **Cache invalidation / hot updates.** If a renderer entity is
  superseded server-side, existing dispatches stay on the old version
  until reload. Defer aggressive invalidation.
- **Capability surface for renderer ESMs.** V1 externals listed above.
  Adding more (e.g. workstation connector access) requires deciding
  whether a renderer is the right place to do wire work — probably
  not. Defer until a concrete need arises.
- **Trust model / sandboxing.** Defer; v1 is "trusted agent in the
  user's session." Two future paths under consideration:
  - **Sandboxed iframe** — render dynamic content in an isolated
    iframe with a postMessage bridge to the host; sidesteps the
    `blob:` import + CSP relaxation entirely.
  - **Trusted-hosted URL** — serve verified renderer bundles from a
    known origin (e.g. a workstation-served `/renderers/<short_id>.js`
    endpoint), so production deployments don't need `script-src
    'blob:'` and provenance is enforceable.
- **Cross-tab cache sharing.** Defer; could ride workstation
  persistence namespaces.
- **Renderer dispatcher v2** — multiple candidate renderers / ranked
  list, type matching via inheritance, policy filters. v1 returns one
  result; v2 design space is open.
- **Debug panel.** A workstation debug panel showing dispatcher state
  (per-entry phase, caches, in-memory module status) would be useful;
  scope this as a follow-up after the system works end to end.

## Design notes to consult

- `stage10a_dispatch_refactor.md` — the abstraction this stage builds on.
- `stage7_dispatch.md` — original static dispatch design;
  state-machine pattern carries over.

## Implementation tasks

- [x] **Chunk 1** — Backend protocol + types.
- [x] **Chunk 2** — Backend `basic_dynamic_dispatch`.
- [x] **Chunk 3** — Frontend external shim infrastructure.
- [x] **Chunk 4** — Frontend dispatcher skeleton through phase-1.
- [x] **Chunk 5** — Frontend phase 2 (renderer resolution).
- [x] **Chunk 6** — Frontend phase 3 (bytes + compile + lookup).
- [x] **Aside 1** — Dispatch-key fallback refactor (see
      `stage10aside1_dispatch_key_fallback_refactor.md`).
- [x] **Chunk 7** — End-to-end demo + integration tests.
- [x] **Chunk 8** — CODE_OVERVIEW + LOCAL_CODE_OVERVIEW updates.

## What's left to finish this stage

1. **Browser smoke-test the demo.** `pnpm dev` from
   `frontend_lib/packages/fxtr-ui-dynamic-dispatch-demo/`. Verify
   in a real browser:
   - Externals install cleanly (no console errors at startup).
   - Each demo Note / Photo entity card renders the dynamically-
     loaded `NoteCard` / `PhotoCard` styling.
   - Reload preserves the dispatcher session's caches (the
     `type_renderer_cache` short-circuits the resolve invoke on
     re-render, but the in-memory `compiledModulesAtom` needs the
     bytes-reload to re-populate).
   - Open the workstation debug UI (link in the header) and verify
     the dispatcher service is present + the resolve invoke
     appears on first dispatch.

2. **Chunk 8 — CODE_OVERVIEW updates.** Two files:
   - `projects/fxtr_workstation/CODE_OVERVIEW.md` — add the
     `fxtr_workstation/dynamic_dispatch/` submodule to the
     structure listing + a short description of the protocol +
     `BasicDynamicDispatchImpl` + the renderer-entity shape. The
     protocol's `resolve()` returns `RendererResolution | None`
     (None = no-match).
   - `projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md` —
     describe `createDynamicDispatcher` (state machine including
     the `no-match` terminal, four userState caches,
     `type_renderer_cache` holds `RendererResolution | null`,
     in-memory compiled-module cache, `loadEntityData` /
     `compileRendererBytes` injection points), the
     `@fxtr-ui/react-dispatch-dynamic-shim` package, and the
     `@fxtr-ui/dynamic-dispatch-demo` package. The
     `react-dispatch` package section already describes
     fallbacks-on-key + overrides (from the aside) but doesn't yet
     mention `dynamic_dispatcher.ts` — needs a fresh subsection.
   - Per the existing memory: describe current state, no chunk /
     stage cross-refs in the body (TODO markers OK for forward-
     looking work).

3. **Optional follow-ups (not blocking stage close):**
   - Browser-side integration test using Playwright or similar to
     exercise the real `URL.createObjectURL` + `import()` path. The
     current vitest test mocks `compileRendererBytes`; verifying
     the real default would catch CSP / import-map regressions.
   - The "fake-agent" runtime-registration stretch demo (a UI
     button that uploads a fresh renderer entity + re-registers it
     against the dispatcher service + watches the change
     propagate). Out of scope for stage close.
