/**
 * `HttpSession` — the client-local session reducer (design/05). A clean
 * reimplementation of the abstract `Session` over purely local state:
 * it emulates what a workstation session would do with the browser as
 * the workstation. There is **no wire mutation protocol** — writes
 * commit locally and immediately; the only failure mode of a write is a
 * synchronous throw at allocate/encode time. Remote breakage (a failed
 * fetch, a dropped ws) surfaces as ordinary *errored fills*, exactly
 * like a workstation `invoke_error`.
 *
 * The slot machinery is kept conceptually from the workstation session
 * (slot counter, op-result / inbox interning, slot-0 dead sentinels,
 * cross-session substitution at flush, load/metadata dedup, the
 * microtask-coalesced flush) — minus everything that existed only
 * because a server owned the state (JSON patches, acks, inflight
 * envelope, snapshot/seq reconciliation, preserve/unpreserve,
 * track/untrack).
 *
 * Persistence / rehydration (design/05): the session serializes its
 * committed state to a `SerializedSession` blob (shortId-mode escape)
 * — debounced after each commit / fill / notify — which the connector
 * writes through its `Storage` handle. `attachSession` rehydrates from
 * that blob; entity load + metadata payloads aren't persisted (they
 * refire), in-flight invokes rehydrate errored ("interrupted"), and
 * transient stubs / live subscriptions rehydrate broken.
 */

import type {
	EntityMetadata,
	EntityRefStub,
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	InvokeArgValue,
	JsonValue,
	ResultUnescapeOptions,
	ResultValue,
	ServiceMetadata,
	Session,
	SessionLifecycleState,
	StateUnescapeOptions,
	StateValue,
	UserState,
	UserStateWriteOptions,
	WireValue,
} from "@fxtr-ui/core";
import {
	CallbackInboxStub,
	type EscapeOptions,
	OpResultStub,
	ServiceRefStub,
	TAG_BROKEN_INBOX,
	TAG_BROKEN_OP_RESULT,
	TAG_CALLBACK,
	TAG_INBOX,
	TAG_OP_RESULT,
	WireEscapeError,
	escapeInvokeArgs,
	escapeWire,
	unescapeWire,
} from "@fxtr-ui/core";
import { type Atom, type PrimitiveAtom, atom, type getDefaultStore } from "jotai";

import { type ServiceDescriptor, descriptorFromWire, descriptorToWire } from "./descriptor";
import {
	SESSION_BLOB_VERSION,
	type SerializedFill,
	type SerializedInbox,
	type SerializedSession,
} from "./persistence";
import {
	type FillRecord,
	HttpCallbackHandleStub,
	HttpCallbackInboxStub,
	HttpOpResultStub,
	type HttpStubSession,
	type InboxEntry,
	type InboxLog,
	type InboxSubscriptionState,
} from "./stubs";
import type { WsPool } from "./ws_pool";

type JotaiStore = ReturnType<typeof getDefaultStore>;

/** The (url, generation) a transient service / live subscription is bound to. */
export interface TransientContext {
	readonly url: string;
	readonly generation: number;
}

/**
 * The slice of `HttpConnector` the session depends on. Declared here (an
 * interface the connector satisfies structurally) so the session has no
 * value-import cycle on the connector.
 */
export interface SessionConnector {
	readonly store: JotaiStore;
	readonly url: string;
	readonly wsPool: WsPool;
	entityRef(shortId: string): EntityRefStub;
	serviceRef(shortId: string): ServiceRefStub;
	internEntity(dataIdString: string, provIdString?: string | null): EntityRefStub;
	internEntityByIdString(idString: string): EntityRefStub;
	internServiceByDescriptor(descriptor: ServiceDescriptor, ctx?: TransientContext): ServiceRefStub;
	entityIdStringFor(shortId: string): string;
	serviceDescriptorFor(shortId: string): ServiceDescriptor;
	loadEntityData<T = unknown>(stub: EntityRefStub<T>): Promise<T>;
	getEntityMetadata(stub: EntityRefStub): Promise<EntityMetadata>;
	getServiceMetadata(stub: ServiceRefStub): ServiceMetadata;
	invokeHttpService(
		descriptor: ServiceDescriptor,
		method: string,
		args: readonly WireValue[],
		kwargs: { readonly [key: string]: WireValue },
	): Promise<WireValue>;
	registerInbox(uuid: string, deliver: (payload: WireValue) => void): void;
	unregisterInbox(uuid: string): void;
	onConnectionDrop(listener: (url: string, generation: number) => void): () => void;
	transientBindingFor(shortId: string): TransientContext | undefined;
	markServiceBroken(shortId: string): void;
	/** Release the consumer's handle — drops the live instance, keeps storage. */
	removeSession(sessionId: string): void;
	/** Write a session's persisted blob (debounced by the session). */
	persistSession(sessionId: string, blob: SerializedSession): void;
	/** Delete a session's persisted blob + registry entry (terminate). */
	deleteSession(sessionId: string): void;
}

type DedupKind = "load" | "get_entity_metadata" | "get_service_metadata";

/** A pending op recorded at allocate time, fired (once) at the next flush. */
type OpSpec =
	| { readonly kind: "load"; readonly shortId: string }
	| { readonly kind: "get_entity_metadata"; readonly shortId: string }
	| { readonly kind: "get_service_metadata"; readonly shortId: string }
	| {
			readonly kind: "invoke";
			readonly serviceShortId: string;
			readonly method: string;
			readonly args: readonly WireValue[];
			readonly kwargs: { readonly [key: string]: WireValue };
	  }
	| {
			readonly kind: "handleInvoke"; // subscribe_to-style invoke on an inbox handle
			readonly callbackSlot: number;
			readonly method: string;
			readonly args: readonly InvokeArgValue[];
	  }
	| { readonly kind: "callback" };

type DedupSpec = Extract<OpSpec, { kind: DedupKind }>;
function isDedupSpec(spec: OpSpec): spec is DedupSpec {
	return (
		spec.kind === "load" ||
		spec.kind === "get_entity_metadata" ||
		spec.kind === "get_service_metadata"
	);
}

/** Per-inbox local bookkeeping (uuid + live subscription / unsubscribe handle). */
interface InboxState {
	readonly uuid: string;
	/** Whether the notify route is live (registered at first commit, not at allocate). */
	registered: boolean;
	subscription: TransientContext | undefined;
	unsubscribe: ServiceRefStub | undefined;
}

interface Envelope {
	readonly user: UserState | null;
}
const EMPTY_ENVELOPE: Envelope = { user: null };

/** Resolve a (possibly relative) ws service URL to an absolute ws/wss URL. */
function toWsUrl(base: string, path: string): string {
	if (path.startsWith("ws://") || path.startsWith("wss://")) return path;
	const u = new URL(path, base);
	u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
	return u.href;
}

/** Collect the op-result slots + inbox (slot → min offset) in a state tree. */
function collectStubs(value: StateValue | null): {
	opResultSlots: Set<number>;
	inboxSlotOffsets: Map<number, number>;
} {
	const opResultSlots = new Set<number>();
	const inboxSlotOffsets = new Map<number, number>();
	const walk = (v: StateValue): void => {
		if (v === null || typeof v !== "object" || v instanceof Uint8Array) return;
		if (v instanceof HttpOpResultStub) {
			if (v.slot !== 0) opResultSlots.add(v.slot);
			return;
		}
		if (v instanceof HttpCallbackInboxStub) {
			if (v.slot !== 0) {
				const prev = inboxSlotOffsets.get(v.slot);
				inboxSlotOffsets.set(v.slot, prev === undefined ? v.offset : Math.min(prev, v.offset));
			}
			return;
		}
		if (v instanceof OpResultStub || v instanceof CallbackInboxStub) return; // foreign-subclass
		if (Array.isArray(v)) {
			for (const item of v) walk(item);
			return;
		}
		for (const key of Object.keys(v)) walk((v as { [k: string]: StateValue })[key] as StateValue);
	};
	if (value !== null) walk(value);
	return { opResultSlots, inboxSlotOffsets };
}

export class HttpSession implements Session, HttpStubSession {
	readonly sessionId: string;
	readonly store: JotaiStore;
	readonly lifecycle: Atom<SessionLifecycleState>;
	readonly userState: FxtrSessionAtom<UserState | null>;

	private readonly connector: SessionConnector;
	private readonly lifecycleAtom: PrimitiveAtom<SessionLifecycleState>;
	private readonly committedAtom: PrimitiveAtom<Envelope>;
	private readonly localAtom: PrimitiveAtom<Envelope>;

	// HttpStubSession surface (the stubs read these reactively).
	private readonly slotFillsPrivate: PrimitiveAtom<ReadonlyMap<number, FillRecord>>;
	private readonly committedSlotSetPrivate: PrimitiveAtom<ReadonlySet<number>>;
	private readonly brokenSlotsPrivate: PrimitiveAtom<ReadonlySet<number>>;
	private readonly inboxLogsPrivate: PrimitiveAtom<ReadonlyMap<number, InboxLog>>;
	private readonly committedInboxSlotSetPrivate: PrimitiveAtom<ReadonlySet<number>>;
	private readonly inboxSubStatePrivate: PrimitiveAtom<ReadonlyMap<number, InboxSubscriptionState>>;

	private readonly updateCtx: FxtrSessionUpdateCtx;
	private metadata: JsonValue;
	private createdAt: number;
	private localSlotCounter = 0;
	private seqCounter = 0;
	private dirty = false;
	private detaching = false;
	private pendingFlush: Promise<void> | null = null;
	private persistScheduled = false;

	/** Slots allocated this tick + their op specs (consumed at flush). */
	private readonly pendingSlotSpecs = new Map<number, OpSpec>();
	/** Same-tick dedup: kind → shortId → slot (cleared each flush). */
	private pendingDedup: Record<DedupKind, Map<string, number>> = freshDedup();
	/** Committed dedup: kind → shortId → slot (rebuilt each flush). */
	private committedDedup: Record<DedupKind, Map<string, number>> = freshDedup();
	/**
	 * Long-lived reverse index `slot → {kind, shortId}` for dedup slots —
	 * the analogue of the workstation's `committedSlotDedupInfo`. Lets the
	 * flush walk repopulate `committedDedup` for slots that are still in
	 * committed state but weren't (re)allocated this tick, so dedup
	 * survives across flushes (behavioral audit I1). GC'd in
	 * `reindexCommittedState` when a slot leaves committed state.
	 */
	private readonly dedupSlotInfo = new Map<number, { kind: DedupKind; shortId: string }>();

	private readonly opResultStubs = new Map<number, HttpOpResultStub>();
	private readonly deadSentinel: HttpOpResultStub;
	private readonly deadInboxSentinel: HttpCallbackInboxStub;
	private readonly inboxState = new Map<number, InboxState>();
	private readonly unsubscribeDropListener: () => void;

	constructor(opts: {
		sessionId: string;
		connector: SessionConnector;
		metadata?: JsonValue;
		createdAt?: number;
		/** A persisted blob to rehydrate from (design/05); omit for a fresh session. */
		restore?: SerializedSession;
		/**
		 * Construct the session directly in the terminal `unavailable`
		 * state — `attachSession` to an id with no persisted blob. Mirrors
		 * the workstation connector, where attaching to a session the
		 * server doesn't know lands `unavailable` (not a silent new
		 * session). Mutually exclusive with `restore`.
		 */
		unavailable?: boolean;
	}) {
		this.sessionId = opts.sessionId;
		this.connector = opts.connector;
		this.store = opts.connector.store;
		this.metadata = opts.metadata ?? {};
		this.createdAt = opts.createdAt ?? Date.now();

		this.lifecycleAtom = atom<SessionLifecycleState>("connecting");
		this.committedAtom = atom<Envelope>(EMPTY_ENVELOPE);
		this.localAtom = atom<Envelope>(EMPTY_ENVELOPE);
		this.slotFillsPrivate = atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>());
		this.committedSlotSetPrivate = atom<ReadonlySet<number>>(new Set<number>());
		this.brokenSlotsPrivate = atom<ReadonlySet<number>>(new Set<number>());
		this.inboxLogsPrivate = atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>());
		this.committedInboxSlotSetPrivate = atom<ReadonlySet<number>>(new Set<number>());
		this.inboxSubStatePrivate = atom<ReadonlyMap<number, InboxSubscriptionState>>(
			new Map<number, InboxSubscriptionState>(),
		);

		this.lifecycle = this.lifecycleAtom;
		this.userState = atom(
			(get) => get(this.localAtom).user,
			(_get, _set, value: FxtrUpdateLike, options?: UserStateWriteOptions) =>
				this.handleWrite(value, options),
		) as FxtrSessionAtom<UserState | null>;

		this.updateCtx = {
			allocateLoad: <T>(ref: EntityRefStub<T>) => this.allocateLoad<T>(ref),
			allocateInvoke: ((
				target: ServiceRefStub<unknown> | HttpCallbackHandleStub<unknown>,
				method: string,
				args: readonly InvokeArgValue[],
				kwargs?: { readonly [key: string]: InvokeArgValue },
			) =>
				this.allocateInvoke(
					target,
					method,
					args,
					kwargs,
				)) as FxtrSessionUpdateCtx["allocateInvoke"],
			allocateGetEntityMetadata: (ref) => this.allocateGetEntityMetadata(ref),
			allocateGetServiceMetadata: (svc) => this.allocateGetServiceMetadata(svc),
			allocateCallback: <R>() => this.allocateCallback<R>(),
			session: this,
		};

		this.deadSentinel = new HttpOpResultStub(0, this);
		this.opResultStubs.set(0, this.deadSentinel);
		this.deadInboxSentinel = new HttpCallbackInboxStub(0, 0, this);

		// A live session registers a connector drop listener (to break its
		// transient stubs / inboxes when a ws generation dies). A terminal
		// `unavailable` session has no live bookkeeping, so it registers
		// none (a no-op unsubscribe). `makeDropListener` registers + returns
		// the unsubscribe so the `readonly` field is assigned inline below.
		const makeDropListener = () =>
			this.connector.onConnectionDrop((url, generation) => this.onConnectionDrop(url, generation));
		if (opts.unavailable) {
			// Attach-to-unknown: terminal, unusable. No storage interaction
			// (the connector neither registers nor persists it).
			this.unsubscribeDropListener = () => {};
			this.store.set(this.lifecycleAtom, "unavailable");
		} else if (opts.restore !== undefined) {
			// Attaching to a persisted session: only become live if we can
			// fully restore it. An unrestorable blob lands `unavailable` —
			// we never fabricate a blank session under an id we couldn't
			// restore (the connector then declines to intern it).
			if (this.restoreFrom(opts.restore)) {
				this.unsubscribeDropListener = makeDropListener();
				this.store.set(this.lifecycleAtom, "connected");
			} else {
				this.unsubscribeDropListener = () => {};
				this.store.set(this.lifecycleAtom, "unavailable");
			}
		} else {
			// Fresh session: no server round-trip, so it's connected at once.
			this.unsubscribeDropListener = makeDropListener();
			this.store.set(this.lifecycleAtom, "connected");
		}
	}

	// -- HttpStubSession surface -------------------------------------
	get slotFillsAtom(): Atom<ReadonlyMap<number, FillRecord>> {
		return this.slotFillsPrivate;
	}
	get committedSlotSetAtom(): Atom<ReadonlySet<number>> {
		return this.committedSlotSetPrivate;
	}
	get brokenSlotsAtom(): Atom<ReadonlySet<number>> {
		return this.brokenSlotsPrivate;
	}
	get inboxLogsAtom(): Atom<ReadonlyMap<number, InboxLog>> {
		return this.inboxLogsPrivate;
	}
	get committedInboxSlotSetAtom(): Atom<ReadonlySet<number>> {
		return this.committedInboxSlotSetPrivate;
	}
	get inboxSubscriptionStateAtom(): Atom<ReadonlyMap<number, InboxSubscriptionState>> {
		return this.inboxSubStatePrivate;
	}

	// -- lifecycle ----------------------------------------------------

	async detach(): Promise<void> {
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (this.detaching || lifecycle === "closed" || lifecycle === "unavailable") return;
		this.detaching = true;
		if (this.pendingFlush) await this.pendingFlush;
		// Best-effort unsubscribe of any live subscriptions, then drop
		// inbox routes.
		for (const [, st] of this.inboxState) {
			this.connector.unregisterInbox(st.uuid);
			if (st.unsubscribe) void this.bestEffortUnsubscribe(st.unsubscribe);
		}
		this.unsubscribeDropListener();
		// Persist the final state before releasing — detach keeps storage so
		// a later `attachSession` rehydrates exactly where we left off.
		this.persistNow();
		this.connector.removeSession(this.sessionId);
		this.store.set(this.lifecycleAtom, "closed");
	}

	/**
	 * Destroy the session (design/05). The counterpart to `detach()`'s
	 * "release the handle but keep the session": `terminate()` tears the
	 * session's local state down for good. HTTP has no remote to refuse
	 * the close, so this never rejects.
	 *
	 * An optional `finalState` is written through the normal flush first
	 * (so any jotai subscriber observes the parting state before
	 * lifecycle goes `closed`); `options.onRollback` is accepted for
	 * signature parity but never fires (HTTP commits locally).
	 *
	 * Unlike `detach`, `terminate` **deletes** the session's persisted
	 * storage (blob + registry entry) — there's nothing to re-attach to.
	 */
	async terminate(finalState?: UserState, _options?: UserStateWriteOptions): Promise<void> {
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (this.detaching || lifecycle === "closed" || lifecycle === "unavailable") return;
		// Commit the parting state through the normal write path (so the
		// flush walk + subscribers see it) *before* setting `detaching`,
		// which would otherwise reject the write.
		if (finalState !== undefined) this.store.set(this.userState, finalState);
		this.detaching = true;
		if (this.pendingFlush) await this.pendingFlush;
		for (const [, st] of this.inboxState) {
			this.connector.unregisterInbox(st.uuid);
			if (st.unsubscribe) void this.bestEffortUnsubscribe(st.unsubscribe);
		}
		this.unsubscribeDropListener();
		// Closed first (so any stray scheduled persist bails), then delete.
		this.store.set(this.lifecycleAtom, "closed");
		this.connector.deleteSession(this.sessionId);
	}

	private assertWritable(operation: string): void {
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (lifecycle === "closed" || lifecycle === "unavailable") {
			throw new Error(`Cannot ${operation} on a ${lifecycle} session`);
		}
		if (this.detaching) throw new Error(`Cannot ${operation} on a detaching session`);
	}

	// -- write path + flush ------------------------------------------

	private handleWrite(value: FxtrUpdateLike, _options: UserStateWriteOptions | undefined): void {
		this.assertWritable("write");
		const prev = this.store.get(this.localAtom).user;
		const next = typeof value === "function" ? value(prev, this.updateCtx) : value;
		this.store.set(this.localAtom, { user: next });
		this.dirty = true;
		this.scheduleFlush();
	}

	private scheduleFlush(): void {
		if (this.pendingFlush !== null) return;
		let resolve!: () => void;
		this.pendingFlush = new Promise<void>((r) => {
			resolve = r;
		});
		queueMicrotask(() => {
			this.pendingFlush = null;
			try {
				this.flush();
			} finally {
				resolve();
			}
		});
	}

	private flush(): void {
		if (!this.dirty) return;
		this.dirty = false;
		const local = this.store.get(this.localAtom);

		// Flush walk: cross-session substitution + slot classification +
		// dedup forward reconstruction.
		const committedSlots = this.store.get(this.committedSlotSetPrivate);
		const forwards = freshDedup();
		const newBroken = new Set<number>();
		let substituted = false;

		const walk = (v: StateValue): StateValue => {
			if (v === null || typeof v !== "object" || v instanceof Uint8Array) return v;
			if (v instanceof HttpOpResultStub) {
				if (v.session !== this) {
					substituted = true;
					return this.deadSentinel;
				}
				if (v.slot === 0) return v;
				const spec = this.pendingSlotSpecs.get(v.slot);
				if (spec !== undefined) {
					if (isDedupSpec(spec)) forwards[spec.kind].set(spec.shortId, v.slot);
					return v;
				}
				// Already-committed dedup slot (not re-allocated this tick):
				// carry its forward entry so cross-tick dedup persists.
				const info = this.dedupSlotInfo.get(v.slot);
				if (info !== undefined && committedSlots.has(v.slot)) {
					forwards[info.kind].set(info.shortId, v.slot);
					return v;
				}
				if (!committedSlots.has(v.slot)) newBroken.add(v.slot); // orphaned (manual / removed)
				return v;
			}
			if (v instanceof HttpCallbackInboxStub) {
				if (v.session !== this) {
					substituted = true;
					return this.deadInboxSentinel;
				}
				return v;
			}
			if (v instanceof OpResultStub) {
				substituted = true;
				return this.deadSentinel;
			}
			if (v instanceof CallbackInboxStub) {
				substituted = true;
				return this.deadInboxSentinel;
			}
			if (Array.isArray(v)) {
				let changed = false;
				const out: StateValue[] = [];
				for (const item of v) {
					const w = walk(item);
					if (w !== item) changed = true;
					out.push(w);
				}
				return changed ? out : v;
			}
			let changed = false;
			const out: Record<string, StateValue> = {};
			for (const key of Object.keys(v)) {
				const cur = (v as { [k: string]: StateValue })[key] as StateValue;
				const w = walk(cur);
				if (w !== cur) changed = true;
				out[key] = w;
			}
			return changed ? out : v;
		};

		const walkedUser = local.user !== null ? (walk(local.user) as UserState) : null;
		const committedEnv: Envelope = substituted ? { user: walkedUser } : local;
		if (substituted) this.store.set(this.localAtom, committedEnv);

		// Carry forward committed dedup entries still referenced; merge new.
		this.committedDedup = forwards;

		if (newBroken.size > 0) {
			const merged = new Set(this.store.get(this.brokenSlotsPrivate));
			for (const s of newBroken) merged.add(s);
			this.store.set(this.brokenSlotsPrivate, merged);
		}

		// Commit: local becomes committed (no inflight envelope on HTTP).
		this.store.set(this.committedAtom, committedEnv);

		// Fire newly-allocated ops whose slot survived into committed.
		const stubs = collectStubs(committedEnv.user);
		// Register inbox notify routes for newly-committed callback slots
		// *before* firing ops below: a `subscribe_to` op hands the inbox
		// handle to its source, which may notify immediately, so the route
		// must already be live. Registering here (rather than at allocate)
		// upholds the workstation's committed-first invariant — an inbox
		// can't receive before its slot commits — and means an
		// allocated-but-never-stored callback never opens a route at all.
		for (const slot of stubs.inboxSlotOffsets.keys()) {
			const st = this.inboxState.get(slot);
			if (st !== undefined && !st.registered) {
				st.registered = true;
				this.connector.registerInbox(st.uuid, (payload) => this.handleNotify(slot, payload));
			}
		}
		for (const slot of stubs.opResultSlots) {
			const spec = this.pendingSlotSpecs.get(slot);
			if (spec !== undefined) void this.fireOp(slot, spec);
		}
		this.pendingSlotSpecs.clear();
		this.pendingDedup = freshDedup();

		this.reindexCommittedState();
		this.schedulePersist();
	}

	// -- allocate surface --------------------------------------------

	private allocateLoad<T>(ref: EntityRefStub<T>): HttpOpResultStub<T> {
		return this.allocateDeduped<T>("load", ref.shortId);
	}
	private allocateGetEntityMetadata(ref: EntityRefStub): HttpOpResultStub<EntityMetadata> {
		return this.allocateDeduped<EntityMetadata>("get_entity_metadata", ref.shortId);
	}
	private allocateGetServiceMetadata(
		svc: ServiceRefStub<unknown>,
	): HttpOpResultStub<ServiceMetadata> {
		return this.allocateDeduped<ServiceMetadata>("get_service_metadata", svc.shortId);
	}

	private allocateDeduped<T>(kind: DedupKind, shortId: string): HttpOpResultStub<T> {
		this.assertWritable("allocate");
		const pending = this.pendingDedup[kind].get(shortId);
		if (pending !== undefined) return this.opResult<T>(pending);
		const committed = this.committedDedup[kind].get(shortId);
		if (committed !== undefined) return this.opResult<T>(committed);
		const slot = ++this.localSlotCounter;
		this.pendingSlotSpecs.set(slot, { kind, shortId });
		this.pendingDedup[kind].set(shortId, slot);
		this.dedupSlotInfo.set(slot, { kind, shortId });
		return this.opResult<T>(slot);
	}

	private allocateInvoke<R>(
		target: ServiceRefStub<unknown> | HttpCallbackHandleStub<unknown>,
		method: string,
		args: readonly InvokeArgValue[],
		kwargs?: { readonly [key: string]: InvokeArgValue },
	): HttpOpResultStub<R> {
		this.assertWritable("allocateInvoke");
		const slot = ++this.localSlotCounter;
		if (target instanceof HttpCallbackHandleStub) {
			if (target.session !== this) {
				throw new Error("allocateInvoke: callback handle belongs to a different session");
			}
			// subscribe_to-style invoke *on the inbox handle*; the source +
			// (optional) method are the args. Reinterpreted at fire time.
			this.pendingSlotSpecs.set(slot, {
				kind: "handleInvoke",
				callbackSlot: target.slot,
				method,
				args,
			});
			return this.opResult<R>(slot);
		}
		// Service-target: encode args to the wire now (fail-fast on a bad
		// arg / cross-session handle, per design/01 Q-L's allocate-time check).
		const { args: wireArgs, kwargs: wireKwargs } = escapeInvokeArgs(
			args,
			kwargs,
			this.encodeOpts(),
		);
		this.pendingSlotSpecs.set(slot, {
			kind: "invoke",
			serviceShortId: target.shortId,
			method,
			args: wireArgs,
			kwargs: wireKwargs,
		});
		return this.opResult<R>(slot);
	}

	private allocateCallback<R>(): HttpCallbackInboxStub<R> {
		this.assertWritable("allocateCallback");
		const slot = ++this.localSlotCounter;
		const uuid = crypto.randomUUID();
		this.inboxState.set(slot, {
			uuid,
			registered: false,
			subscription: undefined,
			unsubscribe: undefined,
		});
		this.pendingSlotSpecs.set(slot, { kind: "callback" });
		// The notify route is registered at commit (in `flush`), not here:
		// an inbox can't receive before its slot commits, and a notify can't
		// arrive before the `subscribe_to` op fires post-commit anyway.
		return new HttpCallbackInboxStub<R>(slot, 0, this);
	}

	private opResult<T>(slot: number): HttpOpResultStub<T> {
		const existing = this.opResultStubs.get(slot);
		if (existing) return existing as unknown as HttpOpResultStub<T>;
		const stub = new HttpOpResultStub<T>(slot, this);
		this.opResultStubs.set(slot, stub as unknown as HttpOpResultStub);
		return stub;
	}

	// -- op firing ----------------------------------------------------

	private async fireOp(slot: number, spec: OpSpec): Promise<void> {
		try {
			if (spec.kind === "load") {
				const value = await this.connector.loadEntityData(this.connector.entityRef(spec.shortId));
				this.fill(slot, { value: value as ResultValue });
			} else if (spec.kind === "get_entity_metadata") {
				const meta = await this.connector.getEntityMetadata(this.connector.entityRef(spec.shortId));
				this.fill(slot, { value: meta as unknown as ResultValue });
			} else if (spec.kind === "get_service_metadata") {
				const meta = this.connector.getServiceMetadata(this.connector.serviceRef(spec.shortId));
				this.fill(slot, { value: meta as unknown as ResultValue });
			} else if (spec.kind === "invoke") {
				const value = await this.dispatchInvoke(
					spec.serviceShortId,
					spec.method,
					spec.args,
					spec.kwargs,
				);
				this.fill(slot, { value });
			} else if (spec.kind === "handleInvoke") {
				const value = await this.fireSubscribeTo(slot, spec);
				this.fill(slot, { value });
			}
		} catch (e) {
			this.fill(slot, { message: e instanceof Error ? e.message : String(e) });
		}
	}

	/** Dispatch a service-target invoke on its descriptor; returns the decoded result. */
	private async dispatchInvoke(
		serviceShortId: string,
		method: string,
		wireArgs: readonly WireValue[],
		wireKwargs: { readonly [key: string]: WireValue },
	): Promise<ResultValue> {
		const descriptor = this.connector.serviceDescriptorFor(serviceShortId);
		if (descriptor.kind === "http") {
			const result = await this.connector.invokeHttpService(
				descriptor,
				method,
				wireArgs,
				wireKwargs,
			);
			return unescapeWire(result, this.decodeOpts());
		}
		// ws or transient: route over the pooled connection.
		let url: string;
		let service: string;
		let expectedGeneration: number | undefined;
		if (descriptor.kind === "transient") {
			const binding = this.connector.transientBindingFor(serviceShortId);
			if (!binding) throw new Error("transient service is not bound to a live connection");
			url = binding.url;
			service = descriptor.id ?? "";
			expectedGeneration = binding.generation;
		} else {
			url = toWsUrl(this.connector.url, descriptor.url ?? "");
			service = descriptor.name ?? "";
		}
		const result = await this.connector.wsPool.invoke(
			url,
			{ service, method, args: wireArgs, kwargs: wireKwargs },
			expectedGeneration !== undefined ? { expectedGeneration } : undefined,
		);
		const gen = this.connector.wsPool.liveGeneration(url);
		return unescapeWire(result, this.decodeOpts(gen > 0 ? { url, generation: gen } : undefined));
	}

	/**
	 * Reinterpret a handle-targeted `subscribe_to` as a source-targeted
	 * `source.<method ?? "subscribe">(inboxHandle)` and dispatch it. On a
	 * ws source, record the resulting transient unsubscribe handle +
	 * connection and flip the inbox's `connectionState` to connected.
	 *
	 * Returns `null` regardless of what the source returned — matching the
	 * workstation's `WorkstationSessionEventInbox.subscribe_to -> None`, so
	 * the op-result the frontend sees fills with `null` on both connectors
	 * (behavioral audit I2). The unsubscribe handle is captured internally
	 * (`st.unsubscribe`) for best-effort teardown, not surfaced.
	 */
	private async fireSubscribeTo(
		_slot: number,
		spec: Extract<OpSpec, { kind: "handleInvoke" }>,
	): Promise<ResultValue> {
		const source = spec.args[0];
		if (!(source instanceof ServiceRefStub)) {
			throw new Error("subscribe_to: first arg must be a service ref (the event source)");
		}
		const subMethod = typeof spec.args[1] === "string" ? spec.args[1] : "subscribe";
		const handle = new HttpCallbackHandleStub(spec.callbackSlot, this);
		const { args: wireArgs, kwargs: wireKwargs } = escapeInvokeArgs(
			[handle],
			{},
			this.encodeOpts(),
		);
		const descriptor = this.connector.serviceDescriptorFor(source.shortId);
		const value = await this.dispatchInvoke(source.shortId, subMethod, wireArgs, wireKwargs);
		// ws source: the result is a transient unsubscribe handle; record
		// it + the (url, generation) and mark the inbox connected.
		if (descriptor.kind === "ws" || descriptor.kind === "transient") {
			const url =
				descriptor.kind === "transient"
					? this.connector.transientBindingFor(source.shortId)?.url
					: toWsUrl(this.connector.url, descriptor.url ?? "");
			const generation = url ? this.connector.wsPool.liveGeneration(url) : 0;
			const st = this.inboxState.get(spec.callbackSlot);
			if (st && url && generation > 0) {
				st.subscription = { url, generation };
				if (value instanceof ServiceRefStub) st.unsubscribe = value;
				this.setInboxSubState(spec.callbackSlot, { state: "connected", reason: null });
			}
		}
		// Match `subscribe_to -> None`: the op-result fills with null, not
		// the unsubscribe handle (which is recorded internally above).
		return null;
	}

	private async bestEffortUnsubscribe(handle: ServiceRefStub): Promise<void> {
		try {
			await this.dispatchInvoke(handle.shortId, "unsubscribe", [], {});
		} catch {
			/* best-effort: the connection may already be gone */
		}
	}

	// -- local fill / notify -----------------------------------------

	private fill(slot: number, result: { value: ResultValue } | { message: string }): void {
		const seq = ++this.seqCounter;
		const record: FillRecord =
			"value" in result
				? { kind: "filled", seq, value: result.value }
				: { kind: "errored", seq, message: result.message };
		const next = new Map(this.store.get(this.slotFillsPrivate));
		next.set(slot, record);
		this.store.set(this.slotFillsPrivate, next);
		this.schedulePersist();
	}

	private handleNotify(slot: number, payload: WireValue): void {
		let decoded: ResultValue;
		try {
			decoded = unescapeWire(payload, this.decodeOpts());
		} catch (e) {
			console.error(
				`HttpSession(${this.sessionId}): notify decode failed for slot ${slot}: ${String(e)}`,
			);
			return;
		}
		const seq = ++this.seqCounter;
		const oldLogs = this.store.get(this.inboxLogsPrivate);
		const existing = oldLogs.get(slot);
		const entry: InboxEntry = { seq, payload: decoded };
		const newLog: InboxLog =
			existing === undefined
				? { firstIndex: 0, events: [entry] }
				: { firstIndex: existing.firstIndex, events: [...existing.events, entry] };
		const newLogs = new Map(oldLogs);
		newLogs.set(slot, newLog);
		this.store.set(this.inboxLogsPrivate, newLogs);
		this.schedulePersist();
	}

	private onConnectionDrop(url: string, generation: number): void {
		// Flip any inbox whose live subscription rode the dead (url, gen).
		for (const [slot, st] of this.inboxState) {
			if (
				st.subscription &&
				st.subscription.url === url &&
				st.subscription.generation === generation
			) {
				st.subscription = undefined;
				st.unsubscribe = undefined;
				this.setInboxSubState(slot, { state: "broken", reason: `ws connection ${url} dropped` });
			}
		}
	}

	private setInboxSubState(slot: number, state: InboxSubscriptionState): void {
		const next = new Map(this.store.get(this.inboxSubStatePrivate));
		next.set(slot, state);
		this.store.set(this.inboxSubStatePrivate, next);
		this.schedulePersist();
	}

	// -- GC -----------------------------------------------------------

	private reindexCommittedState(): void {
		const committed = this.store.get(this.committedAtom);
		const { opResultSlots, inboxSlotOffsets } = collectStubs(committed.user);
		this.store.set(this.committedSlotSetPrivate, opResultSlots);
		this.store.set(this.committedInboxSlotSetPrivate, new Set(inboxSlotOffsets.keys()));

		// Drop dedup reverse-index entries for slots no longer committed
		// (so a later allocate for the same shortId mints fresh, matching
		// the workstation once the stub leaves committed state).
		for (const slot of [...this.dedupSlotInfo.keys()]) {
			if (!opResultSlots.has(slot)) this.dedupSlotInfo.delete(slot);
		}

		// Drop slotFills for slots no longer referenced.
		const fills = this.store.get(this.slotFillsPrivate);
		let fillsChanged = false;
		const newFills = new Map<number, FillRecord>();
		for (const [slot, rec] of fills) {
			if (opResultSlots.has(slot)) newFills.set(slot, rec);
			else fillsChanged = true;
		}
		if (fillsChanged) this.store.set(this.slotFillsPrivate, newFills);

		// Trim / drop inbox logs; drop inbox bookkeeping for closed slots.
		const logs = this.store.get(this.inboxLogsPrivate);
		let logsChanged = false;
		const newLogs = new Map<number, InboxLog>();
		for (const [slot, log] of logs) {
			const minOffset = inboxSlotOffsets.get(slot);
			if (minOffset === undefined) {
				logsChanged = true; // slot gone from committed
				continue;
			}
			const drop = minOffset - log.firstIndex;
			if (drop > 0) {
				logsChanged = true;
				newLogs.set(slot, { firstIndex: minOffset, events: log.events.slice(drop) });
			} else {
				newLogs.set(slot, log);
			}
		}
		if (logsChanged) this.store.set(this.inboxLogsPrivate, newLogs);

		// Inbox slot no longer referenced in committed state → close it:
		// unregister its notify route + best-effort unsubscribe of a live
		// subscription. (A freshly-allocated-but-unstored callback is torn
		// down here too — correct, it was never really opened.)
		for (const [slot, st] of [...this.inboxState]) {
			if (inboxSlotOffsets.has(slot)) continue;
			this.connector.unregisterInbox(st.uuid);
			if (st.unsubscribe) void this.bestEffortUnsubscribe(st.unsubscribe);
			this.inboxState.delete(slot);
		}
	}

	// -- codec opts ---------------------------------------------------

	private encodeOpts(): EscapeOptions {
		return {
			encodeEntity: (stub) => this.connector.entityIdStringFor(stub.shortId),
			encodeService: (stub) => descriptorToWire(this.connector.serviceDescriptorFor(stub.shortId)),
			encodeCallbackHandle: (stub) => {
				if (!(stub instanceof HttpCallbackHandleStub) || stub.session !== this) {
					throw new WireEscapeError("callback handle is not from this session");
				}
				const uuid = this.inboxState.get(stub.slot)?.uuid;
				if (uuid === undefined) throw new WireEscapeError(`no inbox uuid for slot ${stub.slot}`);
				return [TAG_CALLBACK, uuid];
			},
		};
	}

	private decodeOpts(ctx?: TransientContext): ResultUnescapeOptions {
		return {
			// A service result's entity ref may be the layered 3-element form
			// `["entity", data, prov]`; `prov` is undefined for a bare 2-element
			// ref, which interns unprovenanced.
			lookupEntity: (id, prov) => this.connector.internEntity(id, prov),
			lookupServiceDescriptor: (d) =>
				this.connector.internServiceByDescriptor(descriptorFromWire(d), ctx),
		};
	}

	// -- persistence (design/05) -------------------------------------

	/**
	 * Persist after each commit / fill / notify / sub-state change,
	 * debounced to a microtask. The connector owns the `Storage` write.
	 * Bails once closed or detaching so a stray schedule can't resurrect
	 * a terminated session's blob.
	 */
	private schedulePersist(): void {
		if (this.persistScheduled || this.detaching) return;
		if (this.store.get(this.lifecycleAtom) === "closed") return;
		this.persistScheduled = true;
		queueMicrotask(() => {
			this.persistScheduled = false;
			this.persistNow();
		});
	}

	private persistNow(): void {
		if (this.store.get(this.lifecycleAtom) === "closed") return;
		try {
			this.connector.persistSession(this.sessionId, this.serialize());
		} catch (e) {
			console.error(`HttpSession(${this.sessionId}): serialize failed: ${String(e)}`);
		}
	}

	/**
	 * Serialize the committed session state into a persistable blob
	 * (design/05). Persisted state uses **shortId-mode escape**. Entity
	 * load + metadata payloads are *not* persisted (they refire on
	 * rehydrate, identified via the dedup reverse index); only invoke
	 * fills are.
	 */
	serialize(): SerializedSession {
		const enc = this.persistEncodeOpts();
		const committed = this.store.get(this.committedAtom);
		const userState = committed.user === null ? null : escapeWire(committed.user, enc);

		const dedup = {
			load: Object.fromEntries(this.committedDedup.load),
			get_entity_metadata: Object.fromEntries(this.committedDedup.get_entity_metadata),
			get_service_metadata: Object.fromEntries(this.committedDedup.get_service_metadata),
		};

		// Invoke fills only — load/metadata slots (in `dedupSlotInfo`) refire.
		const slotFills: Record<string, SerializedFill> = {};
		for (const [slot, rec] of this.store.get(this.slotFillsPrivate)) {
			if (this.dedupSlotInfo.has(slot)) continue;
			slotFills[String(slot)] =
				rec.kind === "filled"
					? { kind: "filled", seq: rec.seq, value: escapeWire(rec.value, enc) }
					: { kind: "errored", seq: rec.seq, message: rec.message };
		}

		const logs = this.store.get(this.inboxLogsPrivate);
		const subStates = this.store.get(this.inboxSubStatePrivate);
		const inboxes: Record<string, SerializedInbox> = {};
		for (const [slot, st] of this.inboxState) {
			const log = logs.get(slot);
			const sub = subStates.get(slot);
			inboxes[String(slot)] = {
				uuid: st.uuid,
				firstIndex: log?.firstIndex ?? 0,
				events: (log?.events ?? []).map((e) => ({
					seq: e.seq,
					payload: escapeWire(e.payload, enc),
				})),
				...(sub ? { sub: { state: sub.state, reason: sub.reason } } : {}),
			};
		}

		return {
			version: SESSION_BLOB_VERSION,
			metadata: this.metadata,
			createdAt: this.createdAt,
			slotCounter: this.localSlotCounter,
			seqCounter: this.seqCounter,
			userState,
			dedup,
			slotFills,
			inboxes,
		};
	}

	/**
	 * Rehydrate from a persisted blob (design/05). Decodes committed user
	 * state (interning slot/inbox stubs), restores dedup + invoke fills +
	 * inbox logs, refires pending load/metadata ops, marks in-flight
	 * invokes errored ("interrupted"), and rehydrates transient services
	 * + live subscriptions broken (their ws generation is gone).
	 *
	 * Returns `true` if the blob was fully restored, `false` if it
	 * couldn't be decoded (unsupported version / corrupt payload). The
	 * caller turns a `false` into a terminal `unavailable` session rather
	 * than a fabricated blank one — we never silently stand up a session
	 * under an id we couldn't restore. A failed decode has no
	 * connector-level side effects (inbox routes + op refires run only
	 * after the decode succeeds), so there's nothing to unwind.
	 */
	private restoreFrom(blob: SerializedSession): boolean {
		try {
			if (blob.version !== SESSION_BLOB_VERSION) {
				throw new Error(`unsupported session blob version ${blob.version}`);
			}
			this.metadata = blob.metadata;
			this.createdAt = blob.createdAt;
			this.localSlotCounter = blob.slotCounter;
			this.seqCounter = blob.seqCounter;

			// Dedup forward maps + the slot→{kind,shortId} reverse index.
			this.committedDedup = {
				load: new Map(Object.entries(blob.dedup.load)),
				get_entity_metadata: new Map(Object.entries(blob.dedup.get_entity_metadata)),
				get_service_metadata: new Map(Object.entries(blob.dedup.get_service_metadata)),
			};
			for (const kind of ["load", "get_entity_metadata", "get_service_metadata"] as DedupKind[]) {
				for (const [shortId, slot] of this.committedDedup[kind]) {
					this.dedupSlotInfo.set(slot, { kind, shortId });
				}
			}

			// Inbox bookkeeping + logs + sub-states (a previously-connected
			// subscription rehydrates broken — its ws generation is gone).
			const resultDec = this.persistResultDecodeOpts();
			const restoredLogs = new Map<number, InboxLog>();
			const restoredSubs = new Map<number, InboxSubscriptionState>();
			for (const [slotStr, sib] of Object.entries(blob.inboxes)) {
				const slot = Number(slotStr);
				this.inboxState.set(slot, {
					uuid: sib.uuid,
					registered: false,
					subscription: undefined,
					unsubscribe: undefined,
				});
				restoredLogs.set(slot, {
					firstIndex: sib.firstIndex,
					events: sib.events.map((e) => ({
						seq: e.seq,
						payload: unescapeWire(e.payload, resultDec),
					})),
				});
				if (sib.sub?.state === "connected") {
					restoredSubs.set(slot, { state: "broken", reason: "session reloaded; resubscribe" });
				} else if (sib.sub?.state === "broken") {
					restoredSubs.set(slot, { state: "broken", reason: sib.sub.reason });
				}
			}
			this.store.set(this.inboxLogsPrivate, restoredLogs);
			this.store.set(this.inboxSubStatePrivate, restoredSubs);

			// Persisted (invoke) fills.
			const restoredFills = new Map<number, FillRecord>();
			for (const [slotStr, sf] of Object.entries(blob.slotFills)) {
				const slot = Number(slotStr);
				restoredFills.set(
					slot,
					sf.kind === "filled"
						? { kind: "filled", seq: sf.seq, value: unescapeWire(sf.value, resultDec) }
						: { kind: "errored", seq: sf.seq, message: sf.message },
				);
			}
			this.store.set(this.slotFillsPrivate, restoredFills);

			// Decode committed user state (interns slot/inbox stubs).
			const broken = new Set<number>();
			const user =
				blob.userState === null
					? null
					: (unescapeWire(blob.userState, this.persistStateDecodeOpts(broken)) as UserState);
			if (broken.size > 0) this.store.set(this.brokenSlotsPrivate, broken);
			const env: Envelope = { user };
			this.store.set(this.committedAtom, env);
			this.store.set(this.localAtom, env);
			this.reindexCommittedState();

			// Register notify routes + refire / interrupt op-result slots.
			const { opResultSlots, inboxSlotOffsets } = collectStubs(user);
			for (const slot of inboxSlotOffsets.keys()) {
				const st = this.inboxState.get(slot);
				if (st && !st.registered) {
					st.registered = true;
					this.connector.registerInbox(st.uuid, (payload) => this.handleNotify(slot, payload));
				}
			}
			for (const slot of opResultSlots) {
				const info = this.dedupSlotInfo.get(slot);
				if (info !== undefined) {
					// load / metadata — payload not persisted; refire (stays pending until filled).
					void this.fireOp(slot, { kind: info.kind, shortId: info.shortId });
				} else if (!restoredFills.has(slot)) {
					// invoke in flight at save time → errored "interrupted".
					this.fill(slot, { message: "interrupted (session reloaded)" });
				}
			}

			return true;
		} catch (e) {
			console.error(
				`HttpSession(${this.sessionId}): rehydrate failed; session is unavailable: ${String(e)}`,
			);
			// Reset any partially-decoded state — the session will be marked
			// `unavailable` and nothing should read stale atoms off it.
			this.store.set(this.committedAtom, EMPTY_ENVELOPE);
			this.store.set(this.localAtom, EMPTY_ENVELOPE);
			return false;
		}
	}

	/** shortId-mode escape opts for persistence (entity/service → shortId; slots/offsets verbatim). */
	private persistEncodeOpts(): EscapeOptions {
		return {
			encodeEntity: (stub) => stub.shortId,
			encodeService: (stub) => stub.shortId,
			encodeOpResult: (stub) => {
				const slot = (stub as HttpOpResultStub).slot;
				if (slot === 0) return [TAG_BROKEN_OP_RESULT, 0];
				const isBroken = this.store.get(this.brokenSlotsPrivate).has(slot);
				return [isBroken ? TAG_BROKEN_OP_RESULT : TAG_OP_RESULT, slot];
			},
			encodeInbox: (stub) => {
				const ib = stub as HttpCallbackInboxStub;
				if (ib.slot === 0) return [TAG_BROKEN_INBOX, 0];
				return [TAG_INBOX, ib.slot, ib.offset];
			},
			// Handles never appear in committed state (only in invoke args);
			// supplied for completeness so an unexpected one fails loudly.
			encodeCallbackHandle: (stub) => [TAG_CALLBACK, (stub as HttpCallbackHandleStub).slot],
		};
	}

	/** Decode opts for persisted state (shortId mode; slot/inbox stubs are session-scoped). */
	private persistStateDecodeOpts(broken: Set<number>): StateUnescapeOptions {
		return {
			lookupEntity: (shortId) => this.connector.entityRef(shortId),
			lookupService: (shortId) => {
				// A transient service's ws generation is gone after a reload —
				// rehydrate it born-broken. Unknown shortIds are logged but
				// still interned (resolution fails later, gracefully).
				try {
					if (this.connector.serviceDescriptorFor(shortId).kind === "transient") {
						this.connector.markServiceBroken(shortId);
					}
				} catch {
					console.error(
						`HttpSession(${this.sessionId}): unknown service shortId during rehydrate: ${shortId}`,
					);
				}
				return this.connector.serviceRef(shortId);
			},
			lookupOpResult: (slot, isBroken) => {
				if (slot === 0) return this.deadSentinel;
				if (isBroken) broken.add(slot);
				return this.opResult(slot);
			},
			lookupInbox: (slot, offset, isBroken) => {
				if (isBroken || slot === 0) return this.deadInboxSentinel;
				return new HttpCallbackInboxStub(slot, offset, this);
			},
		};
	}

	/** Decode opts for persisted result values (fills + inbox payloads — no client-only stubs). */
	private persistResultDecodeOpts(): ResultUnescapeOptions {
		return {
			lookupEntity: (shortId) => this.connector.entityRef(shortId),
			lookupService: (shortId) => this.connector.serviceRef(shortId),
		};
	}
}

type FxtrUpdateLike =
	| (UserState | null)
	| ((prev: UserState | null, ctx: FxtrSessionUpdateCtx) => UserState | null);

function freshDedup(): Record<DedupKind, Map<string, number>> {
	return { load: new Map(), get_entity_metadata: new Map(), get_service_metadata: new Map() };
}
