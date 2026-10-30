/**
 * Concrete workstation-side stub subclasses. Abstract bases live in
 * `@fxtr-ui/core`; only the connector knows how to mint a concrete
 * one — it needs the workstation `short_id` (for entity / service)
 * or the slot integer + session backref (for op-result / inbox).
 *
 * Entity / service refs are interned per **connector** (cross-session
 * by design — identity is a function of `shortId` alone). Service refs
 * additionally carry a `state` atom over the connector-level
 * broken-services set: brokenness is a global workstation fact (a
 * service breaks for every session at once), so the single interned
 * instance reflects it everywhere. Op-result stubs are interned per
 * **session** because slot integers are session-scoped.
 *
 * `WorkstationOpResultStub`'s atoms (`result` / `committed` / `done`)
 * read from session-level atoms supplied via the
 * `WorkstationStubSession` interface so the stub avoids a
 * circular import on the `WorkstationSession` class.
 */

import {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	OpResultStub,
	type ResultStatus,
	type ResultValue,
	ServiceRefStub,
	type ServiceState,
} from "@fxtr-ui/core";
import { type Atom, atom } from "jotai";

export class WorkstationEntityRefStub<T = unknown> extends EntityRefStub<T> {
	readonly #shortId: string;

	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}

	override get shortId(): string {
		return this.#shortId;
	}
}

export class WorkstationServiceRefStub<T = unknown> extends ServiceRefStub<T> {
	readonly #shortId: string;
	readonly #stateAtom: Atom<ServiceState>;

	/**
	 * @param shortId           Workstation service short_id.
	 * @param brokenServices    Connector-level set of short_ids known to
	 *   be broken. The stub's `state` derives from it, so the single
	 *   interned instance reflects breakage across every session.
	 */
	constructor(shortId: string, brokenServices: Atom<ReadonlySet<string>>) {
		super();
		this.#shortId = shortId;
		this.#stateAtom = atom<ServiceState>((get) =>
			get(brokenServices).has(shortId) ? "broken" : "alive",
		);
	}

	override get shortId(): string {
		return this.#shortId;
	}

	override get state(): Atom<ServiceState> {
		return this.#stateAtom;
	}
}

/**
 * Local per-slot fill record. Lives in the session's `slotFills`
 * map; populated by the reducer from `LoadResultEvent` /
 * `LoadErrorEvent` / `InvokeResultEvent` / `InvokeErrorEvent`.
 * `seq` is the originating event's sequence — used for preserve-set
 * derivation. `value` is the wire-decoded payload, always a
 * `ResultValue` (Python services can't produce client-only stubs).
 */
export type FillRecord =
	| { readonly kind: "filled"; readonly seq: number; readonly value: ResultValue }
	| { readonly kind: "errored"; readonly seq: number; readonly message: string };

/**
 * One entry in a slot's local event log. `seq` is the originating
 * workstation event's sequence number — the preserve diff uses it
 * to derive `PreserveEventsOp` / `UnpreserveEventsOp` lists.
 * `payload` is the wire-decoded structured value (always
 * `ResultValue` — Python services can't produce client-only stubs).
 */
export interface InboxEntry {
	readonly seq: number;
	readonly payload: ResultValue;
}

/**
 * Per-slot in-memory event log. `events[i]` corresponds to the
 * absolute event index `firstIndex + i`. Maintained by the session
 * reducer's `reindexCommittedState` so that
 *
 *     firstIndex == min(stub.offset for inbox stubs in committed
 *                       user state for this slot)
 *
 * (see the "Slot bookkeeping invariants" section on
 * `WorkstationSession`). `handleCallbackEvent` appends to
 * `events`; `reindexCommittedState` advances `firstIndex` and trims
 * the front when the committed min-offset advances; the entry is
 * dropped entirely when no inbox stub for this slot remains in
 * committed.
 */
export interface InboxLog {
	readonly firstIndex: number;
	readonly events: readonly InboxEntry[];
}

/**
 * Per-slot connection state of a callback inbox's `subscribe_to`
 * feed, reduced from the server's `CallbackSubscriptionEvent` stream
 * (latest-wins). A slot absent from the map is "idle" (never
 * subscribed). `reason` is the workstation-reported break reason and
 * is non-null only while `state === "broken"`.
 */
export interface InboxSubscriptionState {
	readonly state: "connected" | "broken";
	readonly reason: string | null;
}

/**
 * Session surface that the workstation-side stub classes
 * (`WorkstationOpResultStub` and `WorkstationCallbackInboxStub`)
 * read through. The concrete `WorkstationSession` implements this;
 * the interface exists so the stub classes avoid a circular import
 * on the session class while still allowing
 * `stub.session === session` comparisons for cross-session
 * detection.
 *
 * Exposes only the public read-view atoms — stubs derive everything
 * reactively through these. (Earlier drafts exposed the jotai
 * `store` for imperative reads from `cleared()`; that method has
 * been removed in favor of explicit `inbox.sliced(events.length)`
 * patterns in user code, so the store doesn't need to be on this
 * interface.)
 */
export interface WorkstationStubSession {
	/** Session UUID; primarily useful for diagnostics. */
	readonly sessionId: string;
	/**
	 * Slot integer → fill record. Reactive; the session's reducer
	 * mutates this in response to load / invoke result + error
	 * events.
	 */
	readonly slotFillsAtom: Atom<ReadonlyMap<number, FillRecord>>;
	/**
	 * Slot integers referenced by `OpResultStub`s in the current
	 * committed user state. Reactive; rebuilt on every committed
	 * state change.
	 */
	readonly committedSlotSetAtom: Atom<ReadonlySet<number>>;
	/**
	 * Slot integers flagged broken — slots in this session's
	 * namespace whose op-result has been determined unrecoverable
	 * (e.g. a slot referenced in local user state without a matching
	 * `pendingSlotSpec` after a foreign mutator removed it, or a
	 * slot decoded from an incoming `["broken_op_result", slot]`
	 * tag). A read of the stub's `result` atom for any slot in this
	 * set returns errored regardless of `slotFills`. Reactive.
	 */
	readonly brokenSlotsAtom: Atom<ReadonlySet<number>>;
	/**
	 * Slot integer → per-slot in-memory event log. Reactive; the
	 * session's reducer mutates this in response to `CallbackEvent`s
	 * (append-only via `handleCallbackEvent`) and the committed-state
	 * walk (trim-on-commit via `reindexCommittedState`).
	 */
	readonly inboxLogsAtom: Atom<ReadonlyMap<number, InboxLog>>;
	/**
	 * Slot integers referenced by `CallbackInboxStub`s in the current
	 * committed user state. Reactive; rebuilt on every committed
	 * state change. The stub's `state` atom reads this to distinguish
	 * `opening` (in local pending only) from `open` (in committed).
	 */
	readonly committedInboxSlotSetAtom: Atom<ReadonlySet<number>>;
	/**
	 * Slot integer → `subscribe_to` connection state, reduced from the
	 * server's `CallbackSubscriptionEvent` stream (latest-wins). A slot
	 * absent from the map is "idle". Reactive; the stub's
	 * `connectionState` / `subscriptionBreakReason` atoms read this.
	 */
	readonly inboxSubscriptionStateAtom: Atom<ReadonlyMap<number, InboxSubscriptionState>>;
}

/**
 * Concrete workstation-side `OpResultStub` — a slot reference with
 * reactive atoms for its current resolution state.
 *
 * Identity is `(slot, session)`. Sessions intern these per slot
 * (see `WorkstationSession.lookupOpResult`), so two
 * `["op_result", 5]` decodes from the same session resolve to the
 * same instance and `===` equality holds for state-walk purposes.
 *
 * The `slot=0` sentinel value identifies the session's
 * *dead-sentinel* — a hard-errored stub used as a replacement in
 * local user state when the user writes a cross-session stub (a
 * stub from a different `WorkstationSession`). Each session mints
 * exactly one such instance at construction.
 *
 * Best-effort caveat on `T`: the type parameter expresses the
 * structural shape of the loaded value when filled, but at runtime
 * types are erased. After a rollback + foreign-commit-at-the-same-
 * slot (rare; only possible in multi-mutator sessions), a stub the
 * consumer captured outside our state tree could end up resolving
 * to data not matching their TS type. Consumers should validate
 * via dispatch on `$fxtr.type` when type safety matters.
 */
export class WorkstationOpResultStub<T = unknown> extends OpResultStub<T> {
	readonly slot: number;
	readonly session: WorkstationStubSession;
	readonly #result: Atom<ResultStatus<T>>;
	readonly #committed: Atom<boolean>;
	readonly #done: Atom<boolean>;

	constructor(slot: number, session: WorkstationStubSession) {
		super();
		this.slot = slot;
		this.session = session;

		if (slot === 0) {
			// Dead-sentinel: a permanent "this slot is dead" stub
			// returned by the session whenever a wire decode hits
			// `["broken_op_result", 0]`, or when the flush walk
			// substitutes a cross-session stub. Atoms are static so
			// they don't depend on the (mutable) session-level state.
			this.#result = atom<ResultStatus<T>>({
				status: "errored",
				message: "dead op-result stub (cross-session or unrecoverable)",
			});
			this.#committed = atom(false);
			this.#done = atom(true);
			return;
		}

		this.#result = atom<ResultStatus<T>>((get) => {
			if (get(session.brokenSlotsAtom).has(slot)) {
				return {
					status: "errored",
					message: `broken op-result slot ${slot} (no spec, or out-of-range, or flagged broken on decode)`,
				};
			}
			const fill = get(session.slotFillsAtom).get(slot);
			if (fill === undefined) return { status: "pending" };
			if (fill.kind === "errored") {
				return { status: "errored", message: fill.message };
			}
			return { status: "filled", value: fill.value as T };
		});
		this.#committed = atom((get) => get(session.committedSlotSetAtom).has(slot));
		this.#done = atom((get) => get(this.#result).status !== "pending");
	}

	override get result(): Atom<ResultStatus<T>> {
		return this.#result;
	}
	override get committed(): Atom<boolean> {
		return this.#committed;
	}
	override get done(): Atom<boolean> {
		return this.#done;
	}

	override debugSummary(): string {
		return `<OpResultStub slot=${this.slot}>`;
	}
}

// --- Callback inbox / handle ----------------------------------------

/**
 * Concrete workstation-side `CallbackInboxStub` — a receive-side
 * cursor `(slot, offset)` over the slot's in-memory event log.
 *
 * Identity is `(slot, offset, session)`. Not interned: each
 * `sliced(n)` mints a fresh instance, and decoding
 * `["inbox", slot, offset]` from the wire also mints fresh.
 * Referential equality isn't a useful invariant for cursor values;
 * `leafEqual` in `state.ts` compares by `(slot, offset)`.
 *
 * The `slot=0` value identifies the session's *inbox dead-sentinel*
 * — used as a replacement when the flush walk encounters a
 * cross-session inbox stub. Atoms are hardwired to a permanently-
 * closed state, mirroring `WorkstationOpResultStub`'s slot-0
 * sentinel.
 *
 * The type parameter `R` is best-effort: the structural shape of
 * each event payload when filled. Runtime payloads are wire-decoded
 * structurally as `ResultValue`; consumers needing a strong
 * guarantee should validate.
 */
export class WorkstationCallbackInboxStub<R = unknown> extends CallbackInboxStub<R> {
	readonly slot: number;
	readonly offset: number;
	readonly session: WorkstationStubSession;
	readonly #events: Atom<readonly R[]>;
	readonly #state: Atom<"opening" | "open" | "closed">;
	readonly #committed: Atom<boolean>;
	readonly #connectionState: Atom<"idle" | "connected" | "broken">;
	readonly #subscriptionBreakReason: Atom<string | null>;

	constructor(slot: number, offset: number, session: WorkstationStubSession) {
		super();
		this.slot = slot;
		this.offset = offset;
		this.session = session;

		if (slot === 0) {
			// Dead-sentinel: a permanent "this inbox is closed" stub
			// returned in place of a cross-session-smuggled stub the
			// flush walk substitutes. Atoms are static so they don't
			// depend on the (mutable) session-level state.
			this.#events = atom<readonly R[]>([]);
			this.#state = atom<"opening" | "open" | "closed">("closed");
			this.#committed = atom(false);
			this.#connectionState = atom<"idle" | "connected" | "broken">("idle");
			this.#subscriptionBreakReason = atom<string | null>(null);
			return;
		}

		this.#events = atom<readonly R[]>((get) => {
			const log = get(session.inboxLogsAtom).get(slot);
			if (log === undefined) return [];
			const startIdx = this.offset - log.firstIndex;
			if (startIdx < 0) {
				throw new Error(
					`WorkstationCallbackInboxStub(session=${session.sessionId}, slot=${slot}, offset=${this.offset}): cursor behind firstIndex=${log.firstIndex} — invariant violation. Expected firstIndex <= every live inbox stub's offset for this slot.`,
				);
			}
			const out: R[] = [];
			for (let i = startIdx; i < log.events.length; i++) {
				const entry = log.events[i];
				if (entry === undefined) continue; // unreachable; satisfies noUncheckedIndexedAccess
				out.push(entry.payload as R);
			}
			return out;
		});
		this.#state = atom<"opening" | "open" | "closed">((get) => {
			if (get(session.committedInboxSlotSetAtom).has(slot)) return "open";
			return "opening";
		});
		this.#committed = atom((get) => get(session.committedInboxSlotSetAtom).has(slot));
		this.#connectionState = atom<"idle" | "connected" | "broken">((get) => {
			const entry = get(session.inboxSubscriptionStateAtom).get(slot);
			return entry === undefined ? "idle" : entry.state;
		});
		this.#subscriptionBreakReason = atom<string | null>((get) => {
			const entry = get(session.inboxSubscriptionStateAtom).get(slot);
			return entry !== undefined && entry.state === "broken" ? entry.reason : null;
		});
	}

	override get events(): Atom<readonly R[]> {
		return this.#events;
	}
	override get state(): Atom<"opening" | "open" | "closed"> {
		return this.#state;
	}
	override get committed(): Atom<boolean> {
		return this.#committed;
	}
	override get connectionState(): Atom<"idle" | "connected" | "broken"> {
		return this.#connectionState;
	}
	override get subscriptionBreakReason(): Atom<string | null> {
		return this.#subscriptionBreakReason;
	}

	/**
	 * Matching send-side handle. Minted lazily; subsequent calls
	 * return the same instance for this stub (referential equality
	 * is convenient for downstream comparisons). The handle carries
	 * the same session backref for cross-session checking at
	 * encode-time.
	 */
	#handle: WorkstationCallbackHandleStub<R> | undefined;
	override get handle(): CallbackHandleStub<R> {
		if (this.#handle === undefined) {
			this.#handle = new WorkstationCallbackHandleStub<R>(this.slot, this.session);
		}
		return this.#handle;
	}

	override sliced(n: number): CallbackInboxStub<R> {
		if (!Number.isInteger(n) || n < 0) {
			throw new Error(
				`WorkstationCallbackInboxStub.sliced: n must be a non-negative integer (got ${n})`,
			);
		}
		return new WorkstationCallbackInboxStub<R>(this.slot, this.offset + n, this.session);
	}

	override debugSummary(): string {
		return `<CallbackInboxStub slot=${this.slot} offset=${this.offset}>`;
	}
}

/**
 * Concrete workstation-side `CallbackHandleStub` — the send-side of
 * a callback inbox. Slot-identified; carries a session backref so
 * the encoder can detect cross-session smuggling at
 * `allocateInvoke` time and throw.
 */
export class WorkstationCallbackHandleStub<R = unknown> extends CallbackHandleStub<R> {
	readonly slot: number;
	readonly session: WorkstationStubSession;

	constructor(slot: number, session: WorkstationStubSession) {
		super();
		this.slot = slot;
		this.session = session;
	}

	override debugSummary(): string {
		return `<CallbackHandleStub slot=${this.slot}>`;
	}
}
