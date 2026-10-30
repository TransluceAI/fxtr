# 03 — Stub type semantics

Builds on 01 + 02. Pins down what each of the four stub types does
and how they're identified, typed, and resolved.

## Stubs are session-bound, with inner reactive atoms

A stub instance is more than an immutable identity — it's a small
session-aware object that exposes **inner atoms** for reading its
live state. This is what makes them fit jotai-style projections:
you don't need to manually thread paths through `stub_results`;
you ask the stub for its result atom, and it knows how to derive
that from its owning session's source-of-truth atom.

```ts
// public interface
class OpResultStub<T> {
  readonly result: Atom<ResultStatus<T>>;   // pending | filled | errored
  readonly committed: Atom<boolean>;        // server has ack'd the mutation
                                            // that introduced this slot
  readonly done: Atom<boolean>;             // result has arrived (filled
                                            // or errored)
  // ... more derived atoms as needed
}

// connector-specific impl
class WorkstationOpResultStub<T> extends OpResultStub<T> {
  readonly slot: number;          // the internal slot number of the op result
  readonly spec: OpSpec;          // load / invoke / etc, used at diff-time
}
```

The atom layer is what `splitAtom`-style projections compose with:
`get(slot_atom).result` (or `get(get(slot_atom).result)` if reading
from a parent atom containing the stub) gives you the live result
without any explicit path threading.

Connectors usually materialize new stubs via `allocateX` or wire
decodes. Connectors are free to **intern** stubs per session for
values that are known to be identical, in particular for
`EntityRefStub`, `ServiceRefStub`, and for an `OpResultStub` that
corresponds to an entity load (which is always deterministic).

## The four stub types

### `EntityRefStub<T>`

- Workstation impl identity: a workstation short_id (or, for HTTP, a fxtr id-string).
- **Wire form**: `["entity", short_id]` (matches the existing
  `wire_escape.py` tag).
- **Type erased**: `T` is used only for typechecking. `T` represents
  the shape of the data when the entity is loaded. Load logic and
  wire operations use an unknown value. (This is distinct from how
  EntityRef is typed in Python, which stores a type in runtime state.)
- **Loading**: `session.allocateLoad(stub)` returns an
  `OpResultStub<T>` for the load. Storing that into state fires
  the fetch. The session dedups: a second `allocateLoad` for the
  same short_id whose existing slot is still preserved returns
  the *same* `OpResultStub`. See
  `05_dispatch_and_inline_loading.md`.
- **Inner atoms**: probably none in v1 beyond what the session
  exposes via dedup. Accessing entity data requires allocating a load
  and binding it to a slot in the state.

### `ServiceRefStub<T>`

- Workstation impl identity: a workstation service short_id.
- Optional type tag `T`: a type-level-only service protocol definition that
  lets the consumer call methods with type checking.
- **Wire form**: `["service", short_id]`.
- **Workstation-only**: HTTP sessions can't construct these.
- **Invoking**: `session.allocateInvoke(stub, "method", args...)` →
  `OpResultStub<R>`. Each invoke always creates a _new_ stub, corresponding
  to a _new_ call to that method.
- **Inner atoms** (TBD): probably none initially. Possible future
  additions: `liveness: Atom<boolean>` if we want to surface
  workstation-level service-record changes (e.g., the underlying
  registration was released) to consumers reactively.

### `OpResultStub<T>`

- Workstation impl identity (in-memory): a slot integer + the originating op spec
  the reducer needs at commit-diff time (`{ kind: "load", short_id }`
  or `{ kind: "invoke", svc_id, method, args }` etc.). The op spec
  is consumed when the stub first appears in state, to emit the
  matching protocol op; it doesn't need to be on the wire after
  that.
- **Wire form**: `["op_result", slot]`. Just the slot. No op-spec
  summary on the wire — if a user wants debug info next to a slot,
  they store it adjacent in their state tree (e.g.
  `state.foo = { slot, label: "loading note" }`). The workstation
  already exposes in-flight metadata via `subscribe_session_debug`.
- **Inner atoms**:
  - `result: Atom<{ status: "pending" | "filled" | "errored",
    value?: T, error?: string }>` — derived from the session's
    local `slotFills` map, which itself is derived from preserved
    fill events on the wire (see `04_session_state_layout.md`).
  - `committed: Atom<boolean>` — the mutation that first
    introduced this slot has been ack'd by the server.
  - `done: Atom<boolean>` — convenience for `result.status !==
    "pending"`.
- **Type tag `T`** is type-level bookkeeping.

Note that the HTTP impl will likely store different data here, while still
exposing the same session-level interface.

### `CallbackInboxStub<R>`

- Workstation impl identity: `(slot, offset)`. The slot is the receive-side slot of
  the workstation callback; the offset is "how many events from
  this slot's history this stub has consumed."
- **Wire form**: `["inbox", slot, offset]`.
- **Workstation-only.**
- The slot's full event history lives in a per-slot append log
  managed by the session (see `04_session_state_layout.md`).
  Multiple inbox stubs can coexist in state with different
  offsets, each acting as an independent read cursor.
- **Inner atoms**:
  - `events: Atom<R[]>` — the events from `offset` onward.
  - `state: Atom<"opening" | "open" | "closed">` — overall
    callback state.
  - `committed: Atom<boolean>` — server ack'd the create.
- Cursor manipulation produces *new* stub values:
  - `inbox.sliced(n)` → new inbox stub with `offset` advanced by n.
  - `inbox.cleared()` → new inbox stub at the current end-of-log.
  - The user commits the new cursor by writing it back to its
    state location, optionally bundling their own reduced state for
    the consumed events into the same mutation.
- The session can drop events from the per-slot log once the
  minimum cursor across all live inboxes for that slot has passed
  them (driven by `ack_seq` advances on the wire).

#### Why `(slot, offset)` and not just `slot`

- Lets the user keep an inbox-shaped value in state that shifts as
  events are consumed, without the underlying slot changing. The
  slot identifies the workstation callback; the offset identifies
  this reader's progress.
- Naturally supports multiple readers (e.g., a panel showing the
  full history alongside a panel that only cares about the latest
  event) by allowing multiple inbox stubs with different offsets
  in different parts of state.
- Closure (next section) maps to "remove all inbox stubs for this
  slot from state."

### Closing a callback

Just removing the inbox stub from state. The reducer detects "all
inbox stubs for slot N are gone" in the diff and emits
`CloseCallbackOp(slot=N)` as part of the mutation. There is no
synchronous close API on the stub; closure flows through state
mutation like everything else.

## The "send-side callback ref" tangent

`wire_escape.py` already has `["callback", slot]`. That's how a
caller embeds the *send* side of a workstation callback into invoke
args — i.e. "here's a service ref the callee can hold and notify
into." The send side is a bridged `ServiceRef[EventListenerProto]`
on the workstation side; the receive side is the slot we own.

In our model:
- The user calls `session.allocateCallback()` to get a
  `CallbackInboxStub<R>` (the receive side). They store this into their
  own state in order to get access to the callback's results.
- They pass it as an argument to an `allocateInvoke(...)` call. The
  reducer translates this into the wire `["callback", slot]` arg.
- The callee notifies; the workstation routes the notify to a
  `CallbackEvent` in our session; the inbox grows.

So we don't need a separate "send-side" stub type — the inbox stub
*is* the entity, and embedding it in invoke args means "here, take
the send side."

## Open questions

### OQ-α — Service ref invoke argument types

When you `allocateInvoke(svc, "method", args)`, the type of `args`
should be inferred from the service protocol `T`. This is a
non-trivial TS exercise. We'll likely use a service-protocol
declaration shape similar to v2's `service_protocol`-decorated
interfaces but adapted for runtime metadata.

Plausible shape:

```ts
const MyServiceProto = serviceProto({
  foo: method({ args: [str(), childRef(MyEntity)], returns: int() }),
  bar: method({ args: [], returns: stream(MyEvent) }),
});
type MyService = ProtoOf<typeof MyServiceProto>;
```

Then `allocateInvoke(svc.as(MyServiceProto), "foo", [s, ref])` is
type-checked against the proto.

### OQ-β — Service ref with method dot syntax

Should we offer `svc.foo(...)` ergonomics? Like
`(await session.invoke(svc.method('foo'), ...))`. The invoke is
still a slot allocation, not a synchronous call. We could expose:

```ts
const slot = svc.method("foo").call(s, ref);   // OpResultStub<int>
state.fooResult = slot;
```

That's nicer than `session.allocateInvoke(svc, "foo", [s, ref])`.
Probably worth it but adding it after the basic plumbing.

### OQ-γ — Stub instance identity / interning

Stubs are immutable, but should multiple references to the same
short_id be the same instance? Pros: cheap referential equality,
trivial set membership for refcounting. Cons: needs interning
infrastructure per session.

Default: yes, intern per session. Connector maintains a weak map
short_id → stub instance.
