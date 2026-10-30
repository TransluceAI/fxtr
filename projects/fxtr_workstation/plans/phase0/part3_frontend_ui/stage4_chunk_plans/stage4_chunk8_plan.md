# Stage 4, Chunk 8 — Demo: invoke

Extend chunk 7's demo with service-invoke mechanics. Each panel gets buttons to allocate `InvokeOp`s against the demo workstation's `demo-service` (the `echo` / `combine` / `fail` methods registered by `demo_workstation.py`). Result slots accumulate in the panel's user state and render through the same `OpResult` display we built for the load case.

The high-level chunk description lives in [`stage4_react_bindings.md`](./stage4_react_bindings.md).

## Scope

In:

- Resolve the demo service in `App`: subscribe to `connector.listServices()`, find the record whose `external_names` contains `"demo-service"`, build a `ServiceRefStub` via `connector.serviceRef(shortId)`. Wrap in an `InvokeTarget` shape and thread down to each `Panel`. Null until the service list lands.
- Rename `LoadedDisplay` → `OpResultDisplay` and parameterize with a label prop so it serves both load and invoke. Keep it in `Panel.tsx`.
- Extend the panel's user-state shape to `{ message?; loaded?; invokes? }`, where `invokes` is a list of `{ label: string; stub: OpResultStub<unknown> }` records — one per invoke clicked, in chronological order.
- Add three "invoke" buttons to each panel: `echo(42)`, `combine("v")`, `fail("oops")`. Each writes a new entry into `invokes` via a ctx-style updater that calls `ctx.allocateInvoke(svc.ref, method, args, kwargs?)`. A "clear" button empties the list.
- Render the `invokes` list under the panel's load section, one `<OpResultDisplay label="invoke echo" />` per entry.

Out (deferred):

- Picking arbitrary methods / args from the UI. Hardcoded calls keep the chunk focused.
- Typed service protocols on the `serviceRef` (the demo uses the untyped `ServiceRefStub<unknown>` flavor). Adding `_DemoServiceProto` on the TS side would be straightforward but doesn't exercise anything new.
- Cleaning up older invoke slots from state (they stay until the user hits "clear" or detaches). The workstation will preserve their fills as long as they're referenced from state, which is the design intent.

## Design decisions

### Invoke results live in an array, not a single slot

Loads are dedup'd (one wire load shared across panels for the same short_id) so a single slot per panel is fine. Invokes are explicitly *not* dedup'd — every `allocateInvoke` mints a fresh slot, every click is a new server-side call. Showing all of them in order makes the no-dedup semantics visible to the user; collapsing to one slot would lose that signal.

### Reuse `OpResultDisplay` for both load and invoke

The `pending → filled → errored` render is identical; only the label differs. Renaming `LoadedDisplay` → `OpResultDisplay` and adding a `label` prop is the smallest change.

### `invokes` is `ReadonlyArray<{ label, stub }>` rather than just stubs

The label captures which method was called for display purposes. Could be derived from the stub at allocate time (the stub doesn't carry method info post-allocation), so we record it alongside.

### Stable React keys

Use the stub's slot integer as the React key — slots are session-wide monotonic and unique, so they're stable across re-renders. Read via `(stub as WorkstationOpResultStub).slot` cast.

## Implementation steps

1. [x] In `App.tsx`: subscribe to `connector.listServices()`, find the demo-service record, build a `ServiceRefStub`. Pass `invokeTarget: InvokeTarget | null` to each `Panel` alongside `loadTarget`.
2. [x] In `Panel.tsx`: rename `LoadedDisplay` → `OpResultDisplay`, add `label: string` prop, use it instead of the hardcoded "load:" string.
3. [x] Extend `PanelUserState` to include `invokes?: ReadonlyArray<{ label: string; stub: OpResultStub<unknown> }>`.
4. [x] Add the three "invoke" buttons; each calls a handler that appends `{ label, stub: ctx.allocateInvoke(...) }` onto `invokes`. Disabled when `invokeTarget === null` or `lifecycle !== "connected"`.
5. [x] Render the invokes list below the load section. One `<OpResultDisplay key={slot} label={label} stub={stub} />` per entry. Add a "clear invokes" button that sets `invokes` to `[]`.
6. [x] `pnpm check && pnpm lint` green. Manual smoke pending — user-driven.
7. [x] Update `frontend_lib/CODE_OVERVIEW.md` `@fxtr-ui/demo` section.

## Notes from implementation

- `PanelUserState` is a nominal TS interface (no index signature) and TS doesn't auto-widen it to `StructuredValue`'s `{ [key: string]: StructuredValue }` arm even though every field's value IS a StructuredValue. The fix at the updater's return site is `return next as unknown as StructuredValue`. Adding an explicit `[key: string]: StructuredValue` index sig to `PanelUserState` would also work but pollutes the type with `| undefined` problems on the optional fields; the cast is less noisy for a demo.
- `allocateInvoke`'s typed signature wants `MethodArgs<P, M>` / `MethodKwargs<P, M>`. Since the demo uses `ServiceRefStub<unknown>` (untyped), those helpers widen to `readonly StructuredValue[]` and `{ [k: string]: StructuredValue }`. Casting our spec.args / kwargs through `readonly never[]` / `Readonly<Record<string, never>>` walks through the loose overload variant. A real consumer with a typed service protocol wouldn't need these casts.

## What chunk 9 will do

React-layer tests covering Provider hydration, multi-panel isolation, the ctx-updater path through real Providers, and `subAtom` end-to-end. Also revisits the runtime tests added piecemeal in chunks 2–4 for any gaps.
