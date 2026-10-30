# Stage D implementation plan

Detailed plan for implementing the lowering layer: `LeasedCapRPCConnection`, the `Remote…`
proxies, the devaluator, the two-phase evaluator, and all the per-message handlers.

Companion docs:
- `overview.md` — top-level stage plan; has Stage D's goal + the test inventory it needs to end
    up satisfying.
- `phase_d_prep_todos.md` — the scratch / working doc from the sketch pass. §1 (small fixes)
    and §2 (committed evaluator design) are already applied to the code. §3 (cleanups) is
    promoted into §1 of this plan because those cleanups are required, not optional.

The scaffolded shell in `fxtr2/rpc/lowering.py` already has the full class + entry-dataclass +
proxy layout; nearly every method body is `raise NotImplementedError("TODO: Stage D")`. This
plan focuses on how to fill in those bodies (plus the prerequisite lease-layer work).

## §1 — Pre-work cleanups (all must land before §2)

Each item ends with tests green. None of these depend on each other, so they can be landed
in any order. **All four are now complete** (2142 tests passing).

### 1a. Add `token_export_id` to `ExportedLeaseExpr` — ✅ done

Closing a small protocol gap: the receiver must know the subject token of a newly-introduced
grant so `RemoteGrant.subject` can return a valid `RemoteLifetimeToken`. Push-allocated and
handle/future-accompanied grants already carry their token ID; a standalone
`ExportedLeaseExpr` currently doesn't.

- **Edit.** `fxtr2/rpc/lowering_types.py`: add `token_export_id: int` to
    `ExportedLeaseExpr`. Update its docstring. Also update the module-level "Leases, Grants,
    and Lifetimes" wire-protocol prose to mention the token ID.
- **Edit.** `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`: update the Expressions table entry.
- **Tests.** No behaviour to test yet; typecheck + existing tests still green.

### 1b. Move grant validation out of `LocalPendingResultBox` — ✅ done

We want `RemoteIRFuture` to validate leases by "grant subject is this future's token" rather
than "grant equals a specific expected grant", because multiple grants can share a subject
token on the import side. Easiest fix is to push the check up into the concrete `IRFuture`.

- **Edit.** `fxtr2/leases/leased_cap_common.py`:
    - Drop the `expected_grant` parameter from `LocalPendingResultBox.__init__`. Remove the
        `_expected_grant` field + `expected_grant` property + the check inside
        `await_resolution`. The box now only cares about the token.
    - Move the `lease.grant == expected_grant` check into `IRFutureLocalImpl.wait` (still
        stored on the impl: the impl knows the single expected grant).
    - Update `spawn_ir_future_in_task_group` accordingly (no longer passes
        `lifetime.root_lease.grant`).
- **Tests.** Update any `LocalPendingResultBox` construction in `tests/test_leases/` to drop
    the grant arg. Add a test that `IRFutureLocalImpl.wait` still rejects a lease on a
    different grant (to confirm we didn't lose the check).

### 1c. Generify `_LeaseCleanupInfo` (promoted from §3a of prep todos) — ✅ done

`LeasedCapRPCConnection` needs its own cleanup-info shape (the grant import ID, mainly);
gluing that into the current `_LeaseCleanupInfo` with a `via=None, kind="connection"` would
be awkward and would mean the connection leaks a dead `via` field on every entry.

- **Refactor.** `fxtr2/leases/local_lease.py`:
    - Turn `_LeaseOwner` into `_LeaseOwner[RB, I]` parameterized on the info type.
    - Split `_LeaseCleanupInfo` into owner-specific shapes:
        - `_UnconditionalLeaseCleanupInfo(lease_repr, alloc_tb)`
        - `_AttachedLeaseCleanupInfo(kind, via, lease_repr, alloc_tb)` (retains the existing
            fields; `kind` is now only "key"|"output")
    - `_on_lease_leaked` becomes generic in `I`; the owner still provides
        `_clean_up_lease_data(lease_id, info)`.
    - `_LeaseKind` literal narrows to the attached case.
- **Tests.** Existing test suite (216 lease tests) should continue to pass with only
    typing-level churn. Add one explicit unit test per new info type if useful (probably
    not — the existing coverage exercises both paths).

### 1d. Make `LifetimeToken` / `Grant` hashable-safe (promoted from §3b of prep todos) — ✅ done

The re-export checker in Stage D keys dicts by `LifetimeToken` and `Grant` directly, so the
base types need to be hashable and mutable state must live off-token to avoid ref cycles.

- **Refactor.** `fxtr2/leases/lease_types.py`: make `LifetimeToken[B]` and `Grant[B]`
    declare `__hash__ = object.__hash__` explicitly (they inherit abstract `__hash__` from
    `collections.abc.Hashable` and that overrides `object.__hash__`). Document the
    "tokens/grants hold only weak references to mutable state" contract in a module-level
    docstring paragraph.
- **Audit.** `fxtr2/leases/local_lease.py`:
    - `LocalLifetimeToken._end_callbacks` currently hangs directly off the token.
        Acceptable for now (the token itself is small, and callbacks are drained when the
        token ends) — but add a short docstring note about why it's OK, and that Stage D's
        `RemoteLifetimeToken` will hold zero mutable state (all state on the import entry).
    - `LocalGrant`'s subject-token reference is fine; also documented.
    - Add explicit `__hash__ = object.__hash__` declarations on both concrete classes.
- **Tests.** Assert tokens and grants are usable as dict keys in a small new test in
    `test_local_lease.py`. Existing 216 pass.

## §2 — Stage D main work

Each sub-stage ends with tests green. The order respects dependencies: tables/handshake
before anything else, devaluator+phase1 before any message-level work, phase2 evaluators
before push/pull/resolve, etc.

### 2a. Scaffold: `__init__`, tables, outbox, reader/writer skeletons — ✅ done

- `__init__` initializes all fields: empty tables, reserved main-interface entries at IDs
    0/1/2 (both directions), id allocators, outbox stream pair, re-export checker dicts,
    `_aborted_reason = None`.
- `_enqueue_message`, `_allocate_export_id`, `_allocate_push_import_ids`, `_check_not_aborted`
    — all small synchronous helpers.
- `_reader_loop` / `_writer_loop` skeleton: drive through the transport, dispatch to
    `_on_*` handlers (all still raising `NotImplementedError` except `_on_hello` +
    `_on_abort` which land in 2b).
- `_abort(reason)` — stores reason, cancels task group, (later: best-effort send Abort,
    revoke imports). For this sub-stage, abort just tears down cleanly.
- `async with __asynccontextmanager__` and `start_in`:
    - Open connection's task group.
    - Send `HelloMessage` immediately (no wait for peer Hello; see §5 decision in prep
        todos).
    - Spawn reader + writer.
    - Yield / return.
    - On normal exit: drain (wait for full drain = no non-broken imported leases in either
        direction). For now, placeholder — actual drain semantics come with lease handling
        in 2e.
    - On exception: call `_abort(reason)` and re-raise.
- Register main-interface export entries:
    - Token 0 → `_TokenExportEntry(0, our_main_token, watch_deregister=None)`.
    - Handle 1 → `_HandleExportEntry(1, token_export_id=0, our_main_interface,
        outstanding_count=1)`.
    - Grant 2 → `_GrantExportEntry(2, token_export_id=0, backing_lease=our_main_lease.take(),
        outstanding_count=1)`.
- Register main-interface import entries (symmetric): token 0 / handle 1 / grant 2 each at
    refcount 1.
- `our_main_interface` / `our_main_grant` / `their_main_interface` / `their_main_lease`
    properties just read the tables.
- `as_client` / `as_server`:
    - `as_client` constructs with a placeholder handle that raises `IRException` on every
        request; leaves the reverse lease alive.
    - `as_server` constructs normally, then discards `their_main_lease` inside the
        context-manager entry.

**Test (`tests/test_rpc/test_lowering.py`, new file):**
- Two in-process connections wired with a `create_memory_object_stream` pair; both enter
    `async with`, both come up, both shut down cleanly with no messages sent beyond the
    two `HelloMessage`s.
- Basic property access (`their_main_interface`, `their_main_lease`) works.

### 2b. `HelloMessage` + `AbortMessage` handling — ✅ done

- `_on_hello`: first receipt transitions the reader state to "handshake done" (or simply no-
    op since we already expose the main interface pre-Hello); second receipt → abort with
    `HandshakeError("peer sent second HelloMessage")`.
- `_eval_static` (sync). Accepts `StaticExpr` / `ListExpr` / `DictExpr` / `IRTaggedExpr`;
    everything else raises. No phase-1 substitution — operates on the raw tree.
- `_on_abort`: run `_eval_static`; synthesize a replacement `IRException` on any failure;
    pass the result into `_abort`. Abort tears down all imports.
- `_abort(reason)` — full implementation now:
    - Best-effort send an `AbortMessage` (may fail if transport is already dead — catch).
    - For each `_TokenImportEntry`, set `ended=True` and fire callbacks.
    - For each `_GrantImportEntry`, mark `broken_reason`, walk `live_leases` and
        `_mark_broken`; clear the dict.
    - For each `_TokenExportEntry`, deregister watch callback if any.
    - Cancel task group → reader/writer unwind → `async with` unwinds.
- Pre-Hello ordering: reader buffers first message; if it's not `HelloMessage`, abort.

**Tests:**
- Wrong first message aborts both sides.
- Double Hello aborts both sides.
- Explicit abort from one side cleanly tears down the other.
- Transport break (close the stream from the outside) aborts gracefully.

### 2c. Devaluator + Phase 1 substitution — ✅ done (tests deferred to §2e)

Implement outbound value → `Expression[T]` translation and inbound
`Imported…Expr`/`Exported…Expr` substitution to `_ConstantExpr`. Both are synchronous, so
they can't interleave.

- `_devaluate(value: IRCallArg) -> Expression[Any]`:
    - Dispatch by type: primitive → `StaticExpr`; list/dict/IRTagged → recurse into
        container expr; `IRException` → `StaticExpr` (it's IRStatic); `Lease` → see below;
        `IRHandle` → see below; `IRFuture` → see below; `LifetimeToken` → see below;
        `UnwrapForPipelining` → `AwaitFutureExpr(_devaluate(future), _devaluate(lease))`.
    - For each value-that-introduces-an-export: consult the re-export dict
        (`_grant_export_id_by_grant` / `_token_export_id_by_token` /
        `_handle_export_id_by_handle_id` / `_future_export_id_by_future_id`). If present,
        emit `ExportedXExpr(id)` and increment `outstanding_count`. If absent, allocate a
        new negative export ID, create the `_XExportEntry`, emit `ExportedXExpr(...)`.
    - For a `Lease`: check `isinstance(lease.grant, RemoteGrant) and lease.grant.conn is
        self`. If so, this lease is bouncing back — emit
        `ImportedLeaseExpr(grant_import_id)` and discard the consumed lease. Otherwise, it's
        a fresh export: look up the grant in `_grant_export_id_by_grant`; if present,
        increment the entry's `outstanding_count` and discard the user's lease; if absent,
        allocate a new grant export ID + token export ID (registering a `_TokenExportEntry`
        if the subject token isn't already exported), `lease.take()` into the new
        `_GrantExportEntry.backing_lease`, emit `ExportedLeaseExpr(grant_id, token_id)`.
    - For an `IRHandle` / `IRFuture` / `LifetimeToken`: symmetric, but simpler (no backing
        lease).
    - Speculative-rollback shape: accumulate `(new_export_entries_by_id, new_reverse_lookup_
        entries, taken_backing_leases)` in a dataclass. On any exception during the walk,
        discard the taken leases (so they don't leak-warn) and drop the allocations. On
        success, commit into `_exports` + reverse-lookup dicts and enqueue the message.
- `_phase1_substitute(expr: Expression[Any]) -> Expression[Any]`:
    - Recursively walks the tree, substituting every `Imported…Expr` (sender's import = our
        export — look up `_exports`) and `Exported…Expr` (sender's export = our import —
        look up or register `_imports`) with `_ConstantExpr(runtime_value)`.
    - For `ImportedLeaseExpr(grant_id)`: look up `_GrantExportEntry[grant_id]`; construct a
        new `LocalLifetimeLease(owner=self, grant=RemoteGrant(self, -grant_id))`... wait,
        no — the grant is something we exported, so the lease here is on the *local* grant,
        not a remote one. We clone our stored `backing_lease` and return that. (Clarified
        below: "Imported…Expr from sender ⇒ this is ours, use local types; Exported…Expr
        from sender ⇒ we're the importer, use Remote proxies.")
    - For `ExportedLeaseExpr(grant_id, token_id)`:
        - If `_GrantImportEntry[grant_id]` exists: increment `introduction_refcount`,
            validate `token_import_id` matches; on mismatch → `_abort`. Return a new
            `LocalLifetimeLease(owner=self, grant=RemoteGrant(self, grant_id))`.
        - If no entry: register a `_TokenImportEntry[token_id]` if needed and a
            `_GrantImportEntry[grant_id]`, then mint the lease as above.
    - For `ImportedIRHandleExpr` / `ExportedIRHandleExpr` / `…IRFutureExpr` /
        `…LifetimeTokenExpr`: analogous.
    - `_ConstantExpr` — private frozen dataclass in `lowering.py` (already sketched).

**Tests:**
- `_devaluate` round-trip: devaluate → manually phase-1-substitute on the other side →
    verify the substituted `_ConstantExpr` payloads match expectations.
- Bounce-back identity: devaluate a `RemoteIRHandle` we imported → must produce
    `ImportedIRHandleExpr`, not a fresh export.
- Rollback: inject a `TypeError` in the middle of a devaluate walk; verify no export
    entries were committed and any taken leases were discarded (leak-warning count
    unchanged).
- Re-export dedup: sending the same local handle twice uses the same export ID.

### 2c_post1. File-split refactor (mechanical)

Goal: break up the now-~2400-line `fxtr2/rpc/lowering.py` into focused submodules before
§2d piles phase-2 evaluators on top. No behaviour changes; all 2152 tests must still
pass after each step.

**Design invariants:**

- `from __future__ import annotations` stays at the top of every file; all annotations are
    strings at runtime, so `TYPE_CHECKING`-gated imports suffice for forward-references to
    `LeasedCapRPCConnection` from submodules.
- Dataclass field annotations referencing `LeasedCapRPCConnection` (proxies, entries) keep
    working because `@dataclass` only iterates `__annotations__` as strings — it does not
    resolve the annotations for `__init__` generation.
- No Protocol abstraction over the connection — the submodules type their `conn` parameter
    directly as `LeasedCapRPCConnection` via a `TYPE_CHECKING` import. Simpler than a
    Protocol, no duplication of the connection's attribute/method surface.

**Target file layout** (all under `fxtr2/rpc/`):

| File | Contents | Runtime imports (new additions; existing leases/leased_cap_ir/lowering_types etc. unchanged) |
|---|---|---|
| `lowering_errors.py` | `RPCConnectionError`, `HandshakeError`, `RPCConnectionAborted` | (none — only stdlib) |
| `lowering_state.py` | `MAIN_TOKEN_ID`/`MAIN_HANDLE_ID`/`MAIN_GRANT_ID`; `_ConstantExpr`; `_ConnectionLeaseCleanupInfo`; all 8 `_*ImportEntry`/`_*ExportEntry` dataclasses; `_ImportEntry`/`_ExportEntry` union aliases | leases, lowering_types |
| `lowering_remote_proxies.py` | `RemoteLifetimeToken`, `RemoteGrant`, `RemoteIRHandle`, `RemoteIRFuture` | leases; **TYPE_CHECKING:** `.lowering.LeasedCapRPCConnection` |
| `lowering_null_main.py` | `_NullMainInterface` | leases (for `IRHandle`, `LifetimeToken`) |
| `lowering_devaluate.py` | `_DevaluateContext`, `devaluate(conn, value)`, `_devaluate_walk`, `_devaluate_lease/_handle/_future/_token`, `_ensure_token_exported`, `_bump_outstanding_count` — all top-level functions taking `conn` as first positional | lowering_state, lowering_remote_proxies, lowering_errors; **TYPE_CHECKING:** `.lowering.LeasedCapRPCConnection` |
| `lowering_evaluate.py` | `phase1_substitute(conn, expr)`, `eval_static(expr)`, `_phase1_resolve_our_export/_register_*_import/_substitute_request/_substitute_await`, `phase1_protocol_violation(conn, reason)` — top-level functions. Leaves room for §2d's async phase-2 evaluators to land here. | lowering_state, lowering_remote_proxies, lowering_errors; **TYPE_CHECKING:** `.lowering.LeasedCapRPCConnection` |
| `lowering_message_handlers.py` *(added in 2c_post2; see below)* | `dispatch_message(conn, msg)` entry point plus all `_on_hello/_on_push/_on_pull/_on_resolve/_on_reject/_on_watch/_on_closed/_on_break/_on_discard/_on_unpull/_on_abort` handlers — top-level functions taking `conn` as first positional. Enforces the "peer's first message must be HelloMessage" handshake rule. | lowering_errors, lowering_evaluate, lowering_types; **TYPE_CHECKING:** `.lowering.LeasedCapRPCConnection` |
| `lowering.py` | `LeasedCapRPCConnection` + thin wrappers that call into the submodules. Re-exports `HandshakeError`/`RPCConnectionAborted`/`RPCConnectionError`/`Remote*` proxies for backward-compat imports. | all of the above |

`eval_static` takes no `conn` param — it's purely `Expression → IRStatic` with no
table access. Flagged as a standalone exception to the "take `conn` first" shape.

**Runtime import graph** (TYPE_CHECKING edges not shown; they don't create cycles):

```
lowering_errors            → (none)
lowering_state             → lowering_errors*, lowering_types
lowering_remote_proxies    → lowering_state
lowering_null_main         → (leases only)
lowering_devaluate         → lowering_errors, lowering_state, lowering_remote_proxies
lowering_evaluate          → lowering_errors, lowering_state, lowering_remote_proxies
lowering                   → all of the above
```

(\* `lowering_state` imports `lowering_errors` only if it needs to raise any of those
exceptions in ctor validation; currently it doesn't, so this edge is absent today.)

**Mechanical steps** (run tests + pyright after each — or at minimum after steps 1, 6,
and 7 — to narrow down any breakage):

1. **Create `lowering_errors.py`.** Move `RPCConnectionError`, `HandshakeError`,
    `RPCConnectionAborted` verbatim. Add `from fxtr2.leases.lease_types import LeasingError`
    as the base-class import. In `lowering.py`, replace the original definitions with
    `from fxtr2.rpc.lowering_errors import HandshakeError, RPCConnectionAborted,
    RPCConnectionError` (with explicit `as` aliases if we want them to count as public
    re-exports for pyright).

2. **Create `lowering_state.py`.** Move:
    - `MAIN_TOKEN_ID`, `MAIN_HANDLE_ID`, `MAIN_GRANT_ID` constants.
    - `_ConnectionLeaseCleanupInfo` dataclass.
    - `_ConstantExpr` dataclass.
    - All 8 entry dataclasses (`_TokenImportEntry`, `_GrantImportEntry`,
        `_HandleImportEntry`, `_FutureImportEntry`, `_TokenExportEntry`,
        `_GrantExportEntry`, `_HandleExportEntry`, `_FutureExportEntry`).
    - `_ImportEntry` / `_ExportEntry` union type aliases.
    - Top-of-file module docstring: "Typed value containers for the connection's import /
        export tables. No logic — see lowering_devaluate.py / lowering_evaluate.py for how
        these entries are produced and consumed."
    - Imports needed: `weakref`, `anyio.CancelScope`, `LifetimeToken`, `Lease`, `IRHandle`,
        `IRFuture`, `Grant`, `LocalLifetimeLease`, `LocalPendingResultBox`,
        `_LeaseCleanupInfo`, `Expression`.
    - In `lowering.py`, replace with `from fxtr2.rpc.lowering_state import (…)`.

3. **Create `lowering_remote_proxies.py`.** Move `RemoteLifetimeToken`, `RemoteGrant`,
    `RemoteIRHandle`, `RemoteIRFuture` verbatim. Top of file:
    ```python
    from __future__ import annotations
    from typing import TYPE_CHECKING, Any
    if TYPE_CHECKING:
        from fxtr2.rpc.lowering import LeasedCapRPCConnection
    ```
    Plus the actual dataclass imports (`Grant`, `LifetimeToken`, `IRHandle`, `IRFuture`,
    `Lease`, `FreshLifetime`, `LifetimeBrand`, `Request`, `UnwrapForPipelining`,
    `IRValue`). The `conn: LeasedCapRPCConnection[Any, Any]` field annotation stays as
    written — string-ized by `from __future__ import annotations`, no runtime dependency.
    In `lowering.py`, replace with `from fxtr2.rpc.lowering_remote_proxies import (…)`.

4. **Create `lowering_null_main.py`.** Move `_NullMainInterface`. Stand-alone
    module — imports only from leases + leased_cap_ir. In `lowering.py`, replace with
    `from fxtr2.rpc.lowering_null_main import _NullMainInterface`.

5. **Create `lowering_devaluate.py`.** Move `_DevaluateContext` and the devaluator
    functions. Function signature changes:
    - `def _devaluate(self, value)` → `def devaluate(conn: "LeasedCapRPCConnection",
        value: IRCallArg) -> tuple[Expression[Any], list[Lease[Any]]]:`
    - `def _devaluate_walk(self, value, ctx)` → `def _devaluate_walk(conn, value, ctx)`.
    - Same for `_devaluate_lease/_handle/_future/_token`, `_ensure_token_exported`,
        `_bump_outstanding_count`. All stay underscore-prefixed (module-private except
        for `devaluate`). The leading-underscore names are fine: within the module they're
        freely callable, and outside only `devaluate` is intended.
    - Inside the bodies: every `self.` becomes `conn.`. Every `self._allocate_export_id()`
        becomes `conn._allocate_export_id()`. Undo-log lambdas that captured `self` now
        capture `conn` (functionally identical — the lambdas don't care).
    - Top of file:
        ```python
        from __future__ import annotations
        from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, cast
        if TYPE_CHECKING:
            from fxtr2.rpc.lowering import LeasedCapRPCConnection
        ```
        plus all the other runtime imports (`weakref`, expression types, lease types,
        ir types, `RemoteGrant`/`RemoteIRHandle`/etc. from `lowering_remote_proxies`,
        entry types from `lowering_state`).

6. **Create `lowering_evaluate.py`.** Same pattern. Function-name changes (drop leading
    underscore on the public entry points — they're now meant to be imported by the
    connection):
    - `_phase1_substitute` → `phase1_substitute(conn, expr)`.
    - `_eval_static` → `eval_static(expr)` (no `conn`).
    - `_phase1_protocol_violation` → `phase1_protocol_violation(conn, reason)` (public —
        §2d phase-2 evaluators will also use it to abort on residual Imported*Expr nodes).
    - Internal helpers (`_phase1_resolve_our_export`, `_phase1_register_*_import`,
        `_phase1_substitute_request`, `_phase1_substitute_await`) stay underscore-prefixed
        and take `conn` as first arg.
    - Bodies: same `self.` → `conn.` rewrite.

7. **Trim `lowering.py`.** After the moves, the file should contain just:
    - Imports (including runtime imports from all new submodules).
    - Backwards-compat re-exports: `HandshakeError as HandshakeError`, etc. (use `as`
        aliases so pyright treats them as re-exports).
    - `LeasedCapRPCConnection` class definition.
    - The class's public methods (`__init__`, `__asynccontextmanager__`, `start_in`,
        properties, `as_client`/`as_server`, `push`), `_LeaseOwner` impl
        (`_register_lease`/`_deregister_lease`/`_clean_up_lease_data`),
        `_enqueue_message`, `_allocate_*`, `_check_not_aborted`, `_abort`, `_reader_loop`,
        `_writer_loop`, `_dispatch_message`, `_drain_and_eof`, and the still-stubbed
        `_on_*` handlers.
    - Thin wrappers that forward to submodule functions:
        ```python
        def _devaluate(self, value: IRCallArg):
            return devaluate(self, value)

        def _phase1_substitute(self, expr: Expression[Any]):
            return phase1_substitute(self, expr)

        def _eval_static(self, expr: Expression[Any]):
            return eval_static(expr)
        ```
        These wrappers exist so existing in-class callers (e.g. `_on_abort` calling
        `self._eval_static`) don't have to change.

8. **Run full test suite + pyright on the whole `fxtr2/rpc/` package.** Must be green. If
    there are residual cast issues surfaced by the move (pyright re-examining expressions
    in their new module context), fix them narrowly; don't change any logic.

**Acceptance:** same 2152 tests passing; `lowering.py` is substantially smaller (< ~1200
lines); `phase_d_prep_todos.md` references still work (they reference lowering.py by
name, not anchors).

### 2c_post2. Message-handler extraction (not in original 2c_post1) — ✅ done

Unplanned mechanical split done alongside 2c_post1: all the `_on_*` message handlers
(`_on_hello` / `_on_push` / `_on_pull` / `_on_resolve` / `_on_reject` / `_on_watch` /
`_on_closed` / `_on_break` / `_on_discard` / `_on_unpull` / `_on_abort`) moved out of
`lowering.py` into a new `lowering_message_handlers.py` module, alongside a
`dispatch_message(conn, msg)` entry point that also enforces the "peer's first message
must be a `HelloMessage`" handshake rule.

Rationale: the handlers are all top-level functions taking `conn` as first positional
(matching the 2c_post1 convention), they form a natural unit (routing + per-message
logic), and extracting them keeps `lowering.py` focused on connection lifecycle + table
machinery + `_LeaseOwner` impl + reader/writer loops. `LeasedCapRPCConnection` retains a
thin `_dispatch_message` wrapper that calls `dispatch_message(self, msg)` so existing
reader-loop code didn't need to change.

`_on_hello` and `_on_abort` are fully implemented in this module (they landed in §2b);
the remaining handlers are stubs raising `NotImplementedError("TODO: Stage D")` waiting
for §2e onwards to fill them in.

### 2d. Phase 2 evaluators (`eval_as_value`, `eval_as_call_arg`, `eval_forwardable`) — ✅ done

The phase-2 evaluators land in `lowering_evaluate.py` as top-level `async` functions
taking `conn` as first positional, matching the 2c_post1 module convention. Stubs
currently on `LeasedCapRPCConnection` in `lowering.py` are replaced with thin method
wrappers (`_eval_as_value` / `_eval_as_call_arg` / `_eval_forwardable`) that forward to
the new module functions.

Landed: full implementation of `eval_as_value` / `eval_as_call_arg` / `eval_forwardable`
plus private helpers (`_eval_future_subexpr` / `_eval_lease_subexpr` /
`_eval_handle_subexpr` for typed sub-slot checks; `_eval_await_fut_and_lease`;
`_dispatch_handle_request` — the shared `HandleRequestExpr` / `HandleDispatchExpr`
evaluator, with partial-failure cleanup that discards the root `lease` and any leases
inside `request` if `wait_for` or `request`-eval throws; `_eval_request`; `_eval_wait_for`).
`_phase2_unsupported` / `_phase2_type_mismatch` produce the per-future-failure
`IRException`s described in K.3.

All 2165 tests still passing (no direct 2d tests yet — exercised from 2e onwards).

All three operate on phase-1-substituted trees (`_ConstantExpr` leaves instead of
`Imported…Expr`/`Exported…Expr`). Implement them as straightforward recursive dispatchers
following the table in `phase_d_prep_todos.md` §K.2.

- Residual `Imported…Expr`/`Exported…Expr` inside a phase-2 evaluator → raise `IRException`
    ("phase-1 bug or malformed phase-1 output"). The spawn plumbing converts this into a
    per-future failure rather than a connection abort (K.3).
- `_ConstantExpr` with a runtime type the current mode can't accept → raise `IRException`
    ("type mismatch at <context>: got X, expected Y"). Same per-future routing.
- `AwaitFutureExpr` in `_as_value` mode: `await future.wait(lease)`. In `_as_call_arg` /
    `_forwardable` mode: `UnwrapForPipelining(future, lease)`.
- `HandleRequestExpr` in `_as_value` mode: `future, result_lease = handle.dispatch(lease,
    request)` then `await future.wait(result_lease.clone())` (discard result_lease in
    finally). In `_as_call_arg` / `_forwardable` mode: return `UnwrapForPipelining(future,
    result_lease)`.
- `HandleDispatchExpr` in all modes: return `[future, result_lease]` as a 2-list IRValue.
- Containers recurse with a mode-appropriate child mode (`_forwardable` container contents
    must still be `IRValue`, so they recurse in `_as_value`).

No direct tests at this stage — exercised by 2e onwards.

### 2e. Push / pull / resolve — basic round-trip call — ✅ done

- `RemoteIRHandle.dispatch`:
    - Sync. `handle_lease.take()`; take leases in `request`.
    - Build `HandleRequestExpr(handle=ImportedIRHandleExpr(self.handle_import_id),
        lease=<devaluated handle_lease>, request=<devaluated request>)`.
    - Call `self.conn._devaluate` on that expression; on the way, `_allocate_push_import_ids`
        for `(token_id, future_id, grant_id)`.
    - Register `_TokenImportEntry` / `_FutureImportEntry` / `_GrantImportEntry` at those
        IDs, with refcounts=1 (the push allocation itself).
    - `_enqueue_message(PushMessage(expression))`.
    - Construct `RemoteIRFuture(conn, future_id)` and `LocalLifetimeLease(owner=conn,
        grant=RemoteGrant(conn, grant_id))` and return.
- `RemoteIRHandle.request`: `dispatch` + `wait` + discard result lease (identical shape to
    `IRHandleLocalImplBase.request`).
- `RemoteIRFuture.walk`: return new `RemoteIRFuture(conn, id, attr_path + extra)` — no
    message.
- `RemoteIRFuture.wait(lease)`:
    - `lease.take()`; validate `lease.grant.subject` is this future's token (this is
        the import-side validation we moved into concrete `IRFuture`s in §1b).
    - Look up `_FutureImportEntry`. If `resolution` is None, construct a
        `LocalPendingResultBox` (the now-grant-agnostic variant) — the box observes the
        token.
    - If nobody has pulled yet, `_enqueue_message(PullMessage(future_id, grant_id))` and
        bump `pending_pull_refcount`. (Mapping future → grant: for push-allocated futures
        the grant is `future_id+1`; otherwise we need to pick a grant we have on the same
        token. Simplest rule: remember the grant_id that was used for the last lease passed
        into `wait`.)
    - `await box.await_resolution(self.attr_path, lease)`.
- `_on_push`:
    - `_phase1_substitute(msg.expression)`.
    - `_allocate_export_id()` three times to mirror the sender's implicit allocation...
        actually no: the sender chose *positive* IDs (importer-allocated), and we receive
        those same positive IDs in our export table. So we don't allocate — we register the
        three specific IDs the sender chose.
    - Need to know what IDs those are. Convention: the push allocates the next 3 positive
        IDs the sender hadn't yet used. On our side, we track the sender's next-expected
        positive ID as `_next_peer_push_id` (starts at 3) and increment by 3 per push.
    - `spawn_ir_future_in_task_group(work=lambda: self._eval_forwardable(substituted),
        task_group=self._tg)` → `(local_future, root_lease)`.
    - Register `_TokenExportEntry`, `_FutureExportEntry`, `_GrantExportEntry` at those IDs;
        grant's `backing_lease = root_lease` (already taken by `spawn_ir_future_in_task_
        group`).
- `_on_pull`:
    - Look up `_FutureExportEntry`. Increment `pending_pull_count`.
    - If no resolver task is running: find the grant export entry (explicit grant_id in
        the message), clone its `backing_lease`, spawn a task in `self._tg` that `await`s
        `local_future.wait(cloned_lease)` then devaluates the result into a `ResolveMessage`
        (or catches `IRException` and sends a `RejectMessage`). Store the task's cancel
        scope on `_FutureExportEntry.resolve_task_cancel_scope`.
- `_on_resolve`:
    - `_phase1_substitute`; spawn a task in `self._tg` that runs `_eval_forwardable` on the
        substituted expression and routes outcome into
        `_FutureImportEntry[future_export_id].resolution` (set_result / resolve_from /
        set_exception).
- `_on_reject`:
    - Synchronous. `_eval_static(msg.expression)`. If it's an `IRException`, deliver via
        `box.set_exception`. Otherwise synthesize a "malformed reject payload"
        `IRException` and deliver that.
- Protocol-violation double-resolve / resolve-after-reject / etc. → `_abort` with a
    descriptive `IRException`.

**Tests:**
- Main-interface round-trip: call `their_main_interface.request(lease, Call("echo",
    arg="hello"))`; primitive args + primitive result.
- `dispatch` + later `wait` explicitly: verifies pull is sent only on wait, not on
    dispatch.
- Multiple `wait`s on the same future: only one pull sent; both waits see the same result
    (deep-copied each time).
- Exception propagation: the callee raises `IRException("oops")`; the caller's `wait`
    raises a fresh copy of it.
- Double-resolve from the peer → our side aborts.

### 2f. Leases across the wire (regrant, break, discard, watch/closed) — ✅ done

- `_LeaseOwner` impl on `LeasedCapRPCConnection`:
    - `_register_lease(lease)`: `lease.grant` is a `RemoteGrant(self, grant_id)`. Look up
        `_GrantImportEntry[grant_id]`; insert into `live_leases`; set up a
        `weakref.finalize` per the §1c protocol using a `_ConnectionLeaseCleanupInfo(grant_
        import_id, lease_repr, alloc_tb)`.
    - `_deregister_lease(lease)`: detach finalizer; call `_clean_up_lease_data`.
    - `_clean_up_lease_data(lease_id, info)`: remove from `live_leases`; if dict is now
        empty and entry isn't already broken, read `introduction_refcount`, zero it, and
        `_enqueue_message(DiscardMessage(import_id, refcount))`.
- `_on_discard`:
    - Look up the export entry; decrement `outstanding_count` by `refcount`. When it hits
        zero: if grant → discard `backing_lease`, remove from tables, remove from
        `_grant_export_id_by_grant`. If handle/future → just remove (we don't have a
        backing lease to drop).
- `_on_break`:
    - Look up `_GrantImportEntry`; set `broken_reason`. Iterate `live_leases.values()` and
        `_mark_broken(reason)` each. The `_deregister_lease` path for each will naturally
        remove them.
- `_on_watch`:
    - Look up `_TokenExportEntry`. If `local_token.ended()`, immediately enqueue
        `ClosedMessage`. Else call `register_end_callback(lambda:
        self._enqueue_message(ClosedMessage(export_id)))` and store the deregister
        callback as `watch_deregister`.
- `_on_closed`:
    - Look up `_TokenImportEntry`; set `ended=True`; fire `end_callbacks`; clear.
- `RemoteLifetimeToken.ended` / `.register_end_callback`:
    - `ended` reads the import entry's `ended` field.
    - `register_end_callback(cb)` appends to `end_callbacks`. First registration also sends
        a `WatchMessage` if `watch_sent` is False. Returns a deregister.
- `RemoteGrant.subject`: returns `RemoteLifetimeToken(conn, token_import_id)` looked up via
    the grant import entry.
- Drain on normal exit of `async with`: wait for no non-broken imported leases in either
    direction. Impl: `_drain_event` set when both `all(entry.live_leases == {} for entry in
    _imports)` (ours) and symmetric signal from peer (their drain state signalled
    explicitly? or implied by all-grants-discarded?). Simplest: drain = "our
    `_GrantImportEntry.live_leases` are all empty, AND our peer has discarded all the
    grants we've exported (i.e. `_GrantExportEntry.outstanding_count == 0` for all
    non-main entries)". Main interface IDs 0/1/2 treated specially — they drain when their
    leases are explicitly discarded, same rule.

**Tests — all passing (2180 total in v2 suite):**
- [x] Pass a lease across; receiver uses it (call an imported handle with it); receiver
    discards; sender's backing lease becomes free when `outstanding_count` hits zero.
- [x] `break` on an exported grant: leases on the receiver side all fire broken-callbacks
    synchronously when the `BreakMessage` is processed.
- [x] Re-export a grant mid-session (send same grant twice): receiver sees same
    `grant_import_id` both times; `_GrantImportEntry.introduction_refcount == 2`;
    `DiscardMessage(refcount=2)` sent when all leases drop.
- [x] `WatchMessage` / `ClosedMessage`: register end-callback on a remote token; revoke on
    the other side; our callback fires.
- [x] Connection abort revokes all imported grants (broken-callbacks fire on every
    still-live imported lease).
- [x] Edge case: peer discards our exported grant before we locally revoke; no stale
    `BreakMessage` is emitted for the torn-down export.

**Implementation notes emerging from §2f:**
- **Outbound `BreakMessage`, local teardown.** When the broken-callback fires on a
    grant export's `backing_lease`, we emit `BreakMessage` to the peer AND remove the
    export entry locally (pop from `_exports` + `_grant_export_id_by_grant`, discard
    the now-broken backing lease). Rationale: once we've sent `BreakMessage`, the
    peer is no longer obligated to send `DiscardMessage` back — if we kept the entry
    alive waiting for it, drain on normal exit would block indefinitely on a broken
    grant. A racing `DiscardMessage` from the peer hits `_on_discard` with no entry
    and is a harmless no-op.
- **Drain signalling via `anyio.Event`.** `_drain_event` is `.set()` by
    `_check_drain` whenever (a) every `GrantImportEntry` has empty `live_leases` or
    is broken, AND (b) every `GrantExportEntry` has `outstanding_count == 0`.
    Called from `_clean_up_lease_data` (outbound DiscardMessage emission path),
    `_on_discard` (inbound tear-down), `_on_break` (inbound broken-grant tear-down),
    the on_break broken-callback (local-revocation tear-down), and `_abort` (abort
    unconditionally sets drain so a pending wait unblocks). Normal-exit path in
    `__asynccontextmanager__` discards `their_main_lease` first, then
    `await self._drain_event.wait()`.
- **Hello-first ordering.** `HelloMessage` moved from `__aenter__` to `__init__` so
    that `as_server`'s construction-time `their_main_lease.discard()` (which now
    emits `DiscardMessage` under §2f) queues after the Hello.
- **Main-grant break-callback subscription at init.** The main grant (export ID 2)
    also subscribes its backing lease's broken callback, so a user who revokes
    their main lifetime (e.g. by exiting `open_lifetime_task_group` exceptionally)
    propagates a `BreakMessage` to the peer. Before Stage D's abort path takes over,
    the peer already sees the break.

**Additional tests worth adding in the future** (surfaced during stock-taking, not
blockers for §2g):

- Grant import breaks during an in-flight call using a lease from that grant — the
    call's future should fail with `IRException`, not hang, and leases inside the
    request should surface as broken on the callee side.
- `BreakMessage` for a grant_export_id we never exported (peer misbehavior) — we
    now abort via `phase1_protocol_violation`, but no test exercises that path.
- `DiscardMessage` with a refcount that overshoots `outstanding_count` — also now
    aborts, not covered by a test.
- Token-watch auto-scrub when the local token ends *and* the peer has not yet
    sent a matching `DiscardMessage` — `_watched_token_exports` should empty and
    abort should not re-call a stale deregister. (We have the fix for the
    peer-discards-watched-token path but not the symmetric "local token ends,
    peer never discards" path.)
- End-to-end drain-hang pattern: caller wrapping the `async with` in their own
    `anyio.move_on_after` and observing that abort cleans up promptly. We document
    the pattern but don't regression-test it.
- `§2j` prework: emit best-effort `DiscardMessage` for handle/future/token imports
    on proxy GC, plus a test that confirms the discards are emitted.

### 2g. Pipelining — ✅ done (2185 tests passing)

Two pieces land together:

- **`RemoteIRFuture.pipeline_dispatch(future_lease, inner_handle_lease, request)`.**
    - Take all inputs.
    - Build `HandleRequestExpr(handle=AwaitFutureExpr(
        future=WalkFutureExpr(ImportedIRFutureExpr(self.future_import_id), self.attr_path)
          or ImportedIRFutureExpr(...) when attr_path is empty,
        lease=<devaluated future_lease>), lease=<devaluated inner_handle_lease>,
        request=<devaluated request>)`.
    - Devaluate + allocate 3 import IDs + enqueue `PushMessage` + return `(RemoteIRFuture,
        Lease)`.
    - Impl note: `devaluate_pipeline_dispatch` sits alongside `devaluate_handle_request`
        in `lowering_devaluate.py`; both share a private inner helper that builds the
        request/wait_for/lease slots so only the `handle` slot construction differs.
- **`_on_pull` forward-delegation.** When the local future being pulled has resolved
    via `resolve_from` to a `RemoteIRFuture` on this same connection, we should emit a
    forwarding `ResolveMessage(AwaitFutureExpr(ImportedIRFutureExpr(...), ImportedLeaseExpr(...)))`
    instead of eagerly awaiting through the forward chain. This avoids a round-trip
    back to the peer for the value they already own.
    - New primitive `LocalPendingResultBox.await_or_forward(attr_path, lease) -> T |
        UnwrapForPipelining[T, Any]`: like `await_resolution` but on `"forwarded"`
        state returns the forward-future+lease pair instead of recursively awaiting.
    - New method `IRFutureLocalImpl.wait_or_forward(lease)`: grant-check + delegate
        to `await_or_forward`. Returns `T | UnwrapForPipelining[T, Any]`.
    - Resolver loop in `_on_pull`: keep calling `wait_or_forward` while the current
        future is an `IRFutureLocalImpl`; terminate when either (a) the box resolved
        to a concrete value → emit normal `ResolveMessage`, (b) the chain terminates
        at a `RemoteIRFuture` on our own connection → devaluate `UnwrapForPipelining`
        and emit forwarding `ResolveMessage(AwaitFutureExpr(...))`, or (c) the chain
        terminates at anything else (e.g. a `RemoteIRFuture` on a different connection)
        → fall back to eager `wait()` and emit normal `ResolveMessage`.
- **`resolve_from` chain on the sending side.** Already drops out naturally from
    the `_eval_forwardable` + spawn plumbing — when the top-level expression evaluates
    to `UnwrapForPipelining`, the result box forwards instead of re-boxing.

**Tests:**
- Push a call on the result of an earlier push without awaiting the first. Baseline
    pipelining — lands on the existing phase-2 `resolve_from` path; no new messages.
- **Bounce-back forward-delegation.** A exports `future_a` + `lease_a` to B; A calls
    `b_handle.bounce(future_a, lease_a)` where `bounce`'s handle impl returns
    `UnwrapForPipelining(future_a_as_B, lease_a_as_B)` (i.e. bounces the imported
    future back). Two variants:
    - `future_a` already resolved before the call: B detects the forward, emits
        `ResolveMessage(AwaitFutureExpr(ImportedIRFutureExpr, ImportedLeaseExpr))` back
        to A; A evaluates it via `_eval_forwardable` → `box.resolve_from` →
        `_await_forwarded` → returns the local value. Message-count asserts: B's
        outbound ResolveMessage's payload is an `AwaitFutureExpr`, not a value; A never
        sends a `PullMessage` back to B referencing the forward target.
    - `future_a` resolved after the call: same wire behaviour, but A's
        `_await_forwarded` blocks on the local box until A's test harness resolves it.
        Confirms the still-pending local-forward path works.

### 2h. `UnpullMessage` handling + drain-semantics rework — ✅ done

Scope expanded during implementation: a naive `UnpullMessage` implementation was
unsound because the existing drain logic (gated only on grant import/export sets)
didn't account for in-flight pull obligations. Cancelling a wait and dropping the
lease would satisfy drain locally while the peer still owed a resolve. The fix
unifies `UnpullMessage` with a full obligation-accounting reshape of the wait
path, tracked via four new pieces of state. Final design:

**Field renames.**
- `FutureImportEntry.pending_pull_refcount` → `resolve_obligation_refcount`.
- `FutureExportEntry.pending_pull_count` → `resolve_obligation_count`.

The rename reflects that the counts track *resolve obligations* (from received
`AwaitFutureExpr` as well as `PullMessage`), not just pulls. See
`UnpullMessage` docstring for the full rule.

**New state.**
- `FutureImportEntry.local_consumer_count` — per-import tally of active local
    consumers (active `wait` calls + phase-1-bumped spawns). When it drops to 0
    while `resolve_obligation_refcount > 0`, emit `UnpullMessage(refcount=the
    full outstanding count)` and zero the refcount.
- `GrantImportEntry.wait_break_callbacks` — break-notification subscriptions
    registered by no-lease `RemoteIRFuture.wait` calls. Fired alongside
    `live_leases` broken-callbacks by `_mark_grant_import_broken` so a grant
    break still gives in-flight waits a prompt `LeaseBrokenError`. Does not
    contribute to `DiscardMessage` refcounting.
- `FutureExportEntry.resolved_sent` — flips to `True` when we emit
    `ResolveMessage` or `RejectMessage` so late inbound `PullMessage` /
    outbound `AwaitFutureExpr` references on an already-resolved future are
    silently no-op'd.
- Two new active sets: `_active_resolve_obligation_imports`,
    `_active_resolve_obligation_exports`. Drain is now satisfied iff all four
    active sets (plus the two grant sets) are empty.

**`LocalPendingResultBox.await_resolution` parameter swap.** Takes
`register_break: Callable[[Callable[[str], None]], Callable[[], None]]` instead
of a lease. Local callers pass `lease.register_broken_callback`; remote callers
pass a registrar that subscribes to `GrantImportEntry.wait_break_callbacks`. One
parameter change, two call-site updates; a small helper in
`test_leased_cap_common.py` adapts the pre-existing tests to the new shape.

**`RemoteIRFuture.wait` rewrite.** Takes the user lease, sends `PullMessage` on
the 0→1 refcount transition, bumps `local_consumer_count`, discards the user
lease immediately (which may emit `DiscardMessage`), registers a wait-break
callback on the grant import entry, awaits the box, and in `finally` decrements
`local_consumer_count` — potentially emitting `UnpullMessage`.

**AwaitFutureExpr obligation tracking.** Synchronous in both directions so the
accounting is race-free with the reader/writer loops:

- Phase-1 substitution (inbound): when `AwaitFutureExpr.future` resolves to one
    of our future imports (direct `ExportedIRFutureExpr` or wrapped in a single
    `WalkFutureExpr`), bump `resolve_obligation_refcount` and
    `local_consumer_count` on the import entry. Returned as a list to the
    message handler so the phase-2 spawn releases the `local_consumer_count`
    slots in its `finally`.
- Devaluator (outbound): when an emitted `AwaitFutureExpr.future` resolves to
    one of our future exports, bump `resolve_obligation_count` on the export
    entry. Recorded in the devaluator's undo log for atomic rollback on error.

**Zero-on-resolve.** Inbound `ResolveMessage` / `RejectMessage` calls
`_zero_future_import_obligation` (no `UnpullMessage` — the peer is
discharging by resolve); outbound `_enqueue_resolve_for` /
`_enqueue_reject_for` calls `_zero_future_export_obligation`, which sets
`resolved_sent = True`.

**`_on_pull` survival lease.** `_on_pull` now clones `backing_lease` twice:
`wait_lease` is consumed by the chain traversal as before, and `survival_lease`
is held by `resolver_with_scope`'s `finally` block and released only when the
resolver has emitted a resolve/reject or been cancelled by `_on_unpull`. This
keeps the push lifetime alive across forwarded/pipelined chains where
`wait_or_forward` would otherwise drop its hold the moment the box transitions
to `"forwarded"` — which, combined with the importer's prompt `DiscardMessage`,
would prematurely end the token and spuriously break the importer's pending
wait.

**`_on_unpull`.** Decrement `resolve_obligation_count` by `msg.refcount`;
clamp at zero with a warning if the peer overshoots; when it hits zero and
`resolved_sent is False`, cancel `resolve_task_cancel_scope`. The underlying
dispatched work keeps running per the "wait is a read, not a control op"
contract.

**Tests (both in `tests/test_rpc/test_lowering.py`):**
- [x] `TestWaitDiscardsLeasePromptly::test_discard_before_resolve` — verifies
    B emits `[Push, Pull, Discard]` before A's Resolve lands (filter out
    `WatchMessage` which interleaves from the box's token subscription).
- [x] `TestUnpullOnCancelledWait::test_unpull_emitted_and_nothing_comes_back` —
    verifies a cancelled wait emits `[Push, Pull, Discard, Unpull]` on B's
    outbound and that A sends no `Resolve`/`Reject`, but A's handler still
    completes (the work is a read, not a control op). Sync point uses a poll on
    A-side `FutureExportEntry.resolve_task_cancel_scope is None` to avoid wall-
    clock timing.

### 2i. Misc polish / bounce-back / as_client / as_server — ✅ done

- [x] Bounce-back identity test: `TestHandleBounceBackIdentity` — A's handler receives
    back an `is`-identical local handle when B echoes it via a `Call` arg.
- [x] `as_client` inbound-rejection test: `TestNullMainInterfaceRejection` —
    calling an `as_client` peer's main interface surfaces
    `IRException(category="NullMainInterface")` on the caller. No change needed
    to `NullMainInterface.dispatch`: the raised exception rides the existing
    `spawn_ir_future_in_task_group` → `set_exception` → `RejectMessage` path,
    and a `Call` without leases in `arg` has no cleanup to worry about.
- `as_server` side discard already covered by `TestSniffedMessageCount`
    (the immediate `their_main_lease.discard()` is what puts the
    `DiscardMessage(MAIN_GRANT_ID)` on the wire before shutdown).

### 2j. Best-effort `DiscardMessage` for handle/future imports — ✅ done

Deferred from §2f. Holding onto a `RemoteIRHandle` / `RemoteIRFuture` doesn't pin any
cross-process resource (leases are the only signal that keeps a peer-side `backing_lease`
alive), so this stage is pure optimization: it frees the peer's export-table slot earlier
so their re-export dict doesn't grow unboundedly under long-running sessions.

- [x] **Per-proxy counter, not `WeakSet`.** Frozen-dataclass proxies compare equal by
    `(conn, id)`, so a `WeakSet` would coalesce equal-but-distinct proxies. Instead,
    each `HandleImportEntry` / `FutureImportEntry` carries a
    `live_proxy_count: int = 0`. Every `RemoteIRHandle` / `RemoteIRFuture`
    `__post_init__` bumps the count and installs a `weakref.finalize` whose
    callback decrements it on GC; on the 1→0 transition with
    `introduction_refcount > 0` we enqueue
    `DiscardMessage(import_id, refcount=introduction_refcount)` and zero the
    count. `RemoteIRFuture.walk()` produces distinct proxy objects per walk
    path, which also bump/decrement independently — so a walked-and-held
    sub-future correctly keeps the import alive. The main interface's
    cached `_their_main_interface_proxy` naturally keeps the main handle
    import's count ≥1 for the connection's lifetime, so we never emit a
    spurious discard for ID 1. No special-casing needed for reserved IDs.
- [x] **`_on_discard` handler.** Already handled HandleExportEntry /
    FutureExportEntry teardown via `_tear_down_*_export` in §2f; no change
    required here.
- [x] **Tests.** `TestFutureProxyGCEmitsDiscard`,
    `TestHandleProxyGCEmitsDiscard`, `TestHandleReIntroductionAfterDiscard`.
    The re-introduction test polls A's export table to eliminate the race
    where B's second call could outrun A's processing of the first discard.
- Drain semantics (§2f) stay as-is: they're gated on grant import/export state, not
    handle/future state.
- **Follow-up observed during implementation**: §2h's
    `TestUnpullOnCancelledWait` test expected the outbound sequence
    `[Push, Pull, Discard, Unpull]`. After §2j, the cancelled waiter's
    `future` proxy is GC'd when the waiter task is torn down, appending a
    trailing `DiscardMessage` for the future import. The test was updated
    to expect `[Push, Pull, Discard, Unpull, Discard]`, with an explicit
    `gc.collect()` before the assertion to pin down the timing.

### 2j.5 — Proxy caches + paired token discards + `NullMainInterface` polish — ✅ done

Landed between §2j and §2k as a pre-doc-pass correctness + polish pass (2196 tests
passing). Three separate threads, all in one iteration:

- **`NullMainInterface.dispatch` lease-leak fix.** The placeholder main was
  raising `IRException` without walking the inbound `Call.arg` for leases
  — any lease in that arg would be stranded until the weakref finalizer
  fired, blocking drain in the meantime. Fixed by calling
  `discard_leases_in(request.arg)` on both the `request` and `dispatch`
  paths before raising. Tested by
  `TestNullMainInterfaceCleansUpRequestLeases`.
- **Proxy singleton caches.** `LeasedCapRPCConnection` now carries three
  `weakref.WeakValueDictionary`s keyed by import id (one per proxy kind),
  and every `Remote…` construction site routes through
  `_get_or_create_{token,handle,future}_proxy` helpers that return the
  cached singleton. This collapses per-access churn from `.token` /
  `.subject` property re-emissions down to a single proxy per id, and
  enables the pin-chain invariant below. `RemoteIRFuture` with a
  non-empty `attr_path` is deliberately *not* cached — each walk path is
  a distinct local view.
- **Paired token discards + pin chain.** Every `Exported…Expr` that
  carries a `token_export_id` is one introduction of the subject token
  on the peer's `token_export.outstanding_count`. For the peer to
  reclaim its `token_export` in step with dependent discards, we emit a
  *paired* `DiscardMessage(token_import_id, N)` from four paths:
    - handle proxy GC finalizer (→ `_dependent_decrement_and_maybe_discard`);
    - future proxy GC finalizer (same);
    - grant discard on last-live-lease drop (`_clean_up_lease_data`);
    - inbound `BreakMessage` on a grant import
      (`_mark_grant_import_broken` — skips the grant's own discard, but
      still emits the paired token discard).
  The four paths share `_emit_paired_token_discard` (in
  `lowering_remote_proxies.py`). A `RemoteGrant` / `RemoteIRHandle` /
  `RemoteIRFuture` `_token` field (populated in `__post_init__` via the
  cache helper) holds a strong reference to the subject token's
  singleton proxy, giving the pin-chain invariant: no standalone token
  proxy can GC while any dependent still references it. That ordering
  lets us use a single `introduction_refcount` on `TokenImportEntry`
  instead of splitting standalone vs. dependent contributions — when
  the last proxy finalizes, whatever `introduction_refcount` remains is
  guaranteed to be the standalone leftover. `__init__` and the three
  push-allocating sites (`RemoteIRHandle.dispatch`,
  `RemoteIRFuture.pipeline_dispatch`, `_on_push`) manually pair-bump
  the subject token's `introduction_refcount` / `outstanding_count` by
  `+= 2` to mirror the "every Exported…Expr bumps both" pattern that
  phase-1 naturally follows. Main-interface IDs 0/1 are pinned by
  `_their_main_token_proxy` + `_their_main_interface_proxy` strong
  refs, so their live_proxy_count stays ≥1 for the connection's
  lifetime and the finalizer path never emits a spurious main discard.

New tests: `TestNullMainInterfaceCleansUpRequestLeases`,
`TestBrokenGrantEmitsPairedTokenDiscard`, `TestPinChainKeepsTokenAlive`
+ paired-discard assertions added to existing
`TestHandleProxyGCEmitsDiscard`, `TestSniffedMessageCount`, and
`TestUnpullOnCancelledWait`.

### 2j.6 — Simplify: drop paired token discards, tighten proxy bookkeeping — ✅ done

Follow-up cleanup after review of §2j.5. Three threads:

- **Drop paired token-discard emission.** The `introduction_refcount`
  pair on both sides is a wire-clock, not a liveness counter — its only
  job is to make discard-vs-reintroduction races safe, not to mirror
  which dependents are live. That means the *timing* of token
  discharges can be decoupled from dependent discharges: we keep the
  protocol rule "every `Exported…Expr` with a `token_export_id` counts
  as one introduction of the subject token" (including on the
  importer's side's `token_entry.introduction_refcount`), but we no
  longer emit a paired `DiscardMessage(token_id, N)` every time a
  dependent retires. Instead, the subject token's share of
  introductions stays accumulated until the cached
  `RemoteLifetimeToken` proxy itself GCs; its finalizer emits one
  `DiscardMessage` for the full accumulated refcount, retiring every
  introduction in one shot. The pin chain
  (`RemoteGrant`/`RemoteIRHandle`/`RemoteIRFuture._token`) still
  guarantees the token proxy can't GC until every dependent has
  already emitted its own retirement, so the ordering remains correct.
  `_emit_paired_token_discard`, the paired block in
  `_clean_up_lease_data`, and the paired block in
  `_mark_grant_import_broken` are all deleted.
- **`_token` becomes a normal init field.** Previously declared on
  `RemoteGrant`/`RemoteIRHandle`/`RemoteIRFuture` as
  `init=False, default=None` + populated via `object.__setattr__` in
  `__post_init__`, with every `.token`/`.subject` accessor carrying a
  `None`-check that raised. Now it's a required init-time field with
  `compare=False, hash=False, repr=False`, supplied by the
  `_get_or_create_*_proxy` helpers (and by hand at the few direct
  construction sites in `__init__`, `dispatch`, `pipeline_dispatch`,
  and `lowering_evaluate`'s `ExportedLeaseExpr` path). The `setattr`
  dance and the `None`-check branches are gone.
- **Silent skips → asserts.** Every `if not isinstance(entry, …):
  return` in the proxy GC callbacks and `__post_init__`s becomes an
  `assert`, as do the `live_proxy_count < 0` and
  `max(0, introduction_refcount - N)` underflow clamps. These clamps
  were dressing up protocol-bookkeeping bugs as tolerance; we'd
  rather catch the bug loudly.

Test updates: `TestSniffedMessageCount` no longer expects the paired
main-token discard; `TestBrokenGrantEmitsPairedTokenDiscard` and
`TestHandleProxyGCEmitsDiscard` updated or retargeted accordingly;
`TestUnpullOnCancelledWait`'s expected sequence shortened. Any test
that directly constructed a `Remote…` proxy now passes `_token`
explicitly.

### 2j.7 — Follow-up: unified introduce helpers, remove-on-drain, stronger pin-chain test — ✅ done

Three threads, in the order listed. Each stands alone but the first
simplifies the surface the second has to reason about, and the third
is independent.

**(a) Unified `_introduce_*_import` helpers.** Today the introduction
path is split three ways: low-level `_register_*_import` on
`LeasedCapRPCConnection`, phase-1's `_phase1_register_*_import`
wrapping those with kind-mismatch protocol-violation handling, and
manual `token_entry.introduction_refcount += 2` pair-bumps at main-
init / `RemoteIRHandle.dispatch` / `RemoteIRFuture.pipeline_dispatch`
/ `_on_push`. The protocol rule "every `Exported…Expr` that carries a
``token_export_id`` is one introduction of both the dep and its
subject token" is duplicated across all these sites.

Refactor: introduce one helper per kind on the connection class that
owns the full "register-or-bump" semantics and the pair-bump:

- `_introduce_token_import(id)`
- `_introduce_handle_import(handle_id, token_id)`
- `_introduce_future_import(future_id, token_id)`
- `_introduce_grant_import(grant_id, token_id)`

Each helper: look up the entry; if missing, create at `refcount=1`
(also ensuring the subject token for dep kinds, via an inner
`_introduce_token_import`); if present and same kind, bump
`introduction_refcount += 1` (also bumping the subject token's
refcount for dep kinds); if present with wrong kind, raise a
dedicated exception (`PhaseOneKindMismatch` or similar) that phase-1
translates into `phase1_protocol_violation` and that other callers
let propagate as a bug.

Effects:

- Main-interface init becomes four sequential `_introduce_*` calls,
  no manual `+= 2`.
- Push paths (`dispatch`, `pipeline_dispatch`, `_on_push`) become
  three sequential `_introduce_*` calls, no manual `+= 2`.
- `_phase1_register_*_import` thin wrappers just call
  `_introduce_*` and translate the kind-mismatch exception.
- `_register_*_import` call sites collapse to zero outside the new
  helpers, so they can be inlined into the `_introduce_*` bodies
  (every current caller is an introduction). If anything needs
  non-introducing registration later we can resurrect them, but that
  context doesn't exist today.

**(b) Remove fully-drained entries from `_imports`.** Today
`_imports` is append-only for the connection's lifetime; several
silent "entry already gone" fallbacks (`RemoteLifetimeToken.ended` /
`register_end_callback` / the `deregister` closure;
`_clean_up_lease_data`'s kind-mismatch branch) nominally guard
against states that current code never actually produces. Under the
new scheme we pick well-defined removal points and those fallbacks
become strict asserts.

Removal points:

- **Token**: in `_on_token_proxy_gc` after emitting the final
  `DiscardMessage`, when `live_proxy_count == 0`,
  `introduction_refcount == 0`, `end_callbacks` is empty, and the
  id is not in `_active_token_imports` (so not pending a
  `_mark_token_import_ended`).
- **Handle**: in `_dependent_decrement_and_maybe_discard` (handle
  branch) after emitting the discard, when
  `live_proxy_count == 0 && introduction_refcount == 0`.
- **Future**: same site (future branch), when
  `live_proxy_count == 0 && introduction_refcount == 0 &&
  resolve_obligation_refcount == 0 && local_consumer_count == 0`,
  and `resolution` either `None` or in a terminal state.
- **Grant**: in `_clean_up_lease_data` after emitting the discard,
  when `live_leases` empty, `wait_break_callbacks` empty, and
  `introduction_refcount == 0` (covers both normal and broken
  paths — though the broken path doesn't emit an outbound
  `DiscardMessage`).

Pin chain guarantees deps' removal ordering: a token's
`live_proxy_count→0` only happens after every dependent's
`live_proxy_count→0`, so dep entries are already gone when we
consider removing the token entry.

After this lands, the following silent fallbacks become asserts:

- `RemoteLifetimeToken.ended` (line ~248), `register_end_callback`
  (line ~265), and the nested `deregister` closure (line ~283) —
  `entry is None` becomes "protocol bookkeeping is broken".
- `_clean_up_lease_data`'s `if not isinstance(entry,
  GrantImportEntry): return  # entry already gone` becomes
  `assert isinstance(entry, GrantImportEntry), …`.

New test: at least one unit verifying that a fully-drained token /
handle / future / grant entry does in fact get removed, and that
subsequent `Exported…Expr` re-introducing the same id re-registers
cleanly.

**(c) Rewrite `TestPinChainKeepsTokenAlive` to actually test the
pin chain.** Current test uses `_HandleServerMain.get_child` which
returns a child handle sharing the main token — so dropping the
child never triggers a token discard (main token has other pins).
Rewrite using a push result, which allocates a fresh subject
token:

- Side A: raw peer that sends `HelloMessage` and otherwise only
  reads (never resolves the push, no `ResolveMessage` back).
- Side B: `LeasedCapRPCConnection.as_client`. After handshake: call
  `their_main_interface.dispatch(their_main_lease.clone(),
  Call(…))` → `(future, result_lease)` pointing at a freshly
  allocated `(token, future, grant)` triple. Drop `result_lease`
  (→ `Discard(grant)`), drop `future` (→ `Discard(future)` after
  gc.collect), gc.collect again (→ `Discard(token)` now that no
  dep's `_token` pin remains), then cancel.

Assertions:

- Exactly one `Discard(grant_id, 1)`, one `Discard(future_id, 1)`,
  and one `Discard(token_id, 3)` (standalone +1, plus +1 each from
  future and grant dep contributions) appear on B→A.
- The `Discard(token_id)` appears *after* both dep discards —
  proves the pin chain ordering.
- Exactly one `Discard(token_id)`, not one per dep — proves the
  deferred-discharge property.

### 2k. Doc pass — ✅ done

Architecture docs were already largely written incrementally in the per-substage
landings (proxy caches, proxy-GC tracking, error-handling policy, table
invariants + active-set bookkeeping, resolve-obligation accounting, BreakMessage
propagation, drain semantics), so the §2k pass turned out to be a stock-take
rather than a rewrite:

- [x] `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` — updated "Implementation status"
    paragraph to reflect §2j.6 and §2j.7 landing (previously said "2a–2j.5
    complete"; now reads as Stage D feature-complete with Stage E + four
    deferred direct-entry-removal tests outstanding).
- [x] Root `CODE_OVERVIEW.md` — updated `fxtr2/rpc/` file listing to enumerate
    the post-`2c_post1` module split (`lowering_state.py`, `lowering_errors.py`,
    `lowering_remote_proxies.py`, `lowering_null_main.py`,
    `lowering_devaluate.py`, `lowering_evaluate.py`,
    `lowering_message_handlers.py`); clarified the prose section to mark Stage
    D feature-complete and point at Stage E (wire codec) as the next work.
- [x] `phase_d_prep_todos.md` — archived in place. Added an archive-note
    header at the top mapping each §1 / §2 / §3 item to its landing spot (most
    already applied; §3a/§3b promoted into §1c/§1d of this plan). Doc kept
    around for historical reference; not extended for new work.
- [x] `overview.md` — Stage D bullets / checklist updated (§2k bullet,
    "Implementation steps checklist" entries for D).
- [x] `phase_d_plan.md` §3 — full test inventory cross-referenced to the
    actual `tests/test_rpc/` + `tests/test_leases/` coverage; all pre-existing
    unchecked items do in fact have tests and have been checked off with file
    pointers.

## §3 — Test suite inventory (pulled from overview.md)

Final Stage D test checklist, satisfied by the sub-stages above. All items listed
below have coverage as of the §2k doc-pass stock-take; file references point into
`tests/test_rpc/test_lowering.py` unless otherwise noted.

- [x] Two in-process `LeasedCapRPCConnection` halves wired with an anyio memory object
    stream. (2a — `TestClientServerBootstrap`, `TestPropertyAccess`,
    `TestMainInterfaceRegistration`)
- [x] Main-interface round-trip call with primitive args/results. (2e —
    `TestMainInterfaceRoundTrip`)
- [x] Dispatch + pull + resolve. (2e — `TestDispatchThenWait`)
- [x] Sending a lease across, using it on the other side, discarding: refcount + discard
    messages work correctly. (2f — `TestLeaseAcrossWire`)
- [x] Pipelining: push a call on the result of an earlier push without awaiting. (2g —
    `TestPipelineDispatchBasic`, `TestBounceBackForwarding`)
- [x] `Break` on an exported grant propagates as a broken lease on the other side. (2f —
    `TestBreakPropagates`)
- [x] Connection close (abort) revokes all imported grants and closes all imported tokens.
    (2b + 2f — `TestAbortRevokesImports`)
- [x] Bounce-back identity: a handle/grant sent there-and-back compares identical to the
    original. (Grant: `TestPushAllocatedBounceBack` in 2e; handle:
    `TestHandleBounceBackIdentity` in 2i.)

Additional tests from this plan:
- [x] Pre-work 1b: `IRFutureLocalImpl.wait` rejects mismatched-grant lease.
    (`tests/test_leases/test_leased_cap_common.py::test_wait_rejects_mismatched_grant`,
    `::test_pipeline_dispatch_rejects_mismatched_grant`.)
- [x] Pre-work 1d: tokens/grants usable as dict keys.
    (`tests/test_leases/test_local_lease.py::test_local_token_is_dict_key`,
    `::test_local_grant_is_dict_key`, `::test_attached_output_grant_is_dict_key`.)
- [x] 2b: wrong first message / double-Hello / explicit-abort / transport-break.
    (`TestWrongFirstMessageAborts`, `TestDoubleHelloAborts`,
    `TestExplicitAbortTearsDownPeer`, `TestTransportBreakAborts`.)
- [x] 2c: devaluator round-trip, rollback on error, re-export dedup.
    (`tests/test_rpc/test_devaluate_phase1.py` — `TestDevaluatePhase1Primitives`,
    `…FreshLeaseExport`, `…BounceBackLease/Handle/Token`, `…ReExportDedup`,
    `…Rollback`, `…UnwrapForPipelining`, `…FreshFutureExport`.)
- [x] 2e: exception propagation, double-resolve aborts, multiple-wait dedupe.
    (`TestExceptionPropagation`, `TestDoubleResolveAborts`,
    `TestMultipleWaitsDeduplicatePull`, plus `TestRejectMessagePath` for the
    `RejectMessage` round-trip.)
- [x] 2f: re-export grant mid-session (refcount=2), watch/closed, break racing discard.
    (Re-export dedup / refcount=2 exercised via `TestLeaseAcrossWire` +
    `TestDevaluatePhase1ReExportDedup`; watch/closed via `TestWatchClosed` and
    `TestPeerDiscardOfWatchedToken`; break racing discard via
    `TestBreakRacingDiscard` and `TestBrokenGrantSuppressesItsOwnDiscard`.)
- [x] 2g: pipeline_dispatch basic shape + bounce-back forwarding (pre- and post-resolved variants).
    (`TestPipelineDispatchBasic`, `TestBounceBackForwarding`.)
- [x] 2h: discard-before-resolve ordering, unpull on cancelled wait.
    (`TestWaitDiscardsLeasePromptly`, `TestUnpullOnCancelledWait`.)
- [x] 2i: handle bounce-back, as_client placeholder rejection. (as_server discard
    is already covered by `TestSniffedMessageCount`.)
    (`TestHandleBounceBackIdentity`, `TestNullMainInterfaceRejection`,
    `TestAsClientNullInterface`.)
- [x] 2j: future proxy GC emits DiscardMessage; handle proxy GC emits
    DiscardMessage; re-introduction after discard allocates fresh export id.
    (`TestFutureProxyGCEmitsDiscard`, `TestHandleProxyGCEmitsDiscard`,
    `TestHandleReIntroductionAfterDiscard`.)
- [x] 2j.5: `NullMainInterface` cleans up request leases before raising; paired-discard
    assertions on existing GC + sniff tests.
    (`TestNullMainInterfaceCleansUpRequestLeases` plus the paired-discard
    assertions layered into `TestHandleProxyGCEmitsDiscard`,
    `TestSniffedMessageCount`, `TestUnpullOnCancelledWait`.)
- [x] 2j.7: unified `_introduce_*_import` helpers (via phase-1 wrappers);
    handle import entry removed from `_imports` on proxy GC
    (`TestImportEntryRemoval`); push-triple discard sequence with pin-chain
    ordering (`TestPinChainKeepsTokenAlive`).

### Planned tests (deferred from §2k; landed after the doc pass)

- [x] Grant import entry removal after all leases + wait-break subscriptions
  drain: `TestImportEntryRemoval::test_grant_entry_removed_after_wait_completes`
  exercises the `wait` path (pin wait-break + lease drain in sequence) and
  asserts the grant entry is gone from `_imports` once `wait` returns.
- [x] Future import entry removal after the proxy GCs with no pending
  obligations: `TestImportEntryRemoval::test_future_entry_removed_after_proxy_gc`
  completes the round-trip, drops every local reference to the push triple,
  forces GC, and asserts the future entry is removed.
- [x] Token import entry removal after its final `DiscardMessage` emits:
  `TestImportEntryRemoval::test_token_entry_removed_after_final_discard`
  drains the grant and future via the same round-trip, breaks the
  `LocalPendingResultBox` ↔ deregister-closure cycle with multiple
  `gc.collect()` rounds, and asserts the token entry is removed once the
  token proxy's finalizer has fired.
- [x] Mirror-image importer test for re-introduction after discard:
  `TestImporterReIntroductionAfterDiscard::test_fresh_entry_registered_after_removal`
  — the importer-side complement to `TestHandleReIntroductionAfterDiscard`.
  After the first handle proxy GCs and the entry is removed from the
  importer's `_imports`, a subsequent handle introduction at a fresh id
  registers a new `HandleImportEntry` cleanly (distinct object,
  `live_proxy_count=1`, `introduction_refcount=1`).

## Implementation order summary

1. §1a — `ExportedLeaseExpr.token_export_id`. ✅
2. §1b — `LocalPendingResultBox` grant-validation extraction. ✅
3. §1c — `_LeaseCleanupInfo` generification. ✅
4. §1d — `LifetimeToken`/`Grant` hashable-safe. ✅
5. §2a — Scaffold + handshake-skip startup. ✅
6. §2b — Hello/Abort. ✅
7. §2c — Devaluator + phase 1. ✅
8. §2c_post1 — File-split refactor. ✅
9. §2c_post2 — Message-handler extraction into `lowering_message_handlers.py`. ✅
10. §2d — Phase 2 evaluators. ✅
11. §2e — Push/pull/resolve round-trip. ✅
12. §2f — Leases across the wire. ✅
13. §2g — Pipelining. ✅
14. §2h — Unpull + obligation-accounting reshape (drain gating, no-lease waits,
    survival-lease, phase-1/devaluator AwaitFutureExpr tracking). ✅
15. §2i — Bounce-back + client/server conveniences. ✅
16. §2j — Best-effort `DiscardMessage` for handle/future imports. ✅
17. §2j.5 — Proxy singleton caches + paired token discards + `NullMainInterface`
    lease-leak fix. ✅ (not in original plan; landed as a pre-§2k polish pass —
    see the "§2j.5" discussion below.)
18. §2j.6 — Drop paired token discards, tighten proxy bookkeeping. ✅
19. §2j.7 — Unified `_introduce_*_import` helpers, remove-on-drain, stronger
    pin-chain test. ✅ (landed in order: (a) helpers, (b) removal, (c) test.)
20. §2k — Doc pass. ✅

Final "Update CODE_OVERVIEW.md" step at the end of 2k. ✅
