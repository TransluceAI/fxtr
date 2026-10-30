# JSON Transport — Design Notes

Key findings, subtleties, and things to pay attention to from studying the Cap'n'Web protocol
and implementation. These notes are for reference during later planning stages.

Cap'n'Web is available at /Users/daniel/extgit/capnweb .

## Refcounting is critical and subtle

Cap'n'Web's refcounting solves a real race condition. The scenario:

1. Side A exports handle H as export -1, sends it in a message to B.
2. B receives it, creates import -1. B uses H, then decides to release.
3. B sends `["release", -1, 1]`.
4. **But**: while that release was in flight, A sent H *again* in another message (as
   `["export", -1]`), incrementing A's refcount to 2.
5. A receives the release with refcount=1. A decrements from 2 to 1 — still alive.
6. B receives the second export. B must recognize that the import was "re-introduced" and
   re-create its stub for import -1.

The implementation detail: `ImportTableEntry` has both `localRefcount` (number of local
references/clones) and `remoteRefcount` (number of times the ID was introduced via
export/promise expressions or push). When releasing, the `remoteRefcount` is sent. The exporting
side decrements by this amount and only actually frees when its count reaches zero.

**For our implementation**: We need to handle the case where we've sent a release for an import
but then receive a new export for the same ID. The import table entry might have been deleted, so
we need to re-create it. This is the "re-acquire" scenario the user mentioned.

## The import/export expression distinction matters

- `["import", id]` → evaluates to a handle (stub). Delivered to the app immediately as-is.
- `["pipeline", id]` → evaluates to a pending value. Must be resolved before delivery.

When either includes a `propertyPath` and/or `callArgument`, the semantics extend:
- `["import", id, path, arg]` → call, result treated as a *resolved* value (not a promise)
- `["pipeline", id, path, arg]` → call, result treated as a *promise* (needs resolution)

In Cap'n'Web's implementation, the distinction controls whether the result is added to the
`promises` list (which get auto-resolved before the payload is delivered) or the `hooks` list
(which get delivered as stubs).

For us, this maps to: `import` expressions produce `RPCHandle` values, `pipeline` expressions
produce `RPCPending` values.

## How push/pipeline calls are processed on the receiving end

When the server receives `["push", ["pipeline", 5, ["method"], arg_expr]]`:

1. The push creates a new export entry (the next positive ID) for the result.
2. The `pipeline` expression is evaluated:
   - Look up export 5 (the target)
   - If export 5 has a pending result that hasn't resolved yet, the call is queued
   - Once it resolves, `method(arg)` is called on the resolved value
3. The result goes into the new export entry as a `PayloadStubHook`.
4. No resolve is sent yet — the server waits for a `pull`.
5. When `pull` arrives, the server starts resolving: serializes the result and sends `resolve`.

**Key insight**: The `push` export starts with a hook that *contains* the computation. The
computation may already be in progress (async). When `pull` arrives (via `ensureResolvingExport`),
the server awaits the hook's `pull()` method, which returns when the computation completes. This
means the server doesn't need a separate "pending computation" tracker — the hook itself is the
pending computation.

**For our implementation**: When we receive a push with a call expression, we should dispatch on
the local handle and store the result in the export entry. The export entry holds an `RPCPending`
(or equivalent) that resolves when the computation completes. When pull arrives, we await the
resolution and send `resolve`. Pipelined calls on the export dispatch through the pending.

## Pull behavior varies by how the import was created

There are three ways an import (pending value) can be created, and they have different pull
behavior:

| Import source | Requires explicit `pull`? | Auto-released? |
|---|---|---|
| `push` result | Yes | No (must send `release`) |
| `submit` result | No (auto-pulled) | Yes (auto-released after resolve/reject) |
| `promise` expression | No (auto-pulled by sender) | No (must send `release`) |

This matches Cap'n'Web exactly (with `submit` = Cap'n'Web `stream`).

**For push results**: The recipient sends `["pull", importId]` when `pull()` is called on the
`RPCPending`. On the export side, `ensureResolvingExport` starts the resolve chain when `pull`
arrives. If the result is already available, resolve is sent immediately. If not, resolve is sent
when the computation completes. If pull is never sent, the result is computed but never sent back
(though pipelining still works).

**For promise expressions**: The sender calls `ensureResolvingExport` immediately when exporting.
The recipient does not need to send `pull`. This is important because promise expressions appear
embedded in values — the recipient may not even know which promises are in a value until it
evaluates it, and requiring pull for each would add latency.

**For submit results**: Auto-pulled and auto-released. This is the efficient path for
`dispatch_and_wait` where the caller just wants the result with no pipelining.

## Submit = stream with a different name

Cap'n'Web's `stream` message and our `submit` are functionally identical:
- Creates the next sequential positive import ID
- Auto-pulled (no separate pull needed)
- Auto-released (refcount=1) after resolve/reject

In Cap'n'Web's implementation, stream imports have `remoteRefcount = 0` (so resolve doesn't
send a release) and `localRefcount = 1` (so resolve doesn't treat as disposed). The import entry
is cleaned up manually after the resolution is received and the payload is disposed.

## Cap'n'Web evaluates expressions eagerly but resolves lazily

When a `push` message arrives, the expression is evaluated immediately (synchronous). This means:
- All `export`/`promise` expressions create entries right away
- All `import`/`pipeline` expressions look up entries right away
- Any calls in the expression start executing immediately

But the *result* of the push export is not resolved until `pull` is sent. This means multiple
pushes can be in flight concurrently, each with a pending result.

## Abort handling

When one side experiences a fatal error:
1. It sends `["abort", error_expr]` (best-effort).
2. It rejects all outstanding imports (setting them to an error state).
3. It disposes all exports.

The other side, upon receiving `abort`, does the same. Connection is fully dead after abort.

For us, this maps to: all `wait_until_teardown` waiters get `RPCResourceBrokenError`, all
`pull()` calls get rejected, all pending `dispatch_and_wait` calls get rejected.

## The export reverse-lookup optimization

Cap'n'Web maintains a `reverseExports: Map<StubHook, ExportId>` that maps hooks to their
export IDs. This enables re-export: when serializing a value that contains a handle we've already
exported, we can reuse the same export ID (incrementing refcount) instead of creating a new one.

**Important**: promises always get new IDs even if the hook was exported before. This is because
"the recipient could miss the resolution" if an existing ID were reused.

For our implementation: we'll want a similar reverse lookup. When encoding an `IRValue` that
contains an `RPCHandle` we've already exported, we reuse the export ID. When encoding an
`RPCPending`, we always create a new export ID.

## The `import` expression with calls references OUR exports, not their imports

This confused me initially. When the sender writes `["import", 5, ["method"], arg]`, the `5` is
an import from the **sender's** perspective. But in the Evaluator (which runs on the
**recipient**), it calls `this.importer.getExport(value[1])` — looking up 5 in the **recipient's
export table**.

This makes sense: "import" from the sender = "export" from the recipient. The sender is
referencing something the recipient owns. So when evaluating, the recipient looks up its own
exports.

For our implementation: the Evaluator's `importStub(id)` creates a new import (the sender
exported something to us), while `getExport(id)` looks up an existing export (the sender is
referencing something we own). Import/pipeline expressions use `getExport`, while export/promise
expressions use `importStub`/`importPromise`.

## Teardown is new territory

Cap'n'Web has no concept of proactive teardown notification. When a handle goes away, the
connection either breaks (abort) or the handle is released (release). There's no way to say
"this handle is shutting down normally, here's why" while keeping the connection alive.

Our `teardown` and `broken` instructions fill this gap. Important design decisions:

- **Teardown doesn't release**: After sending teardown, the export is still alive. The recipient
  still needs to send release when it's done with the handle. Teardown just notifies the
  recipient's `wait_until_teardown()` waiters.
- **Only for handles**: Push results and promise exports don't send teardown. They resolve/reject,
  and that's it. Teardown is for long-lived handle exports that the recipient may be monitoring.
- **Who sends teardown?**: The exporter sends teardown when the local handle tears down. This
  means we need to watch exported handles for teardown events and translate them into protocol
  messages. The `wait_until_teardown()` machinery exists on local handles, so we start a watcher
  task when exporting a handle.

EDIT (by Daniel): Considering an alternative protocol where any value (handle or promise) can send teardown, and the
recipient no longer needs to send release if the value has torn down (and edited protocol file to
match). Under this version, teardown is essentially informing the recipient that every future access
to this object will be rejected. Exporter would be allowed to release the object preemptively as
long as the exporter tracks that it should reject future access. Importer might have messages in
flight that refer to the given export, but these will fail when delivered for an object that has
torn down. Even if teardown message was not sent, a remote object could fail requests if its actual
implementation went down; teardown is an informative message to tell the recipient that this has
occurred, rather than a negotiation process requesting graceful exit (which should happen via a
different mechanism)

## Single callArgument vs array of args

Cap'n'Web uses an array of argument expressions (JS functions take multiple positional args). We
use a single expression. This is a meaningful wire-level difference:

```
Cap'n'Web:  ["pipeline", 1, ["method"], [arg1_expr, arg2_expr, arg3_expr]]
Cap-IR:     ["pipeline", 1, ["method"], arg_expr]
```

Where `arg_expr` evaluates to a single IRValue. If the handle interprets this as positional args,
it's because the IRValue is a list. If keyword args, it's a dict. If no args, it's null/None.
This is purely the receiving handle's convention, not the protocol's.

This means encoding a call like `method(1, 2, 3)` (three positional args) requires:
- Cap'n'Web: `["pipeline", 1, ["method"], [1, 2, 3]]`
- Cap-IR: `["pipeline", 1, ["method"], [[1, 2, 3]]]` (double-nested: the expression for [1,2,3])

And `method()` (no args):
- Cap'n'Web: `["pipeline", 1, ["method"], []]`
- Cap-IR: `["pipeline", 1, ["method"], null]`

## ObjectStream is built on the main interface, not a custom instruction

Originally we considered adding a `["send", expr]` instruction, but this is an unnecessary
deviation from Cap'n'Web. Instead, the ObjectStream is implemented as a convention on top of the
main interface (ID 0): both sides expose a handle with a `send` method, and calling
`send(value)` on the remote main interface delivers values to the application.

This means `stream.send(value)` becomes `["submit", ["pipeline", 0, ["send"], value_expr]]` on
the wire — a standard submit call on the main interface. No custom protocol messages needed.

This is cleaner because it keeps the core protocol identical to Cap'n'Web (minus our documented
changes), and the ObjectStream behavior is purely application-level.
