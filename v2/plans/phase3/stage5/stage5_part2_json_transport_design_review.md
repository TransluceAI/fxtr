Feedback provided by Claude, with resolve intent specified by Daniel.

## Major Issues

1. Positive ID counters: the state section describes one counter but you need two

The state section lists _next_positive_id: int as a single counter, but you need two
independent counters:
- Next positive import ID (incremented when we send push/submit)
- Next positive export ID (incremented when we receive push/submit)

In Cap'n'Web, these are implicitly this.imports.length and this.exports.length — two
separate arrays growing independently. Side A sending a push increments A's import counter
but not A's export counter. Side B receiving that push increments B's export counter but
not B's import counter. If B also sends a push, B's import counter increments
independently.

The prose in the design doc actually gets this right ("For outgoing pushes/submits, we
allocate the next positive import ID. For incoming pushes/submits, we allocate the next
positive export ID") but the state field is singular and could be misread. This needs to
be two fields in the implementation.

**How to Resolve:** Update design to add two counters and describe them.

2. No rollback mechanism for partial expression encoding failures

Cap'n'Web's Exporter interface includes unexport(ids: Array<ExportId>) specifically for
this case: if the Devaluator exports some handles into the export table but then hits an
error encoding a later part of the value, it rolls back all exports created during that
encoding pass (serialize.ts:92-101).

Your design says expression encoding is synchronous and happens inside the send lock, but
doesn't address what happens if encoding raises an exception partway through. If you've
already created export entries for some handles in the value, those entries are now
orphaned — the remote side will never see them, and they'll never be released.

You need either:
- Track exports created during encoding and roll them back on failure (like Cap'n'Web), or
- Accept the leak and document it as a known limitation (not recommended)

**How to Resolve:** Update design to add a rollback mechanism for exports created during encoding

3. Resolve/reject arriving for already-released imports (messages crossed in flight)

Cap'n'Web explicitly handles this case (rpc.ts:803-821):
if (imp) {
    // normal resolve
} else {
    // Import already released. Still evaluate to handle side effects:
    if (msg[0] == "resolve") {
        new Evaluator(this).evaluate(msg[2]).dispose();
    }
}

The resolve expression must still be evaluated even for a released import because the
expression might contain ["export", id] expressions that create new import entries. If you
skip evaluation, you'd leak those exports on the sender's side (they expect the recipient
to track the IDs and eventually release them). After evaluation, the result must be
disposed (discard all created imports/handles).

Your design mentions "re-introduction" for the case where release crosses with a new
export expression, but doesn't address the case where release crosses with resolve/reject
for the same import.

**How to Resolve:** Make a note to evaluate and then dispose results that arrive if the import is a promise that has already been released.

4. as_handle() teardown semantics differ from local caps

This is more of a design decision to document than a bug, but it has test implications.
LocalPendingHandle.wait_until_teardown() has a two-phase behavior:
1. Wait for the future to resolve, verify it's an RPCHandle
2. Wait for the resolved handle's wait_until_teardown()

Your design has JSONTransportPending.as_handle() return a plain JSONTransportHandle
pointing at the same import entry. This handle's wait_until_teardown() only fires on
teardown/broken message, connection abort, or discard. For push result imports,
teardown/broken messages are never sent. So wait_until_teardown() on a pending-handle
created from a push result would only ever fire on abort or discard — it never follows
through to the resolved value's lifetime.

This means tests like test_pending_handle_teardown_propagates_handle_discard will not pass
for the JSON transport. You'd either need:
- A JSONTransportPendingHandle class that mirrors LocalPendingHandle's two-phase teardown,
or
- To exclude those tests from the JSON transport parameterization and document this
behavioral difference

I'd recommend at minimum documenting this as a known divergence and excluding those
specific tests for now.

**How to Resolve:** Actually, ideally, we should change the behavior here so that `.as_handle().wait_until_teardown()` does follow through to the resolved value's lifetime. Implies that `as_handle` should pull and then check for it to resolve to a `JSONTransportHandle`, similar to the `LocalPendingHandle`, and different from normal `JSONTransportHandle`. This can be a separate `JSONTransportPendingHandle`.

## Medium Issues

5. Release timing: Cap-IR holds imports longer than Cap'n'Web

In Cap'n'Web, when a push result resolves, ImportTableEntry.resolve() calls
this.sendRelease() immediately (rpc.ts:88). After that, all operations on the import
delegate to the resolution StubHook locally — no more wire traffic through that import ID.

In your design, release is deferred until all local clones are discarded. This means:
- The server holds the export entry (and its value) longer than necessary
- dispatch() on a resolved JSONTransportPending still sends wire messages referencing the
old export, even though the value is already known

This is correct but less efficient. Cap'n'Web's approach of short-circuiting to the local
resolution after resolve is an optimization you could add later. Worth documenting as a
known difference.

**How to Resolve:** Once a push result or other pending value resolves, we can dispatch to it locally. So, we should go ahead and release pending values as soon as we receive a resolve, matching Cap'n'web, and make sure we can dispatch locally.

6. Connection abort cleanup ordering needs to be precise

The abort section lists steps but doesn't specify ordering constraints. The order matters:

1. Set _aborted = True (prevent new sends)
2. Best-effort send ["abort", error_expr]
3. Reject all import resolution futures (so pending pull() calls wake up)
4. Fire all import teardown events (so wait_until_teardown() calls wake up)
5. Cancel all export watcher tasks
6. Discard all exported handle/pending clones
7. Close the stream

If you fire teardown events (step 4) before rejecting futures (step 3), a
wait_until_teardown() implementation that checks the resolution state might see an
inconsistent state. I'd recommend specifying this order explicitly in the design doc.

**How to Resolve:** Specify this concretely.

7. ObjectStream helper needs more implementation detail

The design says "Creates a local handle (e.g. using LocalRPCContext + LocalHandle) with a
send(value) method that puts values into an internal receive queue" but doesn't specify:
- Who owns the LocalRPCContext? (The helper must own it and enter/exit it with the
transport)
- How is the receive queue bounded? (Unbounded could cause memory issues; bounded provides
backpressure)
- What happens when the connection aborts while the app is blocked on receive()?
- How are handles/pendings in received values managed? (They reference the transport's
import table — if the context exits, are they still valid?)

This is probably fine to figure out during implementation but worth noting.

**How to Resolve:** Fine to leave this underspecified for now, we will refine this part of the design in a later stage. Make a note that we should delay implementation and that edge cases should be considered before implementing this part.

## Minor Issues / Observations

8. Promise chaining optimization

Cap'n'Web's ensureResolvingExport() has a loop that skips intermediate resolves
(rpc.ts:433-448): if a push export's result is itself a promise (and not pointing back at
the peer), it follows the chain and waits for the final resolution rather than sending a
redundant intermediate resolve. Not required for correctness but a nice optimization to
note for later.

**How to Resolve:** Plan to make a comment noting this as a future optimization.

9. Reject expression validation

The PROTOCOL.md says reject expressions "are not permitted to contain stubs." The
implementation note mentions verifying this, but the design doesn't specify what the
encoder should do — throw? Strip stubs? In practice, Python exceptions shouldn't contain
handles, but a defensive check (or at least an assertion) would be good.

**How to Resolve:** Add a note to add a check for this when receiving a reject expression.

10. Prototype pollution in object expression evaluation

Cap'n'Web strips keys matching Object.prototype properties (__proto__, toString, toJSON,
etc.) from evaluated objects (serialize.ts:814-826). This is less critical in Python (no
prototype chain), but dict keys like __class__ or __dict__ could theoretically cause
confusion. Probably fine to skip but worth a brief note.

**How to Resolve:** Actually this should be fine. Validation should already be performed before the value turns into an `IRValue[JSONLeaf]`. No need to take any action.

11. Idempotent cleanup with released flag needs care

Implementation note 3 describes the race between release and teardown for handle exports.
The check-then-set pattern on released needs to be correct under concurrency. Since
Python's GIL makes individual attribute accesses atomic, a simple if self.released:
return; self.released = True should work, but it's worth being explicit that this is safe
because both release processing and teardown watcher run in the same task group (never
truly concurrent in anyio's cooperative model).

**How to Resolve:** Should be fine. Doesn't really need to be in the same task group either, the tasks will cooperate as long as there aren't any `await` between the check and the set.


## Things the Design Gets Right

- The two-layer architecture (core transport + ObjectStream helper) is clean and testable
- The send lock for ensuring ID allocation matches wire order is correct and necessary
- Clone-on-export is the right approach for decoupling export lifetime from application
references
- The teardown-implies-release simplification is well-reasoned
- The decision to deliver promises as RPCPending without awaiting resolution is a good
simplification
- The reverse export lookup with id() stability (kept alive by the export clone) is sound
- The import type table (handle/push/submit/promise with their behavioral flags) captures
the distinctions cleanly
