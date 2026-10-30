/**
 * Abstract stub classes that appear at leaves of a session's
 * structured state. Concrete subclasses (carrying connector-specific
 * identity) live in connector packages — see e.g.
 * @fxtr-ui/connector-workstation. The base classes here exist so the
 * core's wire codec, state walks, and `instanceof` checks work
 * independently of any particular connector.
 *
 * See plans/phase0/part3_frontend_ui/design_notes/03_stub_types.md
 * for the full semantics.
 */

import { type Atom, atom } from "jotai";

// Unique-symbol brands give each class a phantom field, which makes
// the otherwise-unused type parameter T variance-meaningful (so
// `EntityRefStub<DemoNote>` is not silently assignable to
// `EntityRefStub<OtherNote>`) and makes the stub classes nominally
// distinct (otherwise their structural shapes would collapse — they'd
// all be "object with a `shortId` getter" or similar).
declare const ENTITY_REF_BRAND: unique symbol;
declare const SERVICE_REF_BRAND: unique symbol;
declare const OP_RESULT_BRAND: unique symbol;
declare const CALLBACK_INBOX_BRAND: unique symbol;
declare const CALLBACK_HANDLE_BRAND: unique symbol;

/**
 * Reference to a workstation-tracked entity. The type parameter `T`
 * is type-level only — it describes the shape of the entity's loaded
 * data for typechecking; the runtime carries no type information.
 */
export abstract class EntityRefStub<T = unknown> {
	declare readonly [ENTITY_REF_BRAND]: T;

	/**
	 * Connector-scoped identifier used for ref tracking and as the
	 * wire short_id. For the workstation connector this is a
	 * workstation short_id (e.g. `"da1b2c"` or `"da1b2c@2"`).
	 */
	abstract get shortId(): string;

	/**
	 * Short human-readable summary used by inspection / debug-UI
	 * tooling (e.g. `@fxtr-ui/state-util`'s `stringifyForDebug`).
	 * Stub classes own their formatting so subclasses with richer
	 * identity (e.g. version-suffixed short_ids) can override.
	 * **Not** a wire codec — wire encoding lives in connector
	 * packages.
	 */
	debugSummary(): string {
		return `<EntityRef ${this.shortId}>`;
	}
}

/**
 * Reactive liveness of a referenced service.
 *
 * - `alive` — the service is usable (default).
 * - `broken` — the grant backing the workstation's bridge to the
 *   service was revoked / its RPC connection dropped; invokes on it
 *   will fail. One-way and terminal — a client that wants a working
 *   service obtains a fresh ref.
 */
export type ServiceState = "alive" | "broken";

/**
 * Shared constant `"alive"` atom returned by the base `state` getter.
 * Stable identity so consumers can rely on referential equality.
 */
const ALIVE_SERVICE_STATE_ATOM: Atom<ServiceState> = atom<ServiceState>("alive");

/**
 * Reference to a workstation-tracked service. The type parameter `T`
 * is type-level only — a service protocol type for invoke
 * typechecking.
 */
export abstract class ServiceRefStub<T = unknown> {
	declare readonly [SERVICE_REF_BRAND]: T;

	abstract get shortId(): string;

	/**
	 * Reactive liveness of the referenced service. The base returns a
	 * constant `"alive"` atom; connector subclasses that track
	 * breakage (e.g. the workstation connector's connector-level broken
	 * set) override this so the UI can reactively render / disable a
	 * dead service or trigger an effect (e.g. resubscribe).
	 */
	get state(): Atom<ServiceState> {
		return ALIVE_SERVICE_STATE_ATOM;
	}

	/** See `EntityRefStub.debugSummary`. */
	debugSummary(): string {
		return `<ServiceRef ${this.shortId}>`;
	}
}

/**
 * Reactive snapshot of an op-result slot. Three terminal-ish states
 * the consumer reads through the stub's `result` atom:
 *
 *   pending — the load (or other op) is in flight or has not been
 *             scheduled yet; the slot has no fill on file.
 *   filled  — the result event arrived; `value` is the structured
 *             decoded payload.
 *   errored — the op failed (server returned an error event), the
 *             payload couldn't be decoded, the stub was flagged
 *             broken (cross-session smuggling, missing op-spec, etc),
 *             or the slot points at the session's dead-sentinel.
 *
 * The type parameter `T` is best-effort: the structural shape of the
 * loaded value when `status === "filled"`. At runtime types are
 * erased — consumers that need a strong guarantee should validate
 * the value (e.g. via a `$fxtr.type` dispatch).
 */
export type ResultStatus<T = unknown> =
	| { readonly status: "pending" }
	| { readonly status: "filled"; readonly value: T }
	| { readonly status: "errored"; readonly message: string };

/**
 * Pending or filled result of a load / invoke / metadata op.
 *
 * Public surface is the three derived atoms (`result`, `committed`,
 * `done`); the slot integer and session backref are connector-
 * specific and live on concrete subclasses. The base class exists
 * here so framework-agnostic consumers can read the atoms without
 * importing connector-specific types, and so state walks can
 * `instanceof OpResultStub` to identify slot leaves.
 *
 * See plans/phase0/part3_frontend_ui/design_notes/03_stub_types.md.
 */
export abstract class OpResultStub<T = unknown> {
	declare readonly [OP_RESULT_BRAND]: T;

	/**
	 * The slot's current resolution status: `pending`, `filled`, or
	 * `errored`. Reactive — subscribing components re-render when
	 * the slot's fill arrives (or its error / broken state).
	 */
	abstract get result(): Atom<ResultStatus<T>>;

	/**
	 * `true` iff the slot is referenced in the session's
	 * server-committed user state — i.e. the mutation that
	 * introduced the slot has been ack'd. Reactive.
	 */
	abstract get committed(): Atom<boolean>;

	/**
	 * `true` iff the slot's `result` is not pending (it either
	 * filled successfully or errored). Convenience derived atom.
	 */
	abstract get done(): Atom<boolean>;

	/**
	 * Short human-readable summary used by inspection / debug-UI
	 * tooling. The abstract base has no identity to include —
	 * concrete subclasses (e.g. `WorkstationOpResultStub`) override
	 * to add their slot integer. See `EntityRefStub.debugSummary`.
	 */
	debugSummary(): string {
		return "<OpResultStub>";
	}
}

/**
 * Receive-side cursor over a workstation callback's per-slot event
 * log. Conceptually `(slot, offset)`: the slot identifies the
 * underlying callback record; the offset is "events consumed by this
 * cursor so far." Multiple inbox stubs can coexist in session state
 * with different offsets — each is an independent reader. The
 * underlying event log is in the session's local `inboxLogs` map,
 * populated from preserved `CallbackEvent`s on the wire.
 *
 * The abstract base is **opaque**: no public `slot` / `offset`
 * getters. Slot is a workstation-specific identity; offset is
 * encapsulated by the cursor-manipulation methods (`sliced`,
 * `cleared`). Wire encoding accesses the connector identity through
 * encoder-opt callbacks (see `escape.ts`'s `encodeInbox`), so the
 * core never depends on a particular connector's identity scheme.
 *
 * Cursor advance produces a new stub value:
 *
 *   - `inbox.sliced(n)` — new inbox stub with offset advanced by n.
 *
 * "Clear all visible events" is `inbox.sliced(events.length)` where
 * `events` is the result of a reactive read (e.g. `useAtomValue` or
 * a derived atom's `get(inbox.events)`). For "clear at action time
 * including events that may have arrived since render," compose a
 * custom jotai write atom that reads `inbox.events` and slices at
 * action-fire time — there is intentionally no built-in `cleared()`
 * so the snapshot point is always explicit in user code.
 *
 * The user commits a cursor advance by writing the new stub back to
 * its state location, optionally bundling reduced state for the
 * consumed events into the same mutation.
 *
 * Closing the callback is "remove every inbox stub for the slot
 * from state": the reducer's diff produces `CloseCallbackOp` and
 * the per-slot event log is dropped.
 *
 * The `handle` getter produces the matching send-side stub — pass
 * `inbox.handle` (not `inbox` itself) as an invoke arg when the
 * callee should be able to notify into this callback.
 *
 * The type parameter `R` is best-effort: the structural shape of
 * each event payload. Runtime payloads are wire-decoded
 * structurally; consumers needing a strong guarantee should
 * validate.
 *
 * See plans/phase0/part3_frontend_ui/design_notes/03_stub_types.md.
 */
export abstract class CallbackInboxStub<R = unknown> {
	declare readonly [CALLBACK_INBOX_BRAND]: R;

	/**
	 * The cursor's current offset: the absolute count of events for
	 * this callback slot that this cursor has consumed. Exposed
	 * because the user is the one managing it (via `sliced(n)`) and
	 * may want to read it when computing relative advances, building
	 * debug output, or comparing two stubs for the same slot.
	 *
	 * The slot's full delivered history is indexed absolutely from
	 * 0; a cursor at `offset = N` shows events `N..` onward. The
	 * underlying slot identifier is *not* exposed — that's
	 * connector-specific identity.
	 */
	abstract get offset(): number;

	/**
	 * Events from `offset` onward, in seq order. Reactive — re-emits
	 * when new `CallbackEvent`s arrive for this slot or when a
	 * cursor advance shrinks the visible window.
	 */
	abstract get events(): Atom<readonly R[]>;

	/**
	 * Lifecycle state of the callback:
	 *
	 *   opening — the stub is in local user state but the
	 *             `CreateCallbackOp` for its slot hasn't committed
	 *             yet (or has been rolled back).
	 *   open    — the slot is in server-committed state; the callback
	 *             is live and the callee can notify into it.
	 *   closed  — the stub is the session's dead-sentinel
	 *             (cross-session smuggling, etc.) — the underlying
	 *             slot will never deliver events for this stub.
	 *
	 * Reactive. No server-driven "closed" today (e.g. Python service
	 * unregistered the bridged ref) — that would need new wire
	 * signal; see the stage-6 design doc's deferred items.
	 */
	abstract get state(): Atom<"opening" | "open" | "closed">;

	/**
	 * Connection state of the inbox's `subscribe_to` feed — orthogonal
	 * to `state` (the slot's open/closed lifecycle):
	 *
	 *   idle      — no *live* subscription is established for this slot
	 *               (the default).
	 *   connected — a `subscribe_to` subscription is live.
	 *   broken    — the live subscription's upstream source broke
	 *               ungracefully; the inbox has stopped receiving events
	 *               until the client `subscribe_to`s a fresh source.
	 *
	 * Reactive; latest-wins (the workstation reduces it from the server's
	 * `CallbackSubscriptionEvent` stream; the stateless HTTP connector
	 * synthesizes the same transitions locally). When `broken`,
	 * `subscriptionBreakReason` carries the reason string.
	 *
	 * **A source that cannot push events keeps the inbox `idle`.** Only a
	 * source backed by a live event channel (a workstation event-source
	 * service; a `ws`-descriptor stateless service) can transition to
	 * `connected` / `broken`. If you `subscribe_to` something that is not
	 * actually subscribable — a service that statically has no events, or
	 * a stateless `http`/GET-backed service with no push channel — the
	 * subscribe is inert and `connectionState` simply stays `idle`
	 * (forever): there is no live channel to connect or to break. This is
	 * uniform across both connectors, so the same frontend code drives
	 * either: a GET-backed service just never emits, and can later be
	 * swapped to a ws service with no client change.
	 */
	abstract get connectionState(): Atom<"idle" | "connected" | "broken">;

	/**
	 * The break reason reported with the most recent `broken`
	 * transition, or `null` when not currently broken. Reactive.
	 */
	abstract get subscriptionBreakReason(): Atom<string | null>;

	/**
	 * `true` iff the slot is referenced from the session's
	 * server-committed user state — i.e. the mutation that
	 * introduced this slot's first stub has been ack'd. Reactive.
	 */
	abstract get committed(): Atom<boolean>;

	/**
	 * Matching send-side handle for embedding in invoke args. Pass
	 * `inbox.handle` (not `inbox`) to `allocateInvoke` to give the
	 * callee a way to notify into this inbox. The handle wires as
	 * `["callback", slot]`; the inbox wires as
	 * `["inbox", slot, offset]`.
	 */
	abstract get handle(): CallbackHandleStub<R>;

	/**
	 * Return a new inbox stub with the cursor advanced by `n`
	 * events. Pure: the receiver is unchanged. The new stub becomes
	 * effective when the caller writes it back into state.
	 */
	abstract sliced(n: number): CallbackInboxStub<R>;

	/**
	 * Short human-readable summary used by inspection / debug-UI
	 * tooling. Concrete subclasses override to add identity (slot /
	 * offset). See `EntityRefStub.debugSummary`.
	 */
	debugSummary(): string {
		return "<CallbackInboxStub>";
	}
}

/**
 * Send-side handle for a workstation callback. Wire form is
 * `["callback", slot]`. Allowed as an `allocateInvoke` arg
 * (positional or keyword, at any depth); not allowed in
 * `Session.userState` (that's the receive-side `CallbackInboxStub`'s
 * job).
 *
 * Mint via `inbox.handle` — handles are paired 1:1 with the inbox
 * they came from. The type parameter `R` is type-level only,
 * carrying the event payload shape so handle ↔ inbox stay
 * R-aligned across an invoke boundary.
 *
 * Opaque abstract base — no public identity getter. Wire encoding
 * goes through the `encodeCallbackHandle` opt callback in
 * `escape.ts`.
 */
export abstract class CallbackHandleStub<R = unknown> {
	declare readonly [CALLBACK_HANDLE_BRAND]: R;

	/**
	 * Short human-readable summary used by inspection / debug-UI
	 * tooling. Concrete subclasses override to add identity (slot).
	 * See `EntityRefStub.debugSummary`.
	 */
	debugSummary(): string {
		return "<CallbackHandleStub>";
	}
}
