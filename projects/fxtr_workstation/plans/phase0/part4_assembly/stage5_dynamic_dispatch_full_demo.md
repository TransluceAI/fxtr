# Stage 5 — Dynamic dispatch in the full assembly demo

## Goal

The assembly-stage demo (`projects/fxtr_workstation_demo/`) currently
ships a hand-built **static** dispatch registry that maps a fixed
set of type-id strings to React components from
`@fxtr-ui/agent-plugin-renderer`. This stage replaces that with a
**dynamic** dispatcher so the agent can write new visualizers for
new entity types it defines, without operator code changes.

The cleanest split is:

- **Demo-local dispatcher impl.** Lives in
  `projects/fxtr_workstation_demo/`, implements
  `WorkstationDispatcherProto`'s wire surface plus an extra
  `recompile()` method. Reads a single `agent_workdir/renderers/`
  tree containing one shipped `builtin/` subdirectory (the
  hard-coded "default" set, equivalent to the current static
  registry) plus arbitrary agent-written subdirectories.
- **`fxtr_renderer.json` metadata.** Each renderer subdirectory
  declares its routing in a JSON file (subtype-key → `for_type_id`
  + `dispatch_keys`). The dispatcher unions every subdirectory's
  metadata into one global `(type_id, dispatch_key_uuid) →
  (subtype_key, renderer_ref)` lookup. This sidesteps the constraint
  that `resolve()` historically takes only a type — see step 1.
- **Frontend swap.** `createStaticDispatcher` → `createDynamicDispatcher`,
  with `installFxtrExternals` in `main.tsx` so the compiled
  renderer modules can resolve bare imports against the host's
  bundled package instances.

The longer-term design (richer subtype routing, agent-MCP-driven
renderer authoring flow, persistent renderer entities) replaces
this prototype later. The work in this stage stays inside
`projects/fxtr_workstation_demo/` plus the small protocol/frontend
changes from step 1; nothing in the workstation server, MCP plugin,
or shared frontend library beyond `dynamic_dispatcher.ts` needs to
change.

## Layered architecture

```
agent_workdir/renderers/                       (filesystem; agent-writable)
├── builtin/                                   ships in the repo; agent should not edit
│   ├── index.tsx
│   └── fxtr_renderer.json
├── note/                                      ships in the repo as a working example
│   ├── index.tsx
│   └── fxtr_renderer.json
└── <agent-authored>/                          everything else the agent writes
    ├── index.tsx
    └── fxtr_renderer.json

run_demo_workstation.py
└── mounts DemoDispatchImpl  ─── external_services=[ExternalServiceConfig(...)] ──┐
                                                                                  │
frontend                                                                          │
├── main.tsx          installFxtrExternals(react, jsx-runtime, jotai, …,          │
│                                          @fxtr-ui/agent-plugin-renderer,        │
│                                          @fxtr-ui/rd-types-stream)              │
└── App.tsx           createDynamicDispatcher({                                   │
                       session, dispatchServiceRef,                              ─┤
                       loadEntityData })                                          │
                                                                                  │
                                                                                  ▼
                                                                       resolves "renderer-dispatcher"
                                                                       external name on workstation
```

The dispatcher's wire protocol stays
`WorkstationDispatcherProto.{resolve, subscribe_to_changes}` — the
frontend treats `DemoDispatchImpl` as a vanilla
`WorkstationDispatcherProto`. The agent picks up `recompile()` via
a typed-protocol Python module that ships in the agent's sandbox
package layout; the host-side impl exposes the method via plain
`@service_method` decoration without formally implementing the
agent-side protocol class (duck-typed across the wire).

## Step 1 — Extend `WorkstationDispatcherProto.resolve` with a dispatch-key arg

**Motivation.** The current protocol returns one `RendererResolution`
per type. That blocks the "shard a type's renderers across multiple
files" pattern: if module A handles `(type_X, FullPanel)` and module
B handles `(type_X, BlockLink)`, `resolve(type_X)` can return only
one of them, and the other slot misses. Passing the dispatch key
into `resolve` lets the backend disambiguate.

**Interface change.**

```python
# dynamic_dispatch_types.py
class WorkstationDispatcherProto(Protocol):
    async def resolve(
        self,
        type_ref: EntityRef[UnknownEntity],
        dispatch_key_uuid: str,
    ) -> RendererResolution | None: ...
```

**Frontend cache shape.** Switch from flat keyed-by-`typeShortId` to
nested `Record<typeShortId, Record<keyUuid, ...>>`. Same change
applies to `type_renderer_cache` and `type_renderer_active_lookups`:

```ts
// dynamic_dispatcher.ts (sketch)
interface DynamicDispatcherUserState {
  ...
  readonly type_renderer_cache: Record<
    string /* typeShortId */,
    Record<string /* keyUuid */, RendererResolution | null>
  >;
  readonly type_renderer_active_lookups: Record<
    string,
    Record<string, OpResultStub<RendererResolution | null>>
  >;
  ...
}
```

`driveResolvingRenderer` takes a `keyUuid: string` argument (already
in scope as a closure variable of both effect-factory builders) and
threads it through every cache / active-lookup read and write. The
cold-path `allocateInvoke(...)` call gains the `keyUuid` arg.

**Files touched.**

- `projects/fxtr_workstation/fxtr_workstation/dynamic_dispatch/dynamic_dispatch_types.py`
  — protocol signature.
- `projects/fxtr_workstation/fxtr_workstation/dynamic_dispatch/basic_dynamic_dispatch/service_impl.py`
  — `resolve` accepts + ignores the new arg (the subdir-name model
  can't shard by key; existing demo's modules cover every dispatch
  key in the same renderer so resolving by type alone returns the
  right module regardless).
- `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts`
  — cache shape + `driveResolvingRenderer` signature + reload-effect
  wipe path stays valid (sets the nested records back to `{}`).
- `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-dispatch/src/dynamic_debug_view.tsx`
  — type signatures match the new shape; JSON viewer renders the
  nested records as-is.
- `projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-react-dispatch/tests/dynamic_dispatcher.test.ts`
  — every cache / active-lookups assertion gains a key-uuid dimension;
  the mock dispatch-key's UUID becomes part of the test assertions.

**Atomic-flush invariant.** The cold path writes the new invoke stub
into `type_renderer_active_lookups[typeShortId][keyUuid]` in the
same flush that allocates it; the nested form needs care to preserve
the slot-allocation rule that the stub be referenced from userState
by end-of-flush. The function-form updater builds the inner record
once with the new entry and sets the outer record to the new value.

- [x] Update protocol signature in `dynamic_dispatch_types.py`.
- [x] Update `BasicDynamicDispatchImpl.resolve` to accept the new arg.
- [x] Refactor `dynamic_dispatcher.ts` cache shape + thread `keyUuid`
      through `driveResolvingRenderer`.
- [x] Update `dynamic_dispatcher.test.ts` for the new keying scheme.
- [x] Update `dynamic_debug_view.tsx` props/types.
- [x] Update CODE_OVERVIEW.md for the workstation package.
- [x] Update the python `tests/test_basic_dynamic_dispatch_10b.py` +
      `tests/test_dynamic_dispatch_types_10b.py` callsites (and the
      stub impl in the latter) to pass the new arg.

## Step 2 — Demo dispatcher implementation

**Where:** new module
`projects/fxtr_workstation_demo/demo_dispatch.py`. Forks
`BasicDynamicDispatchImpl`'s listener / reload machinery verbatim
(it's the right shape — owned `impl_tg`, per-listener
`_ListenerState` workers, `has_update` skip-ahead, `done` reap) and
replaces the table-building logic.

**Public surface.**

```python
class DemoDispatchImpl(TypedServiceImpl[WorkstationDispatcherProto]):
    """Demo-local dispatcher. Reads <renderers_root> at startup +
    on every `recompile()` call; routes resolve() against a
    (type_id_string, dispatch_key_uuid) → RendererResolution table
    built by unioning each subdirectory's fxtr_renderer.json.

    Exposes `recompile` as a `@service_method` so the agent can
    trigger a hot-reload from a sandbox script. The matching
    `DemoDispatcherProto` definition lives in the agent's
    sandbox-importable `packages/demo_dispatch/`; the host impl
    is duck-typed against it (no formal implements).
    """

    async def resolve(self, type_ref, dispatch_key_uuid) -> RendererResolution | None: ...
    async def subscribe_to_changes(self, listener) -> None: ...
    async def recompile(self) -> None: ...
```

**`fxtr_renderer.json` schema.**

```json
{
  "<subtype_key>": {
    "for_type_id": "<id_string>",
    "dispatch_keys": ["<uuid-1>", "<uuid-2>", ...]
  },
  ...
}
```

Keys are free-form, agent-chosen labels. The dispatcher does not
care what subtype keys mean — they're just opaque indices into the
compiled module's `default` export.

**Per-subdir mtime tracking.** Each subdir's "watermark" is the max
of `index.tsx.mtime` and `fxtr_renderer.json.mtime`. The impl
records `subdir → watermark` in `self._mtimes` at first scan; on
`recompile()` the watermarks are recomputed, and a subdir is
considered "dirty" if its watermark advanced or the subdir is new.
Removed subdirs drop their compiled entity from the live set.

**Table-build algorithm (atomic + incremental).**

```
new_mtimes = scan <renderers_root>/* for subdirs containing both
             index.tsx and fxtr_renderer.json; record max(index.tsx,
             fxtr_renderer.json) mtime per subdir
dirty   = subdirs where new_mtimes[s] != self._mtimes.get(s)
removed = subdirs in self._mtimes but not in new_mtimes
if not dirty and not removed: return  (no broadcast, no swap)

# (1) validation pass — always reads every subdir's JSON, since
# cross-subdir conflicts can be introduced by an edit to *one* file
pending_table = {}        # type_id_string → {uuid: RendererResolution}
parsed_json  = {}         # subdir → parsed JSON
for each subdir in new_mtimes:
    parse fxtr_renderer.json; reject malformed → raise
    for each (subtype_key, {for_type_id, dispatch_keys}):
        for each uuid in dispatch_keys:
            if pending_table[for_type_id].get(uuid) is set:
                raise (conflict between two modules / subtypes)
            pending_table[for_type_id][uuid] = (subtype_key, subdir)

# (2) compile pass — only re-compile dirty subdirs; carry forward
# compiled RendererEntity refs for unchanged subdirs
new_renderer_refs = dict(self._renderer_refs)  # subdir → EntityRef
for s in removed: del new_renderer_refs[s]
for s in dirty:   new_renderer_refs[s] = mint(compile_renderer(s))

# (3) finalise table values: replace (subtype_key, subdir) with
# RendererResolution(subtype_key, new_renderer_refs[subdir])
final_table = ...

# (4) swap atomically — only past this line do we mutate self
self._table          = final_table
self._mtimes         = new_mtimes
self._renderer_refs  = new_renderer_refs
trigger_reload()
```

Atomicity: every mutation to `self.*` happens after the
validation-and-compile phases complete. Any JSON failure, conflict,
or esbuild error raises and leaves the previous table + mtimes
+ renderer refs untouched. The triggering `recompile()` invoke
surfaces the error to the agent (raising propagates back through
the RPC dispatch).

**Resolve.**

```python
async def resolve(self, type_ref, dispatch_key_uuid):
    by_key = self._table.get(type_ref.entity_id.id_string())
    if by_key is None: return None
    return by_key.get(dispatch_key_uuid)  # None on miss too
```

**Reload broadcast.** Identical to `BasicDynamicDispatchImpl`'s
machinery: `recompile()` (and `compile_and_register()` on first
startup) call `trigger_reload()` after a successful table swap. The
subscribed listeners wake, mint a fresh
`ReloadEverythingNotification`, and notify their subscribed
frontends.

**Externals list.** The demo passes a custom externals list:
`DEFAULT_EXTERNALS + ("@fxtr-ui/agent-plugin-renderer",
"@fxtr-ui/rd-types-stream")`. The frontend mirrors this in its
`installFxtrExternals` call. Builtin and agent-authored modules
both compile under the same list.

**Mount helper.** Mirror `mount_basic_dynamic_dispatch`:
`mount_demo_dispatch(renderers_root) → MountedDemoDispatch(scope,
service_ref, impl)` is an async CM that owns the impl scope + task
group + initial `compile_and_register`. Caller wires
`mounted.service_ref` + `mounted.scope` into
`ExternalServiceConfig`.

- [x] Write `demo_dispatch.py` with `DemoDispatchImpl` +
      `mount_demo_dispatch`.
- [ ] Unit-test the JSON-parsing + table-inversion logic in isolation
      (no esbuild dependency — feed it fixture JSON files in a tmp
      tree, assert the resulting table shape). Skipped at
      implementation time; the live smoke test exercises the same
      code paths. Add a fixture-based test if the impl starts
      growing more validation logic.

## Step 3 — Builtin renderer

**Where:**
`projects/fxtr_workstation_demo/agent_workdir/renderers/builtin/{index.tsx,fxtr_renderer.json}`.
Ships in the repo committed; the agent README tells the agent not
to edit it.

**`index.tsx`.** Imports the existing components from
`@fxtr-ui/agent-plugin-renderer`, `@fxtr-ui/workstation-dispatch`,
`@fxtr-ui/rd-types-stream`, and exposes them under one subtype per
existing type-id from the current static registry:

```tsx
// builtin/index.tsx (sketch)
import {
  AgentMcpFullPanel,
  ScriptExecutionStreamEntry,
  InvokeResultStreamEntry,
  InvokeErrorStreamEntry,
  CallbackNotificationStreamEntry,
  WorkstationServicePlaceholderInlineLink,
  InlineLinkWithServiceSubstitution,
} from "@fxtr-ui/agent-plugin-renderer";
import {
  BasicTypeBlockLink,
  BasicTypeInlineLink,
  BlockLink, InlineLink, FullPanel,
} from "@fxtr-ui/workstation-dispatch";
import { StreamEntry } from "@fxtr-ui/rd-types-stream";

export default {
  agent_mcp_running_session: { [FullPanel.uuid]: AgentMcpFullPanel },
  log_script_execution:      { [StreamEntry.uuid]: ScriptExecutionStreamEntry },
  log_invoke_result:         { [StreamEntry.uuid]: InvokeResultStreamEntry },
  log_invoke_error:          { [StreamEntry.uuid]: InvokeErrorStreamEntry },
  log_callback_notification: { [StreamEntry.uuid]: CallbackNotificationStreamEntry },
  workstation_service_placeholder: {
    [InlineLinkWithServiceSubstitution.uuid]: WorkstationServicePlaceholderInlineLink,
  },
  basic_type: {
    [BlockLink.uuid]:  BasicTypeBlockLink,
    [InlineLink.uuid]: BasicTypeInlineLink,
  },
};
```

Note `basic_type`: data entities whose `$fxtr.type` is
`!basic_type` flow through the **normal** dispatch path (the
builtin-shortId short-circuit only fires for the type entity's own
ref). Registering `!basic_type` as a `for_type_id` in the JSON
routes them through this entry, preserving the current behavior.

**`fxtr_renderer.json`.** Strings are the hard-coded `id_string()`
values from
`@fxtr-ui/agent-plugin-renderer/AGENT_PLUGIN_TYPE_IDS`:

```json
{
  "agent_mcp_running_session": {
    "for_type_id": "db7cfuzhcj6744exem23apyoyg3ayx5rs7uvjtkjnovs37nnvm4eq",
    "dispatch_keys": ["<FullPanel uuid>"]
  },
  ...
  "basic_type": {
    "for_type_id": "!basic_type",
    "dispatch_keys": ["<BlockLink uuid>", "<InlineLink uuid>"]
  }
}
```

The frontend-side `TYPE_IDS` defense-in-depth assertion goes away
with no replacement — the strings live in only two places now
(`AGENT_PLUGIN_TYPE_IDS` on the JS side and the JSON), and any
drift surfaces immediately as "no renderer for type X" in the
frontend rather than as silent broken behavior. Good enough for a
prototype.

- [x] Write `builtin/index.tsx` + `builtin/fxtr_renderer.json`.

## Step 4 — Backend orchestration changes

**Files:** `projects/fxtr_workstation_demo/run_demo_workstation.py`,
`projects/fxtr_workstation_demo/frontend/src/server.ts`.

**Backend.**

- Open `mount_demo_dispatch(renderers_root=…/agent_workdir/renderers)`
  alongside the existing CMs.
- Add an `ExternalServiceConfig` entry naming the dispatcher
  `"renderer-dispatcher"` (matches the canonical name the
  dynamic-dispatch demo uses).
- Replace the `TYPE_IDS …` stdout line with
  `RENDERER_DISPATCHER_NAME renderer-dispatcher`. The JS side no
  longer needs to know type-id strings (the type-id-string ↔
  renderer mapping happens in `builtin/fxtr_renderer.json`).

**Orchestrator (`server.ts`).**

- Parse `RENDERER_DISPATCHER_NAME` instead of `TYPE_IDS`. Inject
  the value as `VITE_RENDERER_DISPATCHER_NAME`.
- Drop the `VITE_DEMO_TYPE_IDS` plumbing.

- [x] Update `run_demo_workstation.py` (CM nesting + stdout protocol;
      `_emit_type_ids` removed).
- [x] Update `server.ts` to parse + inject the new env var.

## Step 5 — Frontend changes

**Files:**
`projects/fxtr_workstation_demo/frontend/src/client/{main.tsx,App.tsx,dispatch_registry.tsx}`,
`projects/fxtr_workstation_demo/frontend/package.json`.

**`main.tsx`.** Add the `installFxtrExternals` block (copy the
`dynamic-dispatch-demo`'s shape):

```tsx
import { installFxtrExternals } from "@fxtr-ui/react-dispatch-dynamic-shim";
import * as agentPluginRenderer from "@fxtr-ui/agent-plugin-renderer";
import * as rdTypesStream from "@fxtr-ui/rd-types-stream";
// ...

installFxtrExternals({
  react: React,
  "react/jsx-runtime": ReactJsxRuntime,
  jotai, "jotai-effect": jotaiEffect,
  "@fxtr-ui/core": core,
  "@fxtr-ui/state-util": stateUtil,
  "@fxtr-ui/react": react,
  "@fxtr-ui/react-dispatch": reactDispatch,
  "@fxtr-ui/workstation-dispatch": workstationDispatch,
  "@fxtr-ui/agent-plugin-renderer": agentPluginRenderer,
  "@fxtr-ui/rd-types-stream": rdTypesStream,
});
```

**`App.tsx`.** Replace the static-dispatcher build with the dynamic
flow:

- Wait for the `renderer-dispatcher` service to appear in
  `connector.listServices()` (copy `waitForRecord` /
  `currentRecords` helpers from `dynamic-dispatch-demo`).
- `connector.serviceRef(record.short_id)` → `dispatchServiceRef`.
- `createDynamicDispatcher({ session, dispatchServiceRef,
  loadEntityData })` — no `overrides`, no static registry.
- Session lifecycle preserves the existing persisted-id /
  attach-vs-create dance from the current `App.tsx`.

**`dispatch_registry.tsx`.** Delete the file. The
defense-in-depth `TYPE_IDS` assertion goes away with it (see
step 3 for rationale).

**`package.json`.** Add `@fxtr-ui/react-dispatch-dynamic-shim` and
`jotai-effect` (the latter for `atomEffect` used inside
`createDynamicDispatcher`'s reload effect). The
`@fxtr-ui/rd-types-stream` workspace dep was already present.

- [x] Add packages.
- [x] Rewrite `main.tsx` + `App.tsx`.
- [x] Delete `dispatch_registry.tsx`.

## Step 6 — Agent-side `demo_dispatch` package + sample renderer

**`packages/demo_dispatch/`.** Sandbox-importable Python package the
agent uses to talk to the renderer dispatcher. Same src-layout as
the existing `presentables` / `delayed_echo` packages. Exposes both
the typed protocol (for type-checking) and a convenience
`recompile()` helper that hides the
lookup-by-external-name + schedule-invoke dance:

```python
# packages/demo_dispatch/src/demo_dispatch/__init__.py
from typing import Protocol
from fxtr2.services.typed_service import service_protocol

# External name the renderer dispatcher is registered under.
DISPATCHER_EXTERNAL_NAME = "renderer-dispatcher"


@service_protocol
class DemoDispatcherProto(Protocol):
    async def resolve(...) -> ...: ...
    async def subscribe_to_changes(...) -> None: ...
    async def recompile(self) -> None: ...


async def recompile(slot_key: str = "recompile") -> None:
    """Schedule a `recompile` invoke on the well-known
    renderer-dispatcher service. Returns once the invoke is queued
    onto the script's outgoing ops; the actual result lands on
    `slot_key` in the agent's event log.

    Read the outcome from a subsequent `run_script` via
    `retrieve_result(slot_key)`, or pass `wait_seconds=N` to the
    enclosing `run_script` to wait inline. On compile / validation
    failure the slot promotes to `invoke_error` and the previous
    renderer table stays in effect.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke
    ref = await lookup_service_by_name(DISPATCHER_EXTERNAL_NAME)
    typed = ref.replace_target_type(DemoDispatcherProto)
    await schedule_invoke(slot_key, typed, lambda d: d.recompile())
```

Agent usage:

```python
import demo_dispatch
await demo_dispatch.recompile()  # schedules the invoke
# in a follow-up script, or with wait_seconds:
result = await retrieve_result("recompile")
```

The host-side `DemoDispatchImpl` does not import this package; it
exposes the same three method names via plain `@service_method`s on
the `TypedServiceImpl[WorkstationDispatcherProto]` baseline. The
agent-side typed invoke uses the protocol class for method-shape
encoding only; on-the-wire method dispatch is by name, so the
duck-typing matches. (Comment in `DemoDispatchImpl`'s docstring
calls this out.)

**Sample renderer at `renderers/note/`.** Working renderer for the
`Note` entity from `presentables`. Subtype key is `"note"`:

```tsx
// renderers/note/index.tsx
import { BlockLink, FullPanel, InlineLink } from "@fxtr-ui/workstation-dispatch";

// Simple Note FullPanel / BlockLink / InlineLink components inline.
// Imports stay limited to externals, so esbuild compiles without
// access to node_modules.

export default {
  note: {
    [FullPanel.uuid]: NoteFullPanel,
    [BlockLink.uuid]: NoteBlockLink,
    [InlineLink.uuid]: NoteInlineLink,
  },
};
```

```json
// renderers/note/fxtr_renderer.json
{
  "note": {
    "for_type_id": "<note type id string>",
    "dispatch_keys": ["<FullPanel uuid>", "<BlockLink uuid>", "<InlineLink uuid>"]
  }
}
```

The note type id is hard-coded for the demo; the agent README walks
the human reader through how to discover the same string for a
fresh type they define (`print(MyType.fxtr_type.id_string())` from a
`run_script` cell).

- [x] Write `demo_dispatch` package (pyproject.toml +
      `__init__.py`).
- [x] Write `renderers/note/{index.tsx,fxtr_renderer.json}` with
      the note's actual id-string and subtype key `"note"`.

## Step 7 — Documentation

**`agent_workdir/README.md`.** Add a "Writing a custom visualizer"
section walking the agent through:

1. Define a Python entity type (`@dataclasses.dataclass(...,
   fxtr_type=...)`).
2. Print the type-id string from a sandbox script.
3. Create a folder under `renderers/` with `index.tsx` +
   `fxtr_renderer.json`. Point at `renderers/note/` as a worked
   example.
4. Note which dispatch keys are available — link to
   `@fxtr-ui/workstation-dispatch` for the four shell keys
   (FullPanel / BlockLink / InlineLink / HoverPreview).
5. Note which packages can be imported (the externals list — pin
   the curated set; explain that the agent generally only needs
   `@fxtr-ui/workstation-dispatch` + React).
6. Call the `demo_dispatch.recompile()` helper from a sandbox
   script:

   ```python
   import demo_dispatch
   await demo_dispatch.recompile()
   # in a follow-up script (or use wait_seconds on the run_script call):
   result = await retrieve_result("recompile")
   ```

7. Note that compile failures / JSON validation errors surface as
   an `invoke_error` on the agent's `recompile` slot; the
   previous table stays in effect, so the demo keeps working.
8. Note the **don't edit `builtin/`** rule.

**`projects/fxtr_workstation_demo/README.md`.** Update layout box +
add a one-line summary of the dynamic renderer flow under "Layout".

**`projects/fxtr_workstation_demo/agent_workdir/README.md`.** Add a
top-level pointer to the new "Writing a custom visualizer"
section.

- [x] Write the "Writing a custom visualizer" section in
      `agent_workdir/README.md`.
- [x] Update top-level `projects/fxtr_workstation_demo/README.md`.

## Step 8 — Update CODE_OVERVIEW files

- [x] `projects/fxtr_workstation/CODE_OVERVIEW.md` — note the
      protocol-signature change in step 1 and the new shape of the
      frontend's nested cache.
- [x] (No agent_plugin overview change expected — the MCP plugin
      is unaffected.)
- [x] (No top-level CODE_OVERVIEW for the demo currently — leave
      the README boxes as the source of truth there.)

## Limitations / future work

- **One-folder-per-type for sharded routing.** With step 1, the
  dispatcher can route different dispatch keys for the same type
  to different modules. But each `(type_id, dispatch_key_uuid)`
  pair must live in exactly one module — duplicates raise during
  `recompile()`. That's deliberate for the prototype; an
  alternative "last write wins" or "explicit override" semantics
  is easy to add later.
- **Per-subdir watermark = max(index.tsx.mtime,
  fxtr_renderer.json.mtime).** The watermark notices edits to
  either file, so adding a dispatch_key to a JSON file without
  touching the TSX still triggers a recompile. Edits to files
  *inside* a subdir other than these two (assets, helper TS
  modules, etc.) are NOT detected; that's acceptable for the
  prototype but easy to widen to "max over all files in subdir"
  later if needed.
- **No agent-driven type-id discovery.** The agent has to print
  the id-string from a script and copy it into JSON. A future
  pass could expose a sandbox builtin (`type_id_of(EntityCls)`) or
  let the JSON name the type via a Python module path.
- **`@fxtr-ui/rd-types-stream` externalisation.** Adding this to
  the demo's externals list ties the assembly demo's bundle to a
  specific package version of `rd-types-stream`. Acceptable for
  the prototype; revisit if we want renderer modules to embed
  stream entries themselves rather than reference host
  components.
- **No tests in `projects/fxtr_workstation_demo/`.** The package has
  no test suite (it's a demo). The Step-2 unit test for the JSON
  parsing/inversion lives alongside the impl in the same package
  but is invoked manually until/unless the demo package grows a
  CI hook.
- **Builtin source lives in the agent's writable workdir.** The
  agent _can_ edit `renderers/builtin/`. The README tells them not
  to. A stricter model (separate operator-owned dir) is easy to
  add later by passing two roots to `mount_demo_dispatch` and
  flagging the builtin one read-only at scan time.

## Implementation order recap

1. Protocol + frontend + tests: step 1.
2. Demo dispatcher impl: step 2 (+ its parsing unit test).
3. Builtin renderer files: step 3.
4. Backend / orchestrator plumbing: step 4.
5. Frontend swap: step 5.
6. Agent-side package + sample renderer: step 6.
7. Documentation: step 7.
8. Update CODE_OVERVIEW.md: step 8.

Each step is independently verifiable (run the demo at the end of
each, watch the existing event log render via builtin renderers and
then the agent-authored renderers).
