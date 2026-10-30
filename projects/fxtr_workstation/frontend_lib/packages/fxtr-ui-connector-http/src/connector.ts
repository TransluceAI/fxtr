/**
 * `HttpConnector` — the stateless connector's transport + identity
 * layer. Owns the jotai store, the shortId table (persisted through the
 * `Storage` handle), the entity/service stub intern maps, the
 * `EntityFetcher`, the `WsPool`, and the discovery lists. Sessions
 * (added in `session.ts`) hang off it and use its interning + codec +
 * ws transport.
 *
 * This file holds the connector core; `createSession` / `attachSession`
 * (and the `implements Connector` conformance) live with the session
 * reducer. Everything here is connector-scoped (cross-session): stub
 * identity is a function of `shortId` alone, and the shortId table +
 * discovery lists are shared by every session.
 */

import type {
	ConnectionStatus,
	Connector,
	EntityMetadata,
	EntityRefStub,
	JsonValue,
	ServiceMetadata,
	ServiceRefStub,
	Session,
	WireValue,
} from "@fxtr-ui/core";
import { type Atom, atom, createStore, type getDefaultStore } from "jotai";

import { type ServiceDescriptor, descriptorFromWire } from "./descriptor";
import { EntityFetcher, EntityLoadError, type FetchFn } from "./entity_data";
import { cborToResultValue, uNodeIdStringFor } from "./entity_decode";
import { buildServiceMetadata } from "./metadata";
import type { SerializedSession, SessionRegistryEntry } from "./persistence";
import { DiscoveryList } from "./resource_list";
import { HttpSession } from "./session";
import { type EntityKey, type SerializedShortIdTable, ShortIdTable } from "./shortid_table";
import { HttpEntityRefStub, HttpServiceRefStub } from "./stubs";
import { type DropHandler, type NotifyHandler, type WebSocketCtor, WsPool } from "./ws_pool";

type JotaiStore = ReturnType<typeof getDefaultStore>;

/** A discovery entry for a named entity. */
export interface HttpEntityRecord {
	readonly name: string;
	readonly idString: string;
	readonly ref: EntityRefStub;
}

/** A discovery entry for a named service. */
export interface HttpServiceRecord {
	readonly name: string;
	readonly descriptor: ServiceDescriptor;
	readonly ref: ServiceRefStub;
	readonly typeIdString: string | undefined;
}

/**
 * Decode a discovery entity id value: a bare data id_string, or a layered
 * `[data, prov]` pair (both bare local id_strings). Strict shape —
 * anything else throws.
 */
function decodeEntityIdValue(value: string | readonly string[]): {
	dataIdString: string;
	provIdString: string | null;
} {
	if (typeof value === "string") return { dataIdString: value, provIdString: null };
	if (
		Array.isArray(value) &&
		value.length === 2 &&
		typeof value[0] === "string" &&
		typeof value[1] === "string"
	) {
		return { dataIdString: value[0], provIdString: value[1] };
	}
	throw new Error(
		`discovery: malformed entity id (expected a string or [data, prov]): ${JSON.stringify(value)}`,
	);
}

/** The `/discovery` payload shape (`service_registry.discovery`). */
interface DiscoveryPayload {
	/**
	 * Named entities. Each value is a bare data id_string, or a layered
	 * `[data, prov]` pair.
	 */
	readonly entities?: Record<string, string | readonly string[]>;
	readonly services?: Record<string, WireValue>;
}

export interface HttpConnectorOptions {
	readonly baseUrl: string;
	/** Persistence backend (the standard Web Storage interface). */
	readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
	/** Override the fetch fn (tests / Node). Defaults to the global `fetch`. */
	readonly fetchFn?: FetchFn;
	/** Override the WebSocket ctor (tests). Defaults to the global `WebSocket`. */
	readonly webSocketCtor?: WebSocketCtor;
	/** Override the jotai store (defaults to a fresh `createStore()`). */
	readonly store?: JotaiStore;
	/** Storage-key namespace; defaults to a hash of `baseUrl`. */
	readonly namespace?: string;
}

/** Connection context for binding a transient service to its ws epoch. */
export interface TransientContext {
	readonly url: string;
	readonly generation: number;
}

/** Deterministic short hash of a string (for the storage-key namespace). */
function nsHash(s: string): string {
	let h = 0x811c9dc5;
	for (let i = 0; i < s.length; i++) {
		h ^= s.charCodeAt(i);
		h = Math.imul(h, 0x01000193);
	}
	return (h >>> 0).toString(16);
}

export class HttpConnector implements Connector {
	readonly store: JotaiStore;
	readonly url: string;

	private readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
	private readonly tableKey: string;
	private readonly sessionsKey: string;
	private readonly sessionKeyPrefix: string;
	private readonly table: ShortIdTable;
	private readonly fetchFn: FetchFn;
	private readonly fetcher: EntityFetcher;
	private readonly pool: WsPool;

	private readonly entityStubs = new Map<string, HttpEntityRefStub>();
	private readonly serviceStubs = new Map<string, HttpServiceRefStub>();
	private readonly brokenServicesAtom = atom<ReadonlySet<string>>(new Set<string>());
	private brokenServiceSet = new Set<string>();
	/** transient service shortId → the (url, generation) it arrived on. */
	private readonly transientBindings = new Map<string, TransientContext>();

	/** inbox uuid → deliver(payload). Sessions register their inboxes here. */
	private readonly inboxRoutes = new Map<string, (payload: WireValue) => void>();
	/** Listeners notified when a ws connection's generation drops. */
	private readonly dropListeners = new Set<DropHandler>();

	private readonly entityList: DiscoveryList<HttpEntityRecord>;
	private readonly serviceList: DiscoveryList<HttpServiceRecord>;

	// Tracks **discovery reachability** of `baseUrl`: `connecting` until
	// the first successful `/discovery`, `open` once it lands, back to
	// `reconnecting` if a later `refresh()` fails (no auto-retry — it
	// stays there until the next `refresh()` succeeds), `closed` on
	// `close()`. Deliberately ignores the ws pool: ws connections are
	// connect-on-demand and a transient drop self-heals on the next
	// invoke, surfacing through the per-service broken status instead.
	private readonly connectionStatusAtom = atom<ConnectionStatus>("connecting");
	/** Live session instances, interned one-per-sessionId (Q-B). */
	private readonly sessions = new Map<string, HttpSession>();
	/** Persisted session registry (sessionId → metadata/createdAt), loaded at construction. */
	private readonly registry = new Map<string, SessionRegistryEntry>();
	private closed = false;
	private persistScheduled = false;

	constructor(opts: HttpConnectorOptions) {
		this.url = opts.baseUrl;
		this.store = opts.store ?? createStore();
		this.storage = opts.storage;
		const ns = opts.namespace ?? nsHash(opts.baseUrl);
		this.tableKey = `fxtrhttp:${ns}:table`;
		this.sessionsKey = `fxtrhttp:${ns}:sessions`;
		this.sessionKeyPrefix = `fxtrhttp:${ns}:session:`;

		this.table = new ShortIdTable(opts.baseUrl, this.loadTable());
		for (const entry of this.loadRegistry()) this.registry.set(entry.sessionId, entry);
		this.fetchFn = opts.fetchFn ?? globalThis.fetch.bind(globalThis);
		this.fetcher = new EntityFetcher(opts.baseUrl, this.fetchFn);

		const onNotify: NotifyHandler = (inbox, payload) => {
			this.inboxRoutes.get(inbox)?.(payload);
		};
		const onDrop: DropHandler = (url, generation) => this.handleDrop(url, generation);
		this.pool = new WsPool({
			onNotify,
			onDrop,
			...(opts.webSocketCtor ? { webSocketCtor: opts.webSocketCtor } : {}),
		});

		this.entityList = new DiscoveryList<HttpEntityRecord>(this.store);
		this.serviceList = new DiscoveryList<HttpServiceRecord>(this.store);
		// Fetch discovery once at construction (fire-and-forget; the list
		// surfaces `loading → ready`, and failures leave it `loading` with
		// a logged error).
		void this.refresh();
	}

	// -- identity / interning ----------------------------------------

	/** Intern the entity-ref stub for a shortId (idempotent). */
	entityRef<T = unknown>(shortId: string): EntityRefStub<T> {
		const existing = this.entityStubs.get(shortId);
		if (existing) return existing as unknown as EntityRefStub<T>;
		const stub = new HttpEntityRefStub<T>(shortId);
		this.entityStubs.set(shortId, stub as unknown as HttpEntityRefStub);
		return stub;
	}

	/** Intern the service-ref stub for a shortId (idempotent). */
	serviceRef<T = unknown>(shortId: string): ServiceRefStub<T> {
		const existing = this.serviceStubs.get(shortId);
		if (existing) return existing as unknown as ServiceRefStub<T>;
		const stub = new HttpServiceRefStub<T>(shortId, this.brokenServicesAtom);
		this.serviceStubs.set(shortId, stub as unknown as HttpServiceRefStub);
		return stub;
	}

	/**
	 * Intern (allocating a shortId) the entity-ref for a `(data, prov)`
	 * key. `provIdString` (the bare provenance-node id_string) makes it a
	 * layered ref; omit it for an unprovenanced entity. The shortId carries
	 * the layered identity through the table (so persisted state + metadata
	 * round-trip it without the wire form).
	 */
	internEntity(dataIdString: string, provIdString?: string | null): EntityRefStub {
		const shortId = this.table.shortIdForEntity(dataIdString, provIdString ?? null);
		this.schedulePersist();
		return this.entityRef(shortId);
	}

	/** Intern the entity-ref for a bare data `id_string` (no provenance). */
	internEntityByIdString(idString: string): EntityRefStub {
		return this.internEntity(idString);
	}

	/** The `(data, prov)` key a (known) entity shortId stands for. */
	entityKeyFor(shortId: string): EntityKey {
		const key = this.table.entityKeyFor(shortId);
		if (key === undefined) throw new Error(`shortId ${shortId} is not a known entity`);
		return key;
	}

	/**
	 * Intern (allocating a shortId) the service-ref for a descriptor. For
	 * a transient descriptor with a `ctx`, record the (url, generation)
	 * it arrived on so the stub can be marked broken when that ws epoch
	 * dies.
	 */
	internServiceByDescriptor(descriptor: ServiceDescriptor, ctx?: TransientContext): ServiceRefStub {
		const shortId = this.table.shortIdForService(descriptor);
		this.schedulePersist();
		if (descriptor.kind === "transient" && ctx) this.transientBindings.set(shortId, ctx);
		return this.serviceRef(shortId);
	}

	/** Classify a shortId as naming an entity or a service (or `null`). */
	classifyShortId(shortId: string): "entity" | "service" | null {
		return this.table.classifyShortId(shortId);
	}

	/** The data `id_string` a (known) entity shortId stands for. */
	entityIdStringFor(shortId: string): string {
		const entry = this.table.lookup(shortId);
		if (entry?.kind !== "entity") {
			throw new Error(`shortId ${shortId} is not a known entity`);
		}
		return entry.idString;
	}

	/** The descriptor a (known) service shortId stands for. */
	serviceDescriptorFor(shortId: string): ServiceDescriptor {
		const entry = this.table.lookup(shortId);
		if (entry?.kind !== "service") {
			throw new Error(`shortId ${shortId} is not a known service`);
		}
		return entry.descriptor;
	}

	/** The (url, generation) a transient service shortId is bound to, if any. */
	transientBindingFor(shortId: string): TransientContext | undefined {
		return this.transientBindings.get(shortId);
	}

	// -- entity data + metadata --------------------------------------

	/**
	 * Load + decode an entity's data into a `ResultValue`.
	 *
	 * For a **bare** ref this fetches `/data/{data}` and interns each child
	 * ref bare. For a **layered** ref `(data, prov)` it mirrors the Python
	 * scope's `_load_arena_cbor`: load the **provenance form** (the `prov`
	 * node — an `n`-node whose CBOR is the full content with provenance-level
	 * child refs), then rewrap each child to its layered form
	 * `(unwrap(child_prov), child_prov)` using the `unwraps_to` precomp the
	 * same `/data/{prov}` response bundles. So **children of a layered entity
	 * always load layered**, and provenance propagates through every
	 * subsequent load (each layered child fetches its own prov form in turn).
	 *
	 * **Trivial (`u`-node) provenance is special-cased**: a `u`-node is
	 * synthetic (it just unwraps to its data child) and is never a stored
	 * entity, so it can't be fetched. For a `(data, u(data))` ref — the form
	 * `build_*_report` mints for bare inputs — we load the **data** node and
	 * re-wrap children with their own `u`-node provenance instead.
	 */
	async loadEntityData<T = unknown>(stub: EntityRefStub<T>): Promise<T> {
		const key = this.entityKeyFor(stub.shortId);
		if (key.provIdString === null) {
			const tree = await this.fetcher.load(key.dataIdString);
			const decoded = cborToResultValue(tree, {
				entityRefForIdString: (id) => this.internEntityByIdString(id),
			});
			return decoded as T;
		}
		// Trivial (u-node) provenance: the prov node is **synthetic** — a
		// `u`-node just "unwraps to" its data child, so the server has no bytes
		// for it (loading a u-node in-process wraps the data's children as
		// u-nodes; it is never fetched). Load the **data** node directly and
		// re-attach the trivial u provenance to each child, so children stay
		// layered without ever fetching a u-node (each child's own load lands
		// here again). Mirrors the Python u-node load; matches the `(data,
		// u(data))` form `build_*_report` mints for bare inputs.
		if (key.provIdString.startsWith("u")) {
			const tree = await this.fetcher.load(key.dataIdString);
			const decoded = cborToResultValue(tree, {
				entityRefForIdString: (childData) =>
					this.internEntity(childData, uNodeIdStringFor(childData)),
			});
			return decoded as T;
		}
		// Layered: load the prov form; its children are prov nodes.
		const tree = await this.fetcher.load(key.provIdString);
		const decoded = cborToResultValue(tree, {
			entityRefForIdString: (childProvId) => {
				const childData = this.fetcher.precompFor(childProvId)?.unwraps_to;
				if (childData === undefined) {
					// Every child of a prov form is itself a prov node that
					// unwraps (the store u-wraps even bare children), and the
					// /data/{prov} precomp always bundles that edge — a missing
					// one is a server/precomp bug, not a bare-child fallback.
					throw new EntityLoadError(
						`loadEntityData: prov-form child ${childProvId} has no unwraps_to in the precomp closure`,
					);
				}
				return this.internEntity(childData, childProvId);
			},
		});
		return decoded as T;
	}

	/**
	 * Resolve an entity's metadata locally (design/03), reproducing the
	 * workstation's `GetEntityMetadataOp` field-for-field
	 * (`state.py:_compute_entity_metadata`) so dispatch / ref-links /
	 * metadata panels behave identically across connectors:
	 *
	 * - `id_string` — the data-half local id_string.
	 * - `unprovenanced` — ref of `(data, null)`, always present.
	 * - `provenance_parent` (+ `_id_string`) — ref of `(prov, null)`,
	 *   present iff the ref is layered. Sourced from the shortId table
	 *   (which carries the prov half), not the bare `/data` precomp.
	 * - `unwraps_to` / `context_data` / `backstep` — only when the **data
	 *   half's tag** is a provenance node (`c`/`n`/`p`/`u`), derived from the
	 *   `/data` precomp of the data id. `unwraps_to` is the **layered**
	 *   `(unwrapped, data)` form (matching the workstation's
	 *   `embed_as_provenance_parent`); `context_data` / `backstep` are bare.
	 */
	async getEntityMetadata(stub: EntityRefStub): Promise<EntityMetadata> {
		const key = this.entityKeyFor(stub.shortId);
		const dataIdString = key.dataIdString;
		const meta: {
			id_string: string;
			unprovenanced?: EntityRefStub;
			provenance_parent?: EntityRefStub;
			provenance_parent_id_string?: string;
			unwraps_to?: EntityRefStub;
			unwraps_to_id_string?: string;
			context_data?: EntityRefStub;
			backstep?: EntityRefStub;
		} = { id_string: dataIdString };

		// The entity's own (data, prov) layering — from the table.
		meta.unprovenanced = this.internEntity(dataIdString);
		if (key.provIdString !== null) {
			meta.provenance_parent = this.internEntity(key.provIdString);
			meta.provenance_parent_id_string = key.provIdString;
		}

		// unwraps_to / context_data / backstep apply only when the data half
		// is itself a provenance node; derived from its /data precomp.
		const dataTag = dataIdString[0];
		if (dataTag === "c" || dataTag === "n" || dataTag === "p" || dataTag === "u") {
			if (this.fetcher.precompFor(dataIdString) === undefined) {
				await this.fetcher.load(dataIdString).catch(() => undefined);
			}
			const precomp = this.fetcher.precompFor(dataIdString);
			if (precomp?.unwraps_to) {
				// Layered (unwrapped, data): the data node is the prov parent.
				meta.unwraps_to = this.internEntity(precomp.unwraps_to, dataIdString);
				meta.unwraps_to_id_string = precomp.unwraps_to;
			}
			if (precomp?.ctx_node) meta.context_data = this.internEntity(precomp.ctx_node);
			if (precomp?.backstep) meta.backstep = this.internEntity(precomp.backstep);
		}
		return meta;
	}

	/** Resolve a service's metadata locally from its descriptor. */
	getServiceMetadata(stub: ServiceRefStub): ServiceMetadata {
		return buildServiceMetadata(this.serviceDescriptorFor(stub.shortId));
	}

	/**
	 * Invoke an http service over a one-shot fetch. `GET` encodes
	 * `?method&payload=<urlencoded JSON {args,kwargs}>`; `POST` sends a
	 * JSON body. Returns the resolved-mode result `WireValue`; throws on
	 * a non-2xx response (the body's `{message}` if present).
	 */
	async invokeHttpService(
		descriptor: ServiceDescriptor,
		method: string,
		args: readonly WireValue[],
		kwargs: { readonly [key: string]: WireValue },
	): Promise<WireValue> {
		const base = this.url.endsWith("/") ? this.url.slice(0, -1) : this.url;
		const path = descriptor.url ?? "";
		const abs = path.startsWith("http")
			? path
			: `${base}${path.startsWith("/") ? path : `/${path}`}`;
		let resp: Response;
		if ((descriptor.method ?? "POST").toUpperCase() === "GET") {
			const payload = encodeURIComponent(JSON.stringify({ args, kwargs }));
			resp = await this.fetchFn(`${abs}?method=${encodeURIComponent(method)}&payload=${payload}`);
		} else {
			resp = await this.fetchFn(abs, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ method, args, kwargs }),
			});
		}
		if (!resp.ok) {
			let message = `HTTP ${resp.status}`;
			try {
				const body = (await resp.json()) as { message?: unknown };
				if (typeof body.message === "string") message = body.message;
			} catch {
				/* non-JSON error body — keep the status message */
			}
			throw new Error(message);
		}
		return (await resp.json()) as WireValue;
	}

	// -- discovery ----------------------------------------------------

	listEntities(): DiscoveryList<HttpEntityRecord> {
		return this.entityList;
	}
	listServices(): DiscoveryList<HttpServiceRecord> {
		return this.serviceList;
	}

	/** (Re)fetch `/discovery` and republish the lists. */
	async refresh(): Promise<void> {
		if (this.closed) return;
		this.entityList.setLoading();
		this.serviceList.setLoading();
		let payload: DiscoveryPayload;
		try {
			payload = await this.fetchDiscovery();
		} catch (e) {
			console.error(`HttpConnector: /discovery fetch failed: ${String(e)}`);
			if (this.closed) return;
			// Surface the failure: lists carry an `error` status (records keep
			// their last value), and the connector's discovery reachability
			// drops from `open`/`connecting` to `reconnecting` (no auto-retry —
			// the next `refresh()` recovers it).
			this.entityList.setError();
			this.serviceList.setError();
			this.store.set(this.connectionStatusAtom, "reconnecting");
			return;
		}
		if (this.closed) return;
		const entities: HttpEntityRecord[] = Object.entries(payload.entities ?? {}).map(
			([name, value]) => {
				const { dataIdString, provIdString } = decodeEntityIdValue(value);
				return { name, idString: dataIdString, ref: this.internEntity(dataIdString, provIdString) };
			},
		);
		const services: HttpServiceRecord[] = Object.entries(payload.services ?? {}).map(
			([name, wire]) => {
				const descriptor = descriptorFromWire(wire);
				return {
					name,
					descriptor,
					ref: this.internServiceByDescriptor(descriptor),
					typeIdString: descriptor.service_type_id,
				};
			},
		);
		this.entityList.publish(entities);
		this.serviceList.publish(services);
		this.store.set(this.connectionStatusAtom, "open");
	}

	// -- connection status / lifecycle -------------------------------

	get connectionStatus(): Atom<ConnectionStatus> {
		return this.connectionStatusAtom;
	}

	get brokenServices(): Atom<ReadonlySet<string>> {
		return this.brokenServicesAtom;
	}

	// -- sessions -----------------------------------------------------

	/**
	 * Create a fresh client-local session (a client-minted UUID; no
	 * server round trip). `metadata` is persisted with the session
	 * (registry + blob) so a later reload can `attachSession` to it. Kept
	 * `Promise`-returning to match the abstract `Connector` surface.
	 */
	createSession(metadata: JsonValue = {}): Promise<Session> {
		const sessionId = crypto.randomUUID();
		const createdAt = Date.now();
		this.registry.set(sessionId, { sessionId, metadata, createdAt });
		this.persistRegistry();
		const session = new HttpSession({ sessionId, connector: this, metadata, createdAt });
		this.sessions.set(sessionId, session);
		// Persist the initial blob so an attach after reload finds it.
		this.persistSession(sessionId, session.serialize());
		return Promise.resolve(session);
	}

	/**
	 * Attach to a session by id. **Interns one live instance per
	 * sessionId** (Q-B; a deliberate deviation from the workstation
	 * connector, since two live instances would race on the same storage
	 * key). Rehydrates from the persisted blob if present.
	 *
	 * We only ever return a live session for an id we can **actually
	 * restore**. Anything else — never persisted, an unparseable blob, a
	 * blob we can't decode (bad version / corrupt payload) — yields a
	 * terminal `unavailable` session, *not* a silently-created one. This
	 * mirrors the workstation connector, where attaching to a session the
	 * server doesn't know lands `unavailable` (so the panel wrapper can
	 * render a "session no longer available" body). To create a session,
	 * use `createSession`. A dead session touches no storage and is not
	 * interned (a later `createSession` mints a fresh id anyway, and a
	 * real attach can't collide with it).
	 */
	attachSession(sessionId: string): Session {
		const existing = this.sessions.get(sessionId);
		if (existing) return existing;
		const blob = this.loadSessionBlob(sessionId); // undefined if absent / unparseable
		if (blob === undefined) {
			return new HttpSession({ sessionId, connector: this, unavailable: true });
		}
		const session = new HttpSession({
			sessionId,
			connector: this,
			metadata: blob.metadata,
			createdAt: blob.createdAt,
			restore: blob,
		});
		// A blob that failed to restore (bad version / corrupt payload)
		// lands `unavailable` in the ctor — don't intern a dead session.
		if (this.store.get(session.lifecycle) !== "unavailable") {
			this.sessions.set(sessionId, session);
		}
		return session;
	}

	/** The persisted session registry (for layout rehydration / debug surfaces). */
	listPersistedSessions(): SessionRegistryEntry[] {
		return [...this.registry.values()];
	}

	/** Drop a session's live instance (detach) — storage is retained. */
	removeSession(sessionId: string): void {
		this.sessions.delete(sessionId);
	}

	/** Persist a session's blob through the storage handle (called by the session). */
	persistSession(sessionId: string, blob: SerializedSession): void {
		if (this.closed) return;
		try {
			this.storage.setItem(this.sessionKey(sessionId), JSON.stringify(blob));
		} catch (e) {
			console.error(`HttpConnector: failed to persist session ${sessionId}: ${String(e)}`);
		}
	}

	/** Delete a session's blob + registry entry (terminate). Idempotent. */
	deleteSession(sessionId: string): void {
		this.sessions.delete(sessionId);
		this.registry.delete(sessionId);
		this.persistRegistry();
		try {
			this.storage.removeItem(this.sessionKey(sessionId));
		} catch (e) {
			console.error(`HttpConnector: failed to delete session ${sessionId}: ${String(e)}`);
		}
	}

	/** Close the connector: ws pool, lists, status. Idempotent. */
	close(): void {
		if (this.closed) return;
		// Flush each live session's latest state synchronously (a debounced
		// persist may still be pending) *before* marking closed, since
		// `persistSession` bails once closed.
		for (const session of this.sessions.values()) {
			try {
				this.persistSession(session.sessionId, session.serialize());
			} catch (e) {
				console.error(`HttpConnector: failed to flush session ${session.sessionId}: ${String(e)}`);
			}
		}
		this.closed = true;
		for (const session of [...this.sessions.values()]) void session.detach();
		this.pool.close();
		this.entityList.close();
		this.serviceList.close();
		this.store.set(this.connectionStatusAtom, "closed");
		this.flushPersist();
	}

	// -- session-facing transport hooks (used by session.ts) ---------

	/** The ws pool (sessions invoke ws services through it). */
	get wsPool(): WsPool {
		return this.pool;
	}

	/** Register an inbox uuid → delivery handler (notify routing). */
	registerInbox(uuid: string, deliver: (payload: WireValue) => void): void {
		this.inboxRoutes.set(uuid, deliver);
	}
	unregisterInbox(uuid: string): void {
		this.inboxRoutes.delete(uuid);
	}

	/** Subscribe to ws connection drops (for inbox `connectionState`). */
	onConnectionDrop(listener: DropHandler): () => void {
		this.dropListeners.add(listener);
		return () => this.dropListeners.delete(listener);
	}

	/** Mark a service shortId broken (coalesced into `brokenServices`). */
	markServiceBroken(shortId: string): void {
		if (this.brokenServiceSet.has(shortId)) return;
		this.brokenServiceSet = new Set(this.brokenServiceSet).add(shortId);
		this.store.set(this.brokenServicesAtom, this.brokenServiceSet);
	}

	// -- internals ----------------------------------------------------

	private handleDrop(url: string, generation: number): void {
		// Break every transient stub minted on the dead (url, generation).
		for (const [shortId, ctx] of this.transientBindings) {
			if (ctx.url === url && ctx.generation === generation) this.markServiceBroken(shortId);
		}
		for (const listener of [...this.dropListeners]) listener(url, generation);
	}

	private async fetchDiscovery(): Promise<DiscoveryPayload> {
		const base = this.url.endsWith("/") ? this.url.slice(0, -1) : this.url;
		const resp = await this.fetchFn(`${base}/discovery`);
		if (!resp.ok) throw new Error(`GET /discovery failed: HTTP ${resp.status}`);
		return (await resp.json()) as DiscoveryPayload;
	}

	private loadTable(): SerializedShortIdTable | undefined {
		const raw = this.storage.getItem(this.tableKey);
		if (raw === null) return undefined;
		try {
			return JSON.parse(raw) as SerializedShortIdTable;
		} catch (e) {
			console.error(`HttpConnector: corrupt shortId table at ${this.tableKey}: ${String(e)}`);
			return undefined;
		}
	}

	private sessionKey(sessionId: string): string {
		return `${this.sessionKeyPrefix}${sessionId}`;
	}

	private loadRegistry(): SessionRegistryEntry[] {
		const raw = this.storage.getItem(this.sessionsKey);
		if (raw === null) return [];
		try {
			return JSON.parse(raw) as SessionRegistryEntry[];
		} catch (e) {
			console.error(`HttpConnector: corrupt session registry at ${this.sessionsKey}: ${String(e)}`);
			return [];
		}
	}

	private persistRegistry(): void {
		if (this.closed) return;
		try {
			this.storage.setItem(this.sessionsKey, JSON.stringify([...this.registry.values()]));
		} catch (e) {
			console.error(`HttpConnector: failed to persist session registry: ${String(e)}`);
		}
	}

	private loadSessionBlob(sessionId: string): SerializedSession | undefined {
		const raw = this.storage.getItem(this.sessionKey(sessionId));
		if (raw === null) return undefined;
		try {
			return JSON.parse(raw) as SerializedSession;
		} catch (e) {
			console.error(`HttpConnector: corrupt session blob for ${sessionId}: ${String(e)}`);
			return undefined;
		}
	}

	private schedulePersist(): void {
		if (this.persistScheduled || this.closed) return;
		this.persistScheduled = true;
		queueMicrotask(() => {
			this.persistScheduled = false;
			this.flushPersist();
		});
	}

	private flushPersist(): void {
		try {
			this.storage.setItem(this.tableKey, JSON.stringify(this.table.toJSON()));
		} catch (e) {
			console.error(`HttpConnector: failed to persist shortId table: ${String(e)}`);
		}
	}
}
