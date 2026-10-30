/**
 * `ReactiveList<T>`: the consumer-facing view of one workstation
 * resource subscription. Owns its `SubscriptionHandle` and a
 * primary-key-keyed `Map<string, T>` that snapshots and deltas fold
 * into; the public `records` atom is rebuilt from the map on every
 * change.
 *
 * Lifecycle (mirrored in the `status` atom):
 *
 *   loading - just constructed; the subscribe request is in flight,
 *             no snapshot yet. `records` is the empty array.
 *
 *   ready   - snapshot has arrived; deltas are being applied. This
 *             is the normal steady state.
 *
 *   stale   - the underlying socket dropped (`onClose("reset")`).
 *             We immediately re-register the subscription against
 *             the same connection — the connection's outbound
 *             queue holds the subscribe request until the socket is
 *             back open. `records` keeps the last-known values so
 *             consumers don't see a UI flash; they flip to the
 *             fresh server snapshot when it arrives, at which
 *             point status returns to `ready`.
 *
 *   closed  - terminal. Reached via the connector tearing down or
 *             a server-side subscribe rejection. No further updates;
 *             `records` retains its last value. Consumers cannot
 *             close a `ReactiveList` themselves — lists are
 *             connector-owned singletons; the connector tears them
 *             all down at once via `connector.close()`.
 *
 * The connection layer queues the resubscribe across the reconnect —
 * `connection.registerSubscription` itself enqueues sends on a
 * not-yet-open socket, so this class never has to watch
 * `connectionStatus`.
 */

import { type Atom, type PrimitiveAtom, atom, type getDefaultStore } from "jotai";
import type {
	SubscriptionCloseReason,
	SubscriptionFrame,
	SubscriptionHandle,
	SubscriptionHandler,
	WorkstationServerConnection,
} from "./connection";
import { RESOURCE_KEY_FIELD, type ResourceKind } from "./protocol";

/** Observable lifecycle phases of a `ReactiveList`. */
export type ReactiveListStatus = "loading" | "ready" | "stale" | "closed";

/**
 * Consumer view of a single resource subscription. Read `status` and
 * `records` via the configured jotai store. Lists are connector-owned
 * singletons; there is no public `close()` because the connector
 * tears them down centrally — see `WorkstationConnector.close()`.
 */
export interface ReactiveList<T> {
	readonly status: Atom<ReactiveListStatus>;
	readonly records: Atom<readonly T[]>;
}

type JotaiStore = ReturnType<typeof getDefaultStore>;

interface ReactiveResourceListOptions {
	readonly connection: WorkstationServerConnection;
	readonly store: JotaiStore;
	readonly resource: ResourceKind;
	readonly debug: boolean;
}

/**
 * Internal implementation. The connector constructs these via the
 * `createReactiveResourceList` factory; consumers see only the
 * `ReactiveList<T>` interface.
 *
 * `T` is unconstrained because the workstation record types
 * (`EntityRecord` etc.) have specific known fields and no string
 * index signature — they don't fit `Record<string, unknown>`. We
 * type the internal map as `T` and cast at the boundary where we
 * look up the primary-key field on a raw inbound dict.
 */
class ReactiveResourceList<T> implements ReactiveList<T> {
	readonly status: Atom<ReactiveListStatus>;
	readonly records: Atom<readonly T[]>;

	private readonly connection: WorkstationServerConnection;
	private readonly store: JotaiStore;
	private readonly resource: ResourceKind;
	private readonly debug: boolean;
	private readonly keyField: string;
	private readonly statusAtom: PrimitiveAtom<ReactiveListStatus>;
	private readonly recordsAtom: PrimitiveAtom<readonly T[]>;

	private readonly recordsMap = new Map<string, T>();
	private handle: SubscriptionHandle | null = null;
	private terminal = false;

	constructor(opts: ReactiveResourceListOptions) {
		this.connection = opts.connection;
		this.store = opts.store;
		this.resource = opts.resource;
		this.debug = opts.debug;
		this.keyField = RESOURCE_KEY_FIELD[opts.resource];

		this.statusAtom = atom<ReactiveListStatus>("loading");
		this.recordsAtom = atom<readonly T[]>([]);
		this.status = this.statusAtom;
		this.records = this.recordsAtom;

		this.register();
	}

	/**
	 * Called by the connector during `connector.close()` in the
	 * borrowed-connection case. Closes the underlying subscription
	 * (sends an unsubscribe, fires `onClose("unsubscribed")`, which
	 * routes through `onClose` and transitions us to terminal
	 * `"closed"`). No-op once already terminal.
	 *
	 * Not part of the public `ReactiveList<T>` surface — only the
	 * connector holds a reference to this function (via the factory's
	 * return shape).
	 */
	releaseFromConnector(): void {
		if (this.terminal) return;
		this.handle?.close();
		if (!this.terminal) {
			// `handle.close()` is documented to fan synchronously
			// through `onClose("unsubscribed")`, which sets us
			// terminal. Reaching here means that contract was
			// violated (or the handle was already null — e.g. a
			// terminal state without `terminal=true` being set,
			// which would be a bug in this class). Surface it
			// loudly and drive the terminal transition anyway.
			console.warn(
				`[fxtr-ui] ReactiveList(${this.resource}): handle.close() did not fan through onClose; forcing terminal state. This suggests a contract violation in the connection layer.`,
			);
			this.markTerminal();
		}
	}

	private register(): void {
		const handler: SubscriptionHandler = {
			onFrame: (frame) => this.onFrame(frame),
			onClose: (reason, error) => this.onClose(reason, error),
		};
		this.handle = this.connection.registerSubscription(
			{ kind: "resource", resource: this.resource, debug: this.debug },
			handler,
		);
	}

	private onFrame(frame: SubscriptionFrame): void {
		if (frame.type === "resource_snapshot") {
			this.recordsMap.clear();
			for (const record of frame.data) this.upsert(record);
			this.publishRecords();
			this.store.set(this.statusAtom, "ready");
			return;
		}
		if (frame.type === "resource_changes") {
			// added / updated / removed are disjoint per server
			// invariant; apply in any order.
			for (const key of frame.removed) this.recordsMap.delete(key);
			for (const record of frame.added) this.upsert(record);
			for (const record of frame.updated) this.upsert(record);
			this.publishRecords();
			return;
		}
		// Session-shaped frame on a resource subscription = protocol
		// invariant violation. Log and ignore; failing closed here
		// would tear down the list on what's almost certainly a
		// server bug.
		console.warn(
			`[fxtr-ui] ReactiveList(${this.resource}) got unexpected frame type ${frame.type}`,
		);
	}

	private onClose(reason: SubscriptionCloseReason, error?: Error): void {
		this.handle = null;
		if (reason === "reset") {
			// Keep records visible; re-register so the next snapshot
			// will refresh them. The connection's outbound queue holds
			// the subscribe until the socket reopens.
			this.store.set(this.statusAtom, "stale");
			this.register();
			return;
		}
		if (reason === "subscribe_failed") {
			// Server explicitly rejected our subscribe — almost
			// certainly a protocol mismatch (unknown resource, etc.).
			// Surface the error so it isn't silently lost; the list
			// itself only exposes `status === "closed"`, which is
			// usually enough for UI but not enough for debugging the
			// "why."
			console.error(
				`[fxtr-ui] ReactiveList(${this.resource}) subscribe failed:`,
				error ?? "(no error)",
			);
		}
		// "unsubscribed" | "connector_closed" | "subscribe_failed"
		// are all terminal. Records keep their last value (consumers
		// may still want to render the snapshot they have).
		this.markTerminal();
	}

	private markTerminal(): void {
		if (this.terminal) return;
		this.terminal = true;
		this.store.set(this.statusAtom, "closed");
	}

	private upsert(record: Readonly<Record<string, unknown>>): void {
		const key = record[this.keyField];
		if (typeof key !== "string") {
			console.warn(
				`[fxtr-ui] ReactiveList(${this.resource}) record missing key field "${this.keyField}":`,
				record,
			);
			return;
		}
		this.recordsMap.set(key, record as T);
	}

	private publishRecords(): void {
		// New array on every publish — jotai equality is reference,
		// so a fresh reference is what triggers subscribers. Cheap
		// at the scale of "resource lists in a single workstation."
		this.store.set(this.recordsAtom, Array.from(this.recordsMap.values()));
	}
}

/**
 * Result of `createReactiveResourceList`. `list` is the public
 * consumer-facing view; `release` is a private hook the connector
 * keeps for itself to tear down the subscription in the
 * borrowed-connection case (the URL-owning case relies on
 * `connection.close()` to fan onClose("connector_closed") through
 * naturally).
 */
export interface CreatedReactiveList<T> {
	readonly list: ReactiveList<T>;
	readonly release: () => void;
}

/**
 * Factory used by `WorkstationConnector`. Returns both the public
 * list and a private release function in the same call; only the
 * connector ever sees `release`.
 */
export function createReactiveResourceList<T>(
	opts: ReactiveResourceListOptions,
): CreatedReactiveList<T> {
	const impl = new ReactiveResourceList<T>(opts);
	return { list: impl, release: () => impl.releaseFromConnector() };
}
