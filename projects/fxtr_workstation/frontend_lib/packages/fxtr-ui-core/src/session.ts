/**
 * Abstract Session / Connector surfaces. Connector-specific concrete
 * implementations live in connector packages (see e.g.
 * `@fxtr-ui/connector-workstation`).
 *
 * Not every operation here is guaranteed to be implemented by every
 * connector — impls may throw `NotImplementedError` for ctx methods
 * they don't support (e.g. an HTTP connector's `FxtrSessionUpdateCtx`
 * throws from `allocateInvoke` because HTTP has no services). See
 * `plans/phase0/part3_frontend_ui/design_notes/02_connector_abstraction.md`
 * for the full design.
 */

import type { Atom, WritableAtom, getDefaultStore } from "jotai";
import type { EntityMetadata, ServiceMetadata } from "./metadata";
import type {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	OpResultStub,
	ServiceRefStub,
} from "./stubs";
import type {
	InvokeArgValue,
	JsonValue,
	MethodArgs,
	MethodKwargs,
	MethodNames,
	MethodReturns,
	ResultValue,
	StateValue,
} from "./types";

/**
 * Thrown by implementations whose surface is declared on an
 * interface but whose behavior is unavailable in this impl.
 */
export class NotImplementedError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "NotImplementedError";
	}
}

/**
 * The contents of the wire `{user: ...}` envelope — the user's
 * state tree, of any legal `StateValue` shape. The dict-rooted case
 * (an object whose keys are the user's named slots) is the common
 * one, but arrays and even single-stub roots are legal too — the
 * protocol doesn't constrain it.
 *
 * Values are conventionally immutable; `StateValue`'s container
 * arms enforce this at the type level, but `Uint8Array` leaves are
 * mutable in TS and should be treated as read-only by convention.
 * Mutations produce new state values, which the reducer diffs to
 * derive the wire mutation.
 */
export type UserState = StateValue;

/**
 * Observable lifecycle phases of a `Session`. The full set is shared
 * across backends so consumer code that reacts to lifecycle changes
 * is backend-agnostic. Backends that don't have a reconnect cycle
 * (e.g. an HTTP connector) simply never enter `"stale"`. Backends
 * that can't observe a "session doesn't exist" condition (HTTP-
 * degenerate single-session) won't enter `"unavailable"` either.
 *
 *   connecting  - subscribe/load in flight; no snapshot yet.
 *                 `userState` is `null`.
 *   connected   - snapshot received; live state is current.
 *   stale       - had a snapshot, lost the connection; awaiting
 *                 fresh data. `userState` retains its last value.
 *   closed      - terminal — the session was successfully attached
 *                 (or created) and is now closed (consumer detached,
 *                 server-side terminated, or the connector closed).
 *                 `userState` retains its last value.
 *   unavailable - terminal — the subscription itself failed. The
 *                 session_id is not known to the server: it was never
 *                 created, expired, or was destroyed before this
 *                 consumer attached. `userState` is `null`. Distinct
 *                 from `closed` so consumers can render a
 *                 "session no longer available" body without
 *                 conflating it with a normal close.
 */
export type SessionLifecycleState = "connecting" | "connected" | "stale" | "closed" | "unavailable";

/**
 * Passed to a write's `onRollback` callback (and as the
 * `MutationRolledBackError.info`) when a mutation doesn't commit.
 * Two-way classification:
 *
 *   - `retryable`: the mutation didn't commit because of a
 *     transient or recoverable condition — remote state changed
 *     (the caller could rebase against current state and re-issue),
 *     the server was transiently busy, the transport dropped.
 *   - `permanent`: the mutation can't succeed by retrying — a
 *     validation error, a closed/destroyed session, a protocol-
 *     level bug. The caller should surface the failure / log,
 *     not retry.
 *
 * `message` is a human-readable description. The connector layer
 * may include the server's message verbatim or a transport-error
 * `.message`.
 */
export interface RollbackInfo {
	readonly kind: "retryable" | "permanent";
	readonly message: string;
}

/**
 * Error type thrown when a Promise-returning mutation method
 * (e.g. `WorkstationSession.terminate()`) is rolled back. The
 * `info` field carries the same `RollbackInfo` that `onRollback`
 * callbacks receive.
 */
export class MutationRolledBackError extends Error {
	readonly info: RollbackInfo;
	constructor(info: RollbackInfo) {
		super(info.message);
		this.name = "MutationRolledBackError";
		this.info = info;
	}
}

/**
 * Optional second arg to `set(session.userState, value, options)`.
 */
export interface UserStateWriteOptions {
	/**
	 * Fired if the mutation doesn't commit (server reject,
	 * connection drop, etc.). Receives a `RollbackInfo`
	 * describing why. Multiple writes in the same flush coalesce
	 * into one mutation; on rejection, all their `onRollback`s
	 * fire in registration order (oldest first) so consumers
	 * doing rebase-style retry rebuild forward.
	 */
	readonly onRollback?: (info: RollbackInfo) => void;
}

/**
 * Context passed as the second argument to function-form `FxtrUpdate`
 * updaters. Bound to the session that owns the atom being written.
 *
 * The slot-allocation methods (`allocateLoad` / `allocateInvoke` /
 * `allocateCallback`) live here rather than on the public `Session`
 * surface so the same-tick contract is structural: an allocated slot
 * is only useful if you store it into state on the same write, and
 * the only way to get a slot is from within an updater, so "same
 * tick" falls out for free. The `session` escape hatch is kept for
 * advanced use (reading `lifecycle` inline, passing the session to a
 * helper); most consumers won't touch it.
 */
export interface FxtrSessionUpdateCtx {
	/**
	 * Allocate the receive side of a load op against the
	 * ctx-owning session. Returns a fresh `OpResultStub` whose slot
	 * the reducer will surface as a `LoadEntityOp` when the stub is
	 * first written into state.
	 *
	 * Same-tick contract: the returned stub must end up referenced
	 * from the new value returned by the surrounding updater, or the
	 * slot is dead after the next flush. The updater shape enforces
	 * this by construction (you call `allocateLoad` and store its
	 * result in the value you `return`, all in the same synchronous
	 * function body).
	 *
	 * Sessions intern: a second `allocateLoad` for the same
	 * already-pinned entity returns the existing `OpResultStub`. See
	 * design_notes/05_dispatch_and_inline_loading.md.
	 */
	allocateLoad<T>(ref: EntityRefStub<T>): OpResultStub<T>;

	/**
	 * Allocate a one-shot invoke against the ctx-owning session.
	 * Returns a fresh `OpResultStub` whose slot the reducer will
	 * surface as an `InvokeOp` when the stub is first written into
	 * state. Same same-tick contract as `allocateLoad`.
	 *
	 * `args` is positional; `kwargs` is the optional keyword-arg
	 * dict. Both are walked and encoded for the wire at allocate
	 * time; values can be primitives, dicts, lists, `Uint8Array`,
	 * `EntityRefStub`s, `ServiceRefStub`s, or `CallbackHandleStub`s
	 * (the send-side of a callback inbox; pass `inbox.handle` rather
	 * than the inbox itself). `OpResultStub`s and `CallbackInboxStub`s
	 * are rejected (read the slot's value first, or pass
	 * `inbox.handle`).
	 *
	 * Unlike `allocateLoad`, invokes have no dedup — every call mints
	 * a fresh slot.
	 *
	 * The *target* may also be a `CallbackHandleStub` — the send side
	 * of one of this session's own callback inboxes (`inbox.handle`).
	 * That invokes a method *on the inbox itself* (e.g. `subscribe_to`
	 * to subscribe it to an event source) and works even when the
	 * inbox was allocated in the same tick — the reducer emits a
	 * callback-slot-targeted `CallbackInvokeOp` instead of an
	 * `InvokeOp`. Handle targets use loose types (no `Method<…>`
	 * protocol inference).
	 */
	allocateInvoke<P, M extends MethodNames<P>>(
		svc: ServiceRefStub<P>,
		method: M,
		args: MethodArgs<P, M>,
		kwargs?: MethodKwargs<P, M>,
	): OpResultStub<MethodReturns<P, M, ResultValue>>;
	allocateInvoke<R = ResultValue | null>(
		target: CallbackHandleStub<unknown>,
		method: string,
		args: readonly InvokeArgValue[],
		kwargs?: { readonly [key: string]: InvokeArgValue },
	): OpResultStub<R>;

	/**
	 * Allocate the receive side of a metadata query against
	 * `ref`'s entity. Returns a fresh `OpResultStub<EntityMetadata>`
	 * whose slot the reducer will surface as a `GetEntityMetadataOp`
	 * when the stub is first written into state. Same same-tick
	 * contract as `allocateLoad`.
	 *
	 * **Deterministic + dedup'd** (mirror of `allocateLoad`): a
	 * second `allocateGetEntityMetadata` for the same `shortId`
	 * returns the existing stub. The wire-visible
	 * `entityMetadataDedup` envelope field makes this dedup work
	 * across foreign mutators and across `attachSession` boundaries.
	 * Distinct from `allocateLoad`'s slot pool — a single entity
	 * yields different slots for load vs. metadata.
	 *
	 * The result event's `metadata` payload is decoded as an
	 * `EntityMetadata` value with ref fields as `EntityRefStub`
	 * instances.
	 */
	allocateGetEntityMetadata(ref: EntityRefStub): OpResultStub<EntityMetadata>;

	/**
	 * Allocate the receive side of a metadata query against
	 * `svc`'s service. Returns a fresh
	 * `OpResultStub<ServiceMetadata>`; the reducer surfaces a
	 * `GetServiceMetadataOp` when the stub is first written into
	 * state. Same same-tick contract and dedup semantics as
	 * `allocateGetEntityMetadata` (wire-visible
	 * `serviceMetadataDedup`).
	 */
	allocateGetServiceMetadata(svc: ServiceRefStub<unknown>): OpResultStub<ServiceMetadata>;

	/**
	 * Allocate the receive side of a callback against the ctx-owning
	 * session. Storing the returned inbox stub into state fires the
	 * corresponding `CreateCallbackOp`; removing every inbox stub for
	 * the slot fires `CloseCallbackOp`.
	 */
	allocateCallback<R>(): CallbackInboxStub<R>;

	/**
	 * The session that owns the atom this updater is writing to.
	 * Escape hatch: lets an updater read other session atoms (e.g.
	 * `session.lifecycle`) inline, or pass the session reference to a
	 * helper. Most consumers should not need this — the ctx methods
	 * cover the common cases.
	 */
	readonly session: Session;
}

/**
 * Updater shape accepted by a `FxtrSessionAtom<T>` write. Either a
 * static value, or a function `(prev, ctx) => next` where `ctx` is a
 * `FxtrSessionUpdateCtx` bound to the session owning the atom.
 *
 * Mirrors React's `SetStateAction<T>`, extended with the ctx
 * argument so function-form updaters can allocate slots in the same
 * write that stores them.
 */
export type FxtrUpdate<T> = T | ((prev: T, ctx: FxtrSessionUpdateCtx) => T);

/**
 * The shape we expose for any session-bound writable atom: reads
 * yield `T`; writes accept a `FxtrUpdate<T>` plus optional
 * `UserStateWriteOptions`. `Session.userState` is a
 * `FxtrSessionAtom<UserState | null>`; chunk 4's `subAtom` produces
 * values of this type when given a parent of this type.
 */
export type FxtrSessionAtom<T> = WritableAtom<T, [FxtrUpdate<T>, UserStateWriteOptions?], void>;

/**
 * A single workstation session (or, for an HTTP connector, a
 * degenerate single-session surface). One reactive atom carries the
 * authoritative state; mutations flow through the reducer.
 */
export interface Session {
	/** The session's UUID. */
	readonly sessionId: string;

	/**
	 * The session's own jotai store. Per-session — sessions never
	 * share with their connector or with sibling sessions. The
	 * React layer's `<FxtrSessionProvider>` plugs this into a
	 * `<JotaiProvider>` for the panel's scope, so component-internal
	 * atoms inside one panel are isolated from sibling panels'.
	 * All of the session's own atoms (`userState`, `lifecycle`, …)
	 * have their authoritative values in this store.
	 */
	readonly store: ReturnType<typeof getDefaultStore>;

	/**
	 * Read-only atom of the session's lifecycle state. Use
	 * `useAtomValue` (React) or `store.get(session.lifecycle)`
	 * (sync). Writes are rejected at runtime — the atom is a
	 * derived read-only view over the session's internal state.
	 */
	readonly lifecycle: Atom<SessionLifecycleState>;

	/**
	 * Writable atom of the session's user state — the inside of
	 * the wire `{user: ...}` envelope. Reads return the optimistic
	 * (committed + local-pending) view; writes go through the
	 * connector's mutation reducer, which:
	 *
	 *   - resolves the value or updater against the current
	 *     local view (passing a `FxtrSessionUpdateCtx` to
	 *     function-form updaters so they can allocate slots inline);
	 *   - microtask-coalesces with any concurrent writes;
	 *   - diffs against the last-committed state, builds a JSON
	 *     patch + Track/Untrack ops bundle, ships a
	 *     `MutateSessionRequest`;
	 *   - on commit, folds the in-flight state into committed;
	 *   - on reject (or connection drop), reverts the optimistic
	 *     view to committed and fires accumulated `onRollback`
	 *     callbacks in registration order.
	 *
	 * `null` is both "no snapshot yet" and "user state set to
	 * explicit null." In practice user state is almost always a
	 * dict, so the ambiguity rarely matters.
	 *
	 * Slot allocation is only reachable through the ctx passed to
	 * function-form updaters — there is no public imperative
	 * `session.allocateLoad(...)` surface. The same-tick contract
	 * (slots must be referenced from the value the updater returns,
	 * or they're dead after the next flush) is enforced
	 * structurally by the API shape.
	 */
	readonly userState: FxtrSessionAtom<UserState | null>;

	/**
	 * Release the consumer's handle on this session. Connector-
	 * specific semantics: the workstation connector blocks new
	 * writes, waits for any in-flight mutation to commit or
	 * reject (so the consumer learns the outcome of every write
	 * they registered before detaching), then sends the
	 * unsubscribe and flips lifecycle to `closed`. The server-
	 * side session is *not* destroyed — another consumer could
	 * re-attach to the same `session_id`.
	 *
	 * Idempotent. The returned promise resolves once detachment
	 * is complete (drain finished, lifecycle is `closed`).
	 */
	detach(): Promise<void>;

	/**
	 * Destroy the session — the counterpart to `detach()`'s "release
	 * the handle but keep the session." Connector-specific semantics:
	 *
	 *   - The workstation connector ships a `CloseOp` mutation that
	 *     deletes the server-side session; commit flips lifecycle to
	 *     `closed`. The returned promise resolves on commit and rejects
	 *     with `MutationRolledBackError` if the close is rolled back.
	 *   - The HTTP connector destroys the session's *local* state:
	 *     unregisters its inboxes (best-effort `unsubscribe`), deletes
	 *     its persisted storage blob + registry entry, and flips
	 *     lifecycle to `closed`. It never rejects (there is no remote to
	 *     refuse the close).
	 *
	 * Optional `finalState` writes a parting user-state change in the
	 * same close (e.g. a "closed because…" annotation), observable by
	 * any jotai subscriber before lifecycle goes `closed`. Optional
	 * `onRollback` fires only on backends that can reject the close
	 * (the workstation); HTTP never invokes it.
	 *
	 * Consumed by the generic panel-viewer's terminate-on-panel-close
	 * contract — closing a session-bearing panel destroys its session
	 * regardless of connector. See
	 * `plans/phase0/part3_frontend_ui/design_notes/02_connector_abstraction.md`.
	 */
	terminate(finalState?: UserState, options?: UserStateWriteOptions): Promise<void>;
}

/**
 * Observable connectivity of a `Connector`'s transport, surfaced for
 * UI (e.g. the shell header). Distinct from a `Session`'s lifecycle —
 * transport health is independent of any single session's state.
 * Backends without a reconnect cycle (e.g. a connect-on-demand HTTP
 * connector) only ever report `"connecting"` / `"open"` / `"closed"`.
 *
 *   connecting   - an initial connection attempt is in flight.
 *   open         - the transport is connected; or, for a lazily-
 *                  connected backend, nothing is open but it is ready.
 *   reconnecting - a previously-open transport dropped and is retrying.
 *   closed       - terminal; the connector was closed.
 */
export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

/**
 * The consumer-facing surface shared by every connector
 * (`WorkstationConnector`, `HttpConnector`, …): the post-construction
 * API a viewer frontend (e.g. the PanelViewer) needs from a backend,
 * sibling to the abstract `Session`. The *constructor* side stays
 * per-connector (each takes bespoke construction args), and concrete
 * connectors may expose supersets of this (debug-projected lists, a
 * raw transport handle, …) without breaking assignability.
 *
 * Identifier-interning contract: `entityRef` / `serviceRef` are
 * idempotent — the same `shortId` returns the same stub instance,
 * across sessions, for the connector's lifetime.
 */
export interface Connector {
	/** Connector-level jotai store (distinct from per-session stores). */
	readonly store: ReturnType<typeof getDefaultStore>;

	/** Display label for the backend (e.g. its base URL). */
	readonly url: string;

	/** Read-only atom of the transport's connectivity, for UI. */
	readonly connectionStatus: Atom<ConnectionStatus>;

	/** Intern (or look up) the entity-ref stub for a shortId. */
	entityRef<T = unknown>(shortId: string): EntityRefStub<T>;

	/** Intern (or look up) the service-ref stub for a shortId. */
	serviceRef<T = unknown>(shortId: string): ServiceRefStub<T>;

	/**
	 * Classify a shortId as naming an entity or a service (`null` if
	 * unknown). The sole ref-link consumer needing entity-vs-service;
	 * answered authoritatively per connector (the HTTP connector from
	 * its shortId table, the workstation from its resource lists) — no
	 * reliance on shortId-prefix syntax.
	 */
	classifyShortId(shortId: string): "entity" | "service" | null;

	/** Load + decode an entity's data through the connector's transport. */
	loadEntityData<T = unknown>(stub: EntityRefStub<T>): Promise<T>;

	/** Create a fresh session, returning the live handle. */
	createSession(metadata: JsonValue): Promise<Session>;

	/** Re-attach to an existing session by id. */
	attachSession(sessionId: string): Session;

	/** Close the connector + its transport. Idempotent. */
	close(): void;
}
