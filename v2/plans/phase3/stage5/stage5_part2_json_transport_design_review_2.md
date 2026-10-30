Feedback provided by Claude, with resolve intent specified by Daniel.

## Major Issues (should address before implementation)

1. Missing evaluation rollback (receiver-side cleanup on expression failure)

The design discusses encoding rollback (lines 369-377) — cleaning up export entries when
expression encoding fails. But it doesn't discuss evaluation rollback — cleaning up import
entries when expression evaluation fails on the receiver side.

Cap'n'Web handles this in Evaluator.evaluate() (serialize.ts:473-481):
try {
    payload.value = this.evaluateImpl(value, payload, "value");
    return payload;
} catch (err) {
    payload.dispose();  // disposes all hooks (imports) created during evaluation
    throw err;
}

In Cap-IR, if evaluating a push/submit expression fails partway through (e.g., an
["export", id] created an import entry, then a subsequent sub-expression fails), the
already-created import entries are orphaned — the remote side holds the corresponding
export entries that will never be released.

The evaluator should track all import entries created during a single evaluation pass. On
failure: discard all created handles/pendings. For push/submit context, create a rejected
export entry. This mirrors the encoding rollback mechanism.

**How to fix:** Add this to the plan for the API implementation, mirroring the encoding rollback.

2. Under-specified behavior for ["import", id] (no path, no call) when export value is not
an RPCHandle

The PROTOCOL.md says ["import", id] evaluates to an RPCHandle. The API design says
"Produce RPCHandle" but doesn't specify what happens when the export's value is NOT an
RPCHandle — e.g., it's an RPCPending (push result), a plain value (dict/list), or an
exception.

Cap'n'Web calls hook.dup() which always works regardless of underlying type (it
deep-copies the value and wraps it as a stub). Cap-IR needs explicit rules because
RPCHandle and RPCPending are distinct types:

- Export value is RPCHandle → clone it.
- Export value is RPCPending → call as_handle().
- Export value is a plain IRValue → this is unusual. Error, or wrap?

The design should specify the behavior for each case, or at minimum say that ["import",
id] without path/call is only valid when the export's value is an RPCHandle or RPCPending,
and any other case is a protocol error.

**How to fix:** Add a brief note in the plan. Only RPCHandle or RPCPending are supported, it is a user error to try to import something that is not a handle / does not resolve to a handle.

3. Encoding depth limit

Cap'n'Web limits encoding/decoding depth to 64 (serialize.ts:107-109) to prevent stack
overflow from cyclic or deeply-nested structures. The design doesn't mention this. The
encoder and evaluator should both enforce a depth limit.

**How to fix:** Skip this in initial implementation but make a note at the bottom of the plan.

---
## Moderate Issues (good to address, or document as known limitations)

4. Handle/pending lifecycle in call arguments during expression evaluation

When the evaluator processes ["pipeline", id, path, arg_expr], evaluating arg_expr may
create import entries (via ["export", ...] sub-expressions). These become
JSONTransportHandle objects inside the call argument IRValue. The call argument is passed
to the export's dispatch().

If the dispatch fails, or the called function discards the argument without explicitly
discarding the handles, the import entries leak (the remote side holds exports that are
never released). This is bounded by connection abort (which cleans up everything), but
it's a steady-state leak for long-lived connections with failing calls.

Cap'n'Web handles this via the payload ownership model: deliverCall disposes the argument
payload in a finally block (core.ts:1163-1191). Cap-IR doesn't have a payload model.

Options:
- Track handles created during argument expression evaluation; clean up after dispatch
completes if not captured by the result.
- Add __del__ on JSONTransportHandle as a GC safety net.
- Document as a known limitation for v1.

**How to fix:** (!) fundamentally this likely needs a redesign to account for lifetime management! discussion ongoing.

5. Export cleanup: watcher cancellation vs. idempotent flag

The design mentions idempotent export cleanup (impl note 3) for the race between release
messages and teardown watcher firing. Consider being more explicit: when a release message
arrives and the export's refcount reaches 0, the teardown watcher task should be
cancelled directly (not just relying on the idempotent flag and waiting for the watcher to
discover the discarded clone). Cancellation is more deterministic and avoids the watcher
waking up, checking a flag, and silently exiting.

6. Evaluation failure scoping needs more precision

The design says:
- push/submit: rejected export
- resolve: rejected import
- other contexts: abort

But what about failures in:
- teardown expression evaluation → fire teardown with error? abort? The teardown
expression could contain ["export", ...] which creates imports.
- broken expression evaluation → same question.
- abort expression evaluation → already aborting, so probably best-effort.

Cap'n'Web aborts on any evaluation failure (bad message → throws → abort). The more
lenient approach in Cap-IR is better, but needs to explicitly cover teardown/broken
expression failures.

---
## Minor Issues / Suggestions

7. remote_refcount correctness — Cap'n'Web comparison

The design correctly increments remote_refcount for re-introductions (API design line
~311: "Increments remote_refcount if the entry already exists"). This is actually better
than Cap'n'Web's implementation, which appears to have a latent bug: ImportTableEntry
starts with remoteRefcount=1 and never increments it for re-introductions via
importStub(). If the same export ID is re-exported in a second expression before the first
is released, the release sends refcount=1 when the exporter's refcount is 2, causing a
leak.

Worth noting this explicitly as a known Cap'n'Web divergence/fix.

8. promise expression: re-use prevention

Cap'n'Web (rpc.ts:512-524) explicitly checks that a ["promise", id] doesn't reuse an
existing import ID:
importPromise(idx: ImportId): StubHook {
    if (this.imports[idx]) {
        return new ErrorStubHook(new Error("Bug in RPC system..."));
    }
    ...
}

The API design doesn't mention this validation. The evaluator should reject promise
expressions with already-in-use IDs.

9. Read loop sequentiality vs. background tasks

The design should clarify that message processing in the read loop is sequential (one
message at a time), even though some messages spawn background tasks (resolve watchers,
teardown watchers). Specifically: the read loop should await the expression evaluation for
each message before processing the next one, to preserve message ordering invariants.

10. Main interface (ID 0) initialization details

The design should be explicit: export 0 stores a clone of the user-provided main_interface
(the transport takes ownership); import 0 is created with resolution_future=None (handle
import), local_refcount=1 (for ctx.remote_main), remote_refcount=1 (initial introduction).

11. Error encoding for reject/broken: no-stubs enforcement

The design says "reject expressions cannot contain stubs" and suggests defensive checks.
It should specify: when encoding for reject/broken, the encoder should raise an error if
it encounters any RPCHandle or RPCPending in the value being encoded (rather than silently
exporting them). Alternatively, the encoder could strip them and replace with a
descriptive error value.

12. Trivially-resolved pending: full interface specification

The design mentions this type but should specify all methods:
- pull() → return value immediately
- dispatch() → navigate via _walk_and_dispatch_ir, return appropriate type
- dispatch_and_wait() → same but awaits
- as_handle() → assert value is RPCHandle, return it (or wrap)
- clone() / discard() → standard semantics
- wait_until_teardown() → wait for discard or connection abort (no resolution phase since
already resolved)

---
## Design Quality Assessment

Things the design gets right:

- The import type table (line ~106) is a clear, correct taxonomy matching the protocol
- Release-on-resolve matching Cap'n'Web semantics, with the concurrency implications
well-analyzed
- The send lock design and its interaction with resolution checking is carefully thought
through
- Teardown/broken as an extension to Cap'n'Web for handle lifecycle notification
- Clone-on-export ensuring decoupled lifetimes
- The two-phase teardown pattern for pending handles matching local caps behavior
- Encoding rollback for encoder failures
- Correct remote_refcount tracking (fixing a Cap'n'Web gap)
- Explicit abort ordering for consistency

Overall: This is a solid, well-researched design that demonstrates deep understanding of
both Cap'n'Web and the Cap-IR abstraction layer. The major issues (1-3) are real gaps that
should be addressed before implementation. The moderate issues (4-6) are edge cases that
could be deferred but would benefit from at least being documented. The minor issues are
polish.

The design is at the right level of detail for implementation — most sections are precise
enough to code from directly. The main areas where more specificity would help are:
evaluation rollback (issue 1), the ["import", id] value coercion rules (issue 2), and the
trivially-resolved pending interface (issue 12).
