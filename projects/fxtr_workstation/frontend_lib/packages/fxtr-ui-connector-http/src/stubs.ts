/**
 * Concrete HTTP-connector stub subclasses. Abstract bases live in
 * `@fxtr-ui/core`. Forked near-verbatim from the workstation connector's
 * `stubs.ts` (the slot/fill/inbox machinery is identical); the only
 * connector-specific differences are *how* the session-level atoms are
 * populated — locally, with no server protocol — which is the session
 * reducer's concern, not the stubs'.
 *
 * Entity / service refs are interned per **connector** (cross-session;
 * identity is a function of `shortId` alone). Service refs carry a
 * `state` atom over a connector-level broken-services set: a transient
 * service whose ws connection-generation died is marked broken there, so
 * the single interned instance reflects it everywhere (named http/ws
 * services never break). Op-result / inbox stubs are interned per
 * **session** (slot integers are session-scoped) and read through the
 * `HttpStubSession` interface to avoid a circular import on the session.
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

export class HttpEntityRefStub<T = unknown> extends EntityRefStub<T> {
	readonly #shortId: string;

	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}

	override get shortId(): string {
		return this.#shortId;
	}
}

export class HttpServiceRefStub<T = unknown> extends ServiceRefStub<T> {
	readonly #shortId: string;
	readonly #stateAtom: Atom<ServiceState>;

	/**
	 * @param shortId        Client-minted service short_id.
	 * @param brokenServices Connector-level set of short_ids known broken
	 *   (transient stubs whose ws connection-generation died). The stub's
	 *   `state` derives from it, so the single interned instance reflects
	 *   breakage across every session. Named services are never in it.
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
 * Local per-slot fill record (in the session's `slotFills` map).
 * Populated by the reducer from load / invoke fills. `seq` is a local
 * monotonic counter stamped at delivery (there is no server event
 * sequence). `value` is the decoded payload — always a `ResultValue`
 * (services can't produce client-only stubs).
 */
export type FillRecord =
	| { readonly kind: "filled"; readonly seq: number; readonly value: ResultValue }
	| { readonly kind: "errored"; readonly seq: number; readonly message: string };

/**
 * One entry in a slot's local event log. `seq` is the local delivery
 * counter; `payload` is the decoded value (always `ResultValue`).
 */
export interface InboxEntry {
	readonly seq: number;
	readonly payload: ResultValue;
}

/**
 * Per-slot in-memory event log. `events[i]` corresponds to absolute
 * event index `firstIndex + i`. The session reducer's
 * `reindexCommittedState` advances `firstIndex` + trims the front when
 * the committed min-offset advances, and drops the entry entirely when
 * no inbox stub for the slot remains in committed state.
 */
export interface InboxLog {
	readonly firstIndex: number;
	readonly events: readonly InboxEntry[];
}

/**
 * Per-slot connection state of a callback inbox's `subscribe_to` feed.
 * On the workstation this is reduced from the server's
 * `CallbackSubscriptionEvent` stream; here the connector **synthesizes**
 * it locally (design/05 N1) — `connected` on a successful ws subscribe,
 * `broken` (with a reason) when that ws generation dies. A slot absent
 * from the map is "idle".
 */
export interface InboxSubscriptionState {
	readonly state: "connected" | "broken";
	readonly reason: string | null;
}

/**
 * Session read-surface the slot-scoped stubs (`HttpOpResultStub`,
 * `HttpCallbackInboxStub`) derive from. The concrete `HttpSession`
 * implements it; the interface keeps the stubs free of a circular
 * import on the session class while still allowing
 * `stub.session === session` cross-session checks.
 */
export interface HttpStubSession {
	/** Session UUID; primarily for diagnostics. */
	readonly sessionId: string;
	/** Slot integer → fill record (load / invoke fills). Reactive. */
	readonly slotFillsAtom: Atom<ReadonlyMap<number, FillRecord>>;
	/** Slots referenced by `OpResultStub`s in committed user state. Reactive. */
	readonly committedSlotSetAtom: Atom<ReadonlySet<number>>;
	/**
	 * Slots flagged broken — a read of any such slot's `result` returns
	 * errored regardless of `slotFills`. Reactive.
	 */
	readonly brokenSlotsAtom: Atom<ReadonlySet<number>>;
	/** Slot integer → per-slot in-memory event log. Reactive. */
	readonly inboxLogsAtom: Atom<ReadonlyMap<number, InboxLog>>;
	/** Slots referenced by `CallbackInboxStub`s in committed user state. Reactive. */
	readonly committedInboxSlotSetAtom: Atom<ReadonlySet<number>>;
	/** Slot integer → synthesized `subscribe_to` connection state. Reactive. */
	readonly inboxSubscriptionStateAtom: Atom<ReadonlyMap<number, InboxSubscriptionState>>;
}

/**
 * Concrete HTTP-side `OpResultStub` — a slot reference with reactive
 * atoms for its current resolution state. Identity is `(slot, session)`;
 * interned per session. `slot=0` is the session's dead-sentinel
 * (cross-session / unrecoverable). Identical machinery to the
 * workstation stub; only the upstream fill source differs (local
 * delivery vs server events).
 */
export class HttpOpResultStub<T = unknown> extends OpResultStub<T> {
	readonly slot: number;
	readonly session: HttpStubSession;
	readonly #result: Atom<ResultStatus<T>>;
	readonly #committed: Atom<boolean>;
	readonly #done: Atom<boolean>;

	constructor(slot: number, session: HttpStubSession) {
		super();
		this.slot = slot;
		this.session = session;

		if (slot === 0) {
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
					message: `broken op-result slot ${slot} (no spec, out-of-range, or flagged broken)`,
				};
			}
			const fill = get(session.slotFillsAtom).get(slot);
			if (fill === undefined) return { status: "pending" };
			if (fill.kind === "errored") return { status: "errored", message: fill.message };
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

/**
 * Concrete HTTP-side `CallbackInboxStub` — a receive-side cursor
 * `(slot, offset)` over the slot's in-memory event log. Not interned
 * (each `sliced(n)` / decode mints fresh; equality is by `(slot,
 * offset)`). `slot=0` is the session's inbox dead-sentinel.
 */
export class HttpCallbackInboxStub<R = unknown> extends CallbackInboxStub<R> {
	readonly slot: number;
	readonly offset: number;
	readonly session: HttpStubSession;
	readonly #events: Atom<readonly R[]>;
	readonly #state: Atom<"opening" | "open" | "closed">;
	readonly #committed: Atom<boolean>;
	readonly #connectionState: Atom<"idle" | "connected" | "broken">;
	readonly #subscriptionBreakReason: Atom<string | null>;

	constructor(slot: number, offset: number, session: HttpStubSession) {
		super();
		this.slot = slot;
		this.offset = offset;
		this.session = session;

		if (slot === 0) {
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
					`HttpCallbackInboxStub(session=${session.sessionId}, slot=${slot}, offset=${this.offset}): cursor behind firstIndex=${log.firstIndex} — invariant violation.`,
				);
			}
			const out: R[] = [];
			for (let i = startIdx; i < log.events.length; i++) {
				const entry = log.events[i];
				if (entry === undefined) continue; // satisfies noUncheckedIndexedAccess
				out.push(entry.payload as R);
			}
			return out;
		});
		this.#state = atom<"opening" | "open" | "closed">((get) =>
			get(session.committedInboxSlotSetAtom).has(slot) ? "open" : "opening",
		);
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

	#handle: HttpCallbackHandleStub<R> | undefined;
	override get handle(): CallbackHandleStub<R> {
		if (this.#handle === undefined) {
			this.#handle = new HttpCallbackHandleStub<R>(this.slot, this.session);
		}
		return this.#handle;
	}

	override sliced(n: number): CallbackInboxStub<R> {
		if (!Number.isInteger(n) || n < 0) {
			throw new Error(`HttpCallbackInboxStub.sliced: n must be a non-negative integer (got ${n})`);
		}
		return new HttpCallbackInboxStub<R>(this.slot, this.offset + n, this.session);
	}

	override debugSummary(): string {
		return `<CallbackInboxStub slot=${this.slot} offset=${this.offset}>`;
	}
}

/**
 * Concrete HTTP-side `CallbackHandleStub` — the send-side of a callback
 * inbox. Slot-identified, with a session backref so the encoder can
 * detect cross-session smuggling at `allocateInvoke` time and throw.
 */
export class HttpCallbackHandleStub<R = unknown> extends CallbackHandleStub<R> {
	readonly slot: number;
	readonly session: HttpStubSession;

	constructor(slot: number, session: HttpStubSession) {
		super();
		this.slot = slot;
		this.session = session;
	}

	override debugSummary(): string {
		return `<CallbackHandleStub slot=${this.slot}>`;
	}
}
