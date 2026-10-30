/**
 * `WorkstationConnector`: consumer-facing wrapper around a
 * `WorkstationServerConnection`. Exposes the four workstation
 * resource lists (`listRpcConnections` / `listServices` /
 * `listEntities` / `listSessions`) plus `createSession` /
 * `attachSession` for session-level interaction.
 *
 * Construction takes either a URL (the connector mints and owns its
 * own `WorkstationServerConnection`) or an existing connection (the
 * connector borrows it; close-of-connector leaves it alive). Tests
 * use the latter to inject a mock `WebSocket` at the connection
 * layer; production code uses the former.
 *
 * Resource lists are workstation-scoped singletons. `listEntities()`
 * called twice returns the same `ReactiveList`; the connector
 * subscribes to each (resource, debug) pair at most once for its
 * lifetime. Consumers can't close individual lists — there's no
 * `close()` on `ReactiveList` — they're torn down only by
 * `connector.close()`. This avoids the question of "what happens
 * when one consumer closes a list other consumers are still reading."
 */

import type { Connector, JsonValue } from "@fxtr-ui/core";
import { type EntityRefStub, unescapeWire } from "@fxtr-ui/core";
import { type Atom, atom, getDefaultStore } from "jotai";
import { type ConnectionStatus, WorkstationServerConnection } from "./connection";
import type {
	EntityRecord,
	ResourceKind,
	RpcConnectionRecord,
	ServiceRecord,
	SessionRecord,
} from "./protocol";
import { type ReactiveList, createReactiveResourceList } from "./resource_list";
import { WorkstationSession } from "./session";
import { WorkstationEntityRefStub, WorkstationServiceRefStub } from "./stubs";

/** Internal cache entry: the public list + a release fn the
 * connector keeps to itself for borrowed-connection teardown. */
interface CachedList {
	readonly list: ReactiveList<unknown>;
	readonly release: () => void;
}

type JotaiStore = ReturnType<typeof getDefaultStore>;

/**
 * Options for `new WorkstationConnector(...)`. Either provide a
 * `url` (the connector mints and owns a `WorkstationServerConnection`)
 * or an existing `connection` (the connector borrows it and does not
 * close it on teardown). Tests use the latter; production uses the
 * former.
 */
export type WorkstationConnectorOptions =
	| { readonly url: string; readonly store?: JotaiStore }
	| { readonly connection: WorkstationServerConnection; readonly store?: JotaiStore };

/** Per-list options accepted by every `listX` method. */
export interface ListResourceOptions {
	/**
	 * When `true`, subscribe with the workstation's `debug` flag set —
	 * server includes the debug-only fields on each record (see
	 * `RpcConnectionRecord` / `ServiceRecord` / `EntityRecord` in
	 * `protocol.ts`). Debug and non-debug lists are cached
	 * independently; both can be open at once. Sessions have no
	 * debug-only fields today but accept the flag for symmetry.
	 */
	readonly debug?: boolean;
}

/**
 * Thrown when a `listX` call lands on a `WorkstationConnector` that
 * has been closed — either via explicit `connector.close()` or
 * because its underlying connection reached terminal `"closed"`
 * state (in which case the connector self-closes; see
 * `WorkstationConnector` constructor for the status observer).
 *
 * Distinct from `ConnectionClosedError` (in `connection.ts`), which
 * is rejected by `sendRequest` at the wire layer when the
 * `WorkstationServerConnection` itself has been closed. The two
 * exist for the two layers; a closed connector usually implies a
 * closed connection, but consumers throwing on `listX` see the
 * higher-level one.
 */
export class ConnectorClosedError extends Error {
	constructor(message = "WorkstationConnector is closed") {
		super(message);
		this.name = "ConnectorClosedError";
	}
}

/**
 * Thrown (as a promise rejection from `WorkstationConnector.loadEntityData`)
 * when the workstation server replies with `LoadEntityErrorResponse` —
 * e.g. the short_id is unknown or no longer active in the server's
 * direct-load allocator. Carries the entity's short_id and the
 * server-supplied message so callers can surface both.
 *
 * Distinct from transport errors (`ConnectionLostError` /
 * `ConnectionClosedError`), `ProtocolError`, and decode errors
 * (`WireEscapeError`) — all of which can also reject the same promise.
 */
export class LoadEntityError extends Error {
	readonly shortId: string;
	readonly serverMessage: string;
	constructor(shortId: string, serverMessage: string) {
		super(`load_entity(${shortId}) failed: ${serverMessage}`);
		this.name = "LoadEntityError";
		this.shortId = shortId;
		this.serverMessage = serverMessage;
	}
}

/** Cache key: `${resource}:${debug ? 1 : 0}`. */
function cacheKey(resource: ResourceKind, debug: boolean): string {
	return `${resource}:${debug ? 1 : 0}`;
}

export class WorkstationConnector implements Connector {
	/** Underlying transport's connection status. Re-exposed for convenience. */
	readonly connectionStatus: Atom<ConnectionStatus>;

	/**
	 * The connector's own jotai store. Holds connector-level reactive
	 * state — the resource lists (`listEntities` etc.) and the
	 * underlying connection's `connectionStatus`. Distinct from
	 * per-session stores: `session.store` is its own thing.
	 */
	readonly store: JotaiStore;

	/**
	 * The WebSocket URL of the underlying connection. Surfaced so
	 * UI chrome (e.g. the shell's connection-status header) can
	 * display it without the consumer threading it separately
	 * alongside the connector.
	 */
	get url(): string {
		return this.connection.url;
	}

	/**
	 * Underlying wire-transport connection. Public so consumers
	 * needing low-level access (e.g. debug surfaces opening their
	 * own session-event / session-debug subscriptions via
	 * `registerSubscription`) can reach it without going through a
	 * passthrough wrapper. The standard interactive paths
	 * (`createSession` / `attachSession` / `listX`) don't need this.
	 */
	readonly connection: WorkstationServerConnection;
	private readonly connectionOwned: boolean;
	private readonly lists = new Map<string, CachedList>();
	private readonly unsubscribeConnectionStatus: () => void;
	private closed = false;

	// Connector-scoped intern maps. Pure refs (entity/service) have
	// no per-session state, so sharing across sessions is fine and
	// gives us instance equality for the same shortId regardless of
	// whether the ref was wire-decoded inside a session or minted
	// directly via `entityRef` / `serviceRef`. `OpResultStub` and
	// `CallbackInboxStub` stay session-scoped because they carry
	// per-session slot identity.
	private readonly entityStubs = new Map<string, WorkstationEntityRefStub>();
	private readonly serviceStubs = new Map<string, WorkstationServiceRefStub>();

	// Connector-level set of service short_ids known to be broken.
	// Brokenness is a global workstation fact (a service breaks for
	// every session at once), so it's coalesced here and read by every
	// interned `WorkstationServiceRefStub`'s `state` atom. Fed by
	// session `ServiceBrokenNotification` frames (via the
	// `markServiceBroken` callback handed to each session) and by the
	// `services` resource list's `broken` field.
	private readonly brokenServicesAtom = atom<ReadonlySet<string>>(new Set<string>());

	// Unsubscribe fns for the `services` resource list → broken-set
	// feed (a connector subscribed to the services list also surfaces
	// `broken` records into `brokenServicesAtom`). Drained on close.
	private readonly brokenFeedUnsubs: Array<() => void> = [];

	constructor(opts: WorkstationConnectorOptions) {
		this.store = opts.store ?? getDefaultStore();
		if ("connection" in opts) {
			this.connection = opts.connection;
			this.connectionOwned = false;
		} else {
			this.connection = new WorkstationServerConnection({
				url: opts.url,
				store: this.store,
			});
			this.connectionOwned = true;
		}
		this.connectionStatus = this.connection.connectionStatus;

		// Track the underlying connection's lifecycle so the connector
		// self-closes when the connection reaches terminal "closed"
		// state. Matters most in the borrowed-connection case (some
		// other code closed the connection out from under us); in the
		// owned case our own close() drives the transition.
		this.unsubscribeConnectionStatus = this.store.sub(this.connection.connectionStatus, () => {
			if (this.store.get(this.connection.connectionStatus) === "closed") {
				this.markClosed();
			}
		});
	}

	listRpcConnections(opts: ListResourceOptions = {}): ReactiveList<RpcConnectionRecord> {
		return this.getOrCreateList<RpcConnectionRecord>("rpc_connections", opts.debug ?? false);
	}

	listServices(opts: ListResourceOptions = {}): ReactiveList<ServiceRecord> {
		return this.getOrCreateList<ServiceRecord>("services", opts.debug ?? false);
	}

	listEntities(opts: ListResourceOptions = {}): ReactiveList<EntityRecord> {
		return this.getOrCreateList<EntityRecord>("entities", opts.debug ?? false);
	}

	listSessions(opts: ListResourceOptions = {}): ReactiveList<SessionRecord> {
		return this.getOrCreateList<SessionRecord>("sessions", opts.debug ?? false);
	}

	/**
	 * Create a new workstation session and return a
	 * `WorkstationSession` attached to it. Async because it sends a
	 * `CreateSessionRequest` and awaits the server-allocated
	 * `session_id` before constructing the session.
	 *
	 * Rejects with `ConnectorClosedError` if called on a closed
	 * connector (synchronously checked at call time, and again
	 * after the round-trip in case `close()` happened mid-flight).
	 * If the request itself fails (`ProtocolError`,
	 * `ConnectionLostError`, etc.), the promise rejects with that
	 * error and no session is constructed.
	 */
	async createSession(
		metadata: JsonValue = {},
		opts: { readonly store?: JotaiStore } = {},
	): Promise<WorkstationSession> {
		if (this.closed) throw new ConnectorClosedError();
		const response = await this.connection.sendRequest({
			op: "create_session",
			metadata,
		});
		if (this.closed) throw new ConnectorClosedError();
		return new WorkstationSession({
			connection: this.connection,
			sessionId: response.session_id,
			store: opts.store ?? this.store,
			lookupEntity: (shortId) => this.entityRef(shortId),
			lookupService: (shortId) => this.serviceRef(shortId),
			markServiceBroken: (shortId) => this.markServiceBroken(shortId),
		});
	}

	/**
	 * Attach to an existing workstation session by its
	 * `session_id`. Sync: returns the session immediately, its
	 * own state machine handles the in-flight subscribe (starts
	 * in `connecting`, transitions to `connected` when the
	 * server's snapshot arrives).
	 *
	 * No caching: two `attachSession("abc")` calls return two
	 * independent sessions, each with its own subscription. The
	 * server allows multiple concurrent subscribers on the same
	 * session_id, and sessions carry per-consumer state that
	 * doesn't share cleanly.
	 *
	 * Throws `ConnectorClosedError` if called on a closed
	 * connector.
	 */
	attachSession(sessionId: string, opts: { readonly store?: JotaiStore } = {}): WorkstationSession {
		if (this.closed) throw new ConnectorClosedError();
		return new WorkstationSession({
			connection: this.connection,
			sessionId,
			store: opts.store ?? this.store,
			lookupEntity: (shortId) => this.entityRef(shortId),
			lookupService: (shortId) => this.serviceRef(shortId),
			markServiceBroken: (shortId) => this.markServiceBroken(shortId),
		});
	}

	/**
	 * Stateless entity-data load. Sends a `LoadEntityRequest` over
	 * the underlying connection (the workstation's out-of-band
	 * direct-load path) and resolves with the decoded payload.
	 *
	 * This is **not** the session-aware load. Specifically, the server's
	 * `direct_load_worker` opens a transient load_scope, runs the load,
	 * escapes children via `get_or_assign_entity_inactive`, and replies.
	 * It does NOT touch refcounts, mint a `TrackedEntityRecord`, or
	 * pin anything past the response — and the entity ref stubs
	 * appearing inside the decoded payload are **descriptors for
	 * display only**. Two implications callers must respect:
	 *
	 *   1. Passing a stub that came out of a `loadEntityData` result
	 *      back into `loadEntityData` is NOT guaranteed to succeed —
	 *      the child entity may not be lookupable through the server's
	 *      active allocator, in which case the call rejects with
	 *      `LoadEntityError`.
	 *   2. Callers wanting to traverse children should use the session
	 *      system (`FxtrSessionUpdateCtx.allocateLoad`), which goes
	 *      through proper refcounting and child-liveness guarantees.
	 *
	 * The resolved value is structurally a `ResultValue` (same shape
	 * as the in-session load result payloads); the cast to `T` is
	 * unchecked, matching the rest of the stub-typed surface. Returned
	 * `EntityRefStub` instances are reference-equal to whatever
	 * `connector.entityRef(shortId)` returns, since both paths share
	 * the connector's intern map.
	 *
	 * Rejection cases:
	 *   - `LoadEntityError` — server replied with `load_entity_error`.
	 *   - `WireEscapeError` — payload couldn't be decoded.
	 *   - `ConnectionLostError` / `ConnectionClosedError` /
	 *     `ProtocolError` — transport-level failures from `sendRequest`.
	 *
	 * Rejects with `ConnectorClosedError` if the connector is already
	 * closed (matches `createSession`'s same gate; since the method is
	 * async, the gate surfaces as a rejected promise rather than a
	 * synchronous throw).
	 *
	 * No caching, no retry, no abort signal — each call ships one
	 * wire request.
	 */
	async loadEntityData<T = unknown>(stub: EntityRefStub<T>): Promise<T> {
		if (this.closed) throw new ConnectorClosedError();
		const response = await this.connection.sendRequest({
			op: "load_entity",
			short_id: stub.shortId,
		});
		if (response.type === "load_entity_error") {
			throw new LoadEntityError(stub.shortId, response.message);
		}
		// Result-mode decode: no client-only stub lookups. Service refs
		// can't appear in entity data by system invariant, but we pass
		// `serviceRef` as defense-in-depth so a violation produces a
		// connector-interned stub instead of throwing.
		const decoded = unescapeWire(response.payload, {
			lookupEntity: (shortId) => this.entityRef(shortId),
			lookupService: (shortId) => this.serviceRef(shortId),
		});
		return decoded as T;
	}

	/**
	 * Get the canonical `WorkstationEntityRefStub` for a short_id —
	 * minted on first call, interned thereafter. Two calls with the
	 * same `shortId` return the same instance; a wire-decoded ref
	 * for the same shortId from any session also collapses to this
	 * instance. Pure value constructor — safe to call after
	 * `connector.close()`.
	 */
	entityRef<T = unknown>(shortId: string): WorkstationEntityRefStub<T> {
		const existing = this.entityStubs.get(shortId);
		if (existing) return existing as WorkstationEntityRefStub<T>;
		const stub = new WorkstationEntityRefStub<T>(shortId);
		this.entityStubs.set(shortId, stub);
		return stub;
	}

	/**
	 * Get the canonical `WorkstationServiceRefStub` for a short_id.
	 * See `entityRef` — same semantics for services.
	 */
	serviceRef<T = unknown>(shortId: string): WorkstationServiceRefStub<T> {
		const existing = this.serviceStubs.get(shortId);
		if (existing) return existing as WorkstationServiceRefStub<T>;
		const stub = new WorkstationServiceRefStub<T>(shortId, this.brokenServicesAtom);
		this.serviceStubs.set(shortId, stub);
		return stub;
	}

	/**
	 * Classify a bare `shortId` as naming an entity or a service
	 * (`null` if neither list knows it). The `Connector`-interface
	 * method the dispatch ref-resolver consumes: the workstation
	 * answers it by scanning its (always-loaded) entity + service
	 * resource lists — no reliance on shortId-prefix syntax. Walks the
	 * records once per call (O(n + m)); cheap, and intentionally
	 * un-memoized since the result may flip as the workstation pushes
	 * resource-list updates.
	 */
	classifyShortId(shortId: string): "entity" | "service" | null {
		const entities = this.listEntities();
		for (const r of this.store.get(entities.records)) {
			if (r.short_id === shortId) return "entity";
		}
		const services = this.listServices();
		for (const r of this.store.get(services.records)) {
			if (r.short_id === shortId) return "service";
		}
		return null;
	}

	/**
	 * Read-only view of the connector-level broken-services set.
	 * Exposed so consumers / tests can observe coalesced brokenness
	 * directly; the usual path is `serviceRef(id).state`.
	 */
	get brokenServices(): Atom<ReadonlySet<string>> {
		return this.brokenServicesAtom;
	}

	/**
	 * Mark a service short_id broken connector-wide. Idempotent — a
	 * repeat is a no-op (no spurious atom write). Called by sessions on
	 * a `ServiceBrokenNotification` and by the `services` resource list
	 * for `broken` records. Every interned `WorkstationServiceRefStub`
	 * for the short_id reactively flips its `state` to `"broken"`.
	 */
	markServiceBroken(shortId: string): void {
		const current = this.store.get(this.brokenServicesAtom);
		if (current.has(shortId)) return;
		const next = new Set(current);
		next.add(shortId);
		this.store.set(this.brokenServicesAtom, next);
	}

	private getOrCreateList<T>(resource: ResourceKind, debug: boolean): ReactiveList<T> {
		if (this.closed) throw new ConnectorClosedError();

		const key = cacheKey(resource, debug);
		const existing = this.lists.get(key);
		if (existing) return existing.list as ReactiveList<T>;

		const { list, release } = createReactiveResourceList<T>({
			connection: this.connection,
			store: this.store,
			resource,
			debug,
		});
		this.lists.set(key, { list: list as ReactiveList<unknown>, release });

		if (resource === "services") {
			// Secondary feed: surface `broken` records from the services
			// list into the connector-level broken set (covers a
			// list-only connector with no session tracking the service).
			// Idempotent with the session-frame feed.
			const servicesList = list as ReactiveList<ServiceRecord>;
			const scan = () => {
				for (const rec of this.store.get(servicesList.records)) {
					if (rec.broken) this.markServiceBroken(rec.short_id);
				}
			};
			this.brokenFeedUnsubs.push(this.store.sub(servicesList.records, scan));
			scan(); // catch records already present in the snapshot
		}
		return list;
	}

	/**
	 * Tear down the connector. If it owns its underlying connection
	 * (URL constructor), closes that too and returns its close
	 * promise; otherwise releases its lists' subscriptions and
	 * returns immediately. Idempotent.
	 */
	close(): Promise<void> {
		if (this.closed) return Promise.resolve();
		this.markClosed();

		if (this.connectionOwned) {
			// connection.close() fans out onClose("connector_closed")
			// to every active subscription, including every list's,
			// which each flip to terminal "closed" status.
			return this.connection.close();
		}

		// Borrowed connection: ask each list to release its
		// subscription (sends an unsubscribe + transitions to
		// "closed"), leave the connection alone.
		for (const { release } of this.lists.values()) release();
		return Promise.resolve();
	}

	/**
	 * Marks the connector closed and unsubscribes the connection-
	 * status observer. Called from `close()` and from the observer
	 * itself when the underlying connection reaches `"closed"`.
	 * Idempotent.
	 */
	private markClosed(): void {
		if (this.closed) return;
		this.closed = true;
		this.unsubscribeConnectionStatus();
		for (const unsub of this.brokenFeedUnsubs) unsub();
		this.brokenFeedUnsubs.length = 0;
	}
}
