/**
 * HTTP client for consuming a fxtr HTTP API.
 *
 * Port of Python's client.py. Provides:
 * - HttpReadableArena: ReadableArena backed by fetch requests
 * - HttpDeepFetchable: DeepFetchable for deep-fetch integration
 * - HttpFxtrReadNamespace: Read-only namespace backed by HTTP
 * - createHttpReadNamespace: Create an HTTP namespace (non-callback lifecycle)
 * - withHttpReadNamespace: Callback-based lifecycle for HTTP namespaces
 * - withBaseUrl: Convenience helper for base-URL-aware fetch
 */

import { decode } from "cbor2";

import {
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	type FxtrCbor,
	FxtrID,
	type FxtrReadNamespace,
	type FxtrScope,
	RawStrongFxtrID,
	type RetrieveOpts,
	createFxtrScope,
	fxtrCborLoads,
	visitIds,
} from "@fxtr/core";
import {
	type DeepFetchable,
	DepKind,
	FXTR_SCOPE_INTERNALS,
	type FxtrEntityFlatData,
	type ReadableArena,
} from "@fxtr/core/internal";

// ---------------------------------------------------------------------------
// FetchFn type
// ---------------------------------------------------------------------------

/** A fetch-compatible function. Matches the standard fetch signature. */
export type FetchFn = (url: string, init?: RequestInit) => Promise<Response>;

// ---------------------------------------------------------------------------
// withBaseUrl helper
// ---------------------------------------------------------------------------

/**
 * Create a FetchFn that prepends a base URL to relative paths.
 *
 * @param baseUrl - Base URL to prepend (e.g., "http://localhost:8080")
 * @param fetchFn - Underlying fetch function. Defaults to globalThis.fetch.
 */
export function withBaseUrl(baseUrl: string, fetchFn?: FetchFn): FetchFn {
	const base = baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
	const fn = fetchFn ?? globalThis.fetch.bind(globalThis);
	return (url, init) => {
		const fullUrl =
			url.startsWith("http://") || url.startsWith("https://")
				? url
				: `${base}${url.startsWith("/") ? "" : "/"}${url}`;
		return fn(fullUrl, init);
	};
}

// ---------------------------------------------------------------------------
// HttpReadableArena
// ---------------------------------------------------------------------------

/**
 * A ReadableArena backed by HTTP requests to a fxtr server.
 *
 * Port of Python's HttpReadableArena. Uses precomp maps from server
 * responses for provenance operations (unwrap, ctx_node, backstep).
 */
export class HttpReadableArena implements ReadableArena {
	private readonly _fetchFn: FetchFn;
	/** Tracked node IDs (keyed by id.key()). */
	readonly _tracked = new Map<string, FxtrID>();
	private readonly _precompUnwrapsTo = new Map<string, FxtrID>();
	private readonly _precompCtxNode = new Map<string, FxtrID>();
	private readonly _precompStepBackPCtx = new Map<string, FxtrID>();

	constructor(fetchFn: FetchFn) {
		this._fetchFn = fetchFn;
	}

	/** Populate precomp dicts from a server response's precomp map. */
	_ingestPrecomp(precomp: Record<string, Record<string, string>>): void {
		for (const [idStr, entry] of Object.entries(precomp)) {
			const nodeId = FxtrID.fromIdString(idStr);
			this._tracked.set(nodeId.key(), nodeId);

			if (entry.unwraps_to) {
				const target = FxtrID.fromIdString(entry.unwraps_to);
				this._precompUnwrapsTo.set(nodeId.key(), target);
				this._tracked.set(target.key(), target);
			}

			if (entry.ctx_node) {
				const target = FxtrID.fromIdString(entry.ctx_node);
				this._precompCtxNode.set(nodeId.key(), target);
				this._tracked.set(target.key(), target);
			}

			if (entry.backstep) {
				const target = FxtrID.fromIdString(entry.backstep);
				this._precompStepBackPCtx.set(nodeId.key(), target);
				this._tracked.set(target.key(), target);
			}
		}
	}

	private async _fetchData(nodeId: FxtrID): Promise<Record<string, unknown>> {
		const resp = await this._fetchFn(`/data/${nodeId.idString()}`);
		if (resp.status === 404) {
			throw new Error(`Entity ${nodeId.idString()} not found on server`);
		}
		if (resp.status === 503) {
			throw new Error("Server scope is closed");
		}
		if (!resp.ok) {
			throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
		}
		const buf = await resp.arrayBuffer();
		return decode(new Uint8Array(buf)) as Record<string, unknown>;
	}

	// -- ReadableArena implementation --

	isTracked(nodeId: FxtrID): boolean {
		return this._tracked.has(nodeId.key());
	}

	async load(nodeId: FxtrID): Promise<FxtrCbor> {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}

		const result = await this._fetchData(nodeId);

		if (result.precomp) {
			this._ingestPrecomp(result.precomp as Record<string, Record<string, string>>);
		}

		const entityBytes = result.bytes as Uint8Array;
		const decoded = fxtrCborLoads(entityBytes);

		// Walk CBOR to discover and track child IDs
		visitIds(decoded, (rawId) => {
			this._tracked.set(rawId.innerId.key(), rawId.innerId);
		});

		return decoded;
	}

	async loadFlat(nodeId: FxtrID): Promise<FxtrEntityFlatData> {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}

		const result = await this._fetchData(nodeId);

		if (result.precomp) {
			this._ingestPrecomp(result.precomp as Record<string, Record<string, string>>);
		}

		const entityBytes = result.bytes as Uint8Array;
		const decoded = fxtrCborLoads(entityBytes);
		const deps: Array<readonly [FxtrID, DepKind]> = [];

		visitIds(decoded, (rawId) => {
			this._tracked.set(rawId.innerId.key(), rawId.innerId);
			if (rawId instanceof RawStrongFxtrID) {
				deps.push([rawId.innerId, DepKind.STRONG_REF]);
			} else {
				// RawWeakFxtrID or RawFxtrEntityMarkerID
				deps.push([rawId.innerId, DepKind.WEAK_REF]);
			}
		});

		return {
			entityId: nodeId,
			entityBytes,
			deps,
		};
	}

	unwrapProvenance(nodeId: FxtrID): FxtrID | null {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}
		const result = this._precompUnwrapsTo.get(nodeId.key()) ?? null;
		if (result !== null) {
			this._tracked.set(result.key(), result);
		}
		return result;
	}

	cNodeToContextData(nodeId: FxtrID): FxtrID {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}
		const result = this._precompCtxNode.get(nodeId.key());
		if (result === undefined) {
			throw new Error(`No ctx_node precomp for ${nodeId.idString()}`);
		}
		this._tracked.set(result.key(), result);
		return result;
	}

	pNodeToPathroot(nodeId: FxtrID): FxtrID {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}
		const result = this._precompCtxNode.get(nodeId.key());
		if (result === undefined) {
			throw new Error(`No ctx_node precomp for ${nodeId.idString()}`);
		}
		this._tracked.set(result.key(), result);
		return result;
	}

	stepBackPCtx(nodeId: FxtrID): FxtrID | null {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}
		const result = this._precompStepBackPCtx.get(nodeId.key()) ?? null;
		if (result !== null) {
			this._tracked.set(result.key(), result);
		}
		return result;
	}

	abandon(nodeId: FxtrID): void {
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
		}
		this._tracked.delete(nodeId.key());
		this._precompUnwrapsTo.delete(nodeId.key());
		this._precompCtxNode.delete(nodeId.key());
		this._precompStepBackPCtx.delete(nodeId.key());
	}

	copyToNewArena(nodeIds: Iterable<FxtrID>): HttpReadableArena {
		const nodeIdArray = [...nodeIds];
		for (const nodeId of nodeIdArray) {
			if (!this.isTracked(nodeId)) {
				throw new Error(`Node ${nodeId.idString()} is not tracked in this arena`);
			}
		}

		const newArena = new HttpReadableArena(this._fetchFn);

		// Compute transitive closure of precomp data
		const toCopy = new Set<string>();
		const frontier: FxtrID[] = [...nodeIdArray];
		while (frontier.length > 0) {
			// biome-ignore lint/style/noNonNullAssertion: length check above guarantees non-empty
			const nid = frontier.shift()!;
			const nk = nid.key();
			if (toCopy.has(nk)) continue;
			toCopy.add(nk);

			for (const precompMap of [
				this._precompUnwrapsTo,
				this._precompCtxNode,
				this._precompStepBackPCtx,
			]) {
				const target = precompMap.get(nk);
				if (target !== undefined && !toCopy.has(target.key())) {
					frontier.push(target);
				}
			}
		}

		// Copy precomp data
		for (const nk of toCopy) {
			const nid = this._tracked.get(nk);
			if (nid) newArena._tracked.set(nk, nid);

			const unwraps = this._precompUnwrapsTo.get(nk);
			if (unwraps) newArena._precompUnwrapsTo.set(nk, unwraps);

			const ctx = this._precompCtxNode.get(nk);
			if (ctx) newArena._precompCtxNode.set(nk, ctx);

			const backstep = this._precompStepBackPCtx.get(nk);
			if (backstep) newArena._precompStepBackPCtx.set(nk, backstep);
		}

		return newArena;
	}

	close(): void {
		this._tracked.clear();
		this._precompUnwrapsTo.clear();
		this._precompCtxNode.clear();
		this._precompStepBackPCtx.clear();
	}
}

// ---------------------------------------------------------------------------
// HttpDeepFetchable
// ---------------------------------------------------------------------------

/**
 * DeepFetchable backed by HTTP, for deep-fetch integration.
 *
 * Port of Python's HttpObservableStore (minus backrefs).
 * tryLookup does a GET to /data/ so the returned arena has precomp populated.
 */
export class HttpDeepFetchable implements DeepFetchable {
	private readonly _fetchFn: FetchFn;

	constructor(fetchFn: FetchFn) {
		this._fetchFn = fetchFn;
	}

	async tryLookup(nodeId: FxtrID): Promise<ReadableArena | null> {
		const resp = await this._fetchFn(`/data/${nodeId.idString()}`);
		if (resp.status === 404 || !resp.ok) {
			return null;
		}

		const arena = new HttpReadableArena(this._fetchFn);
		arena._tracked.set(nodeId.key(), nodeId);

		const buf = await resp.arrayBuffer();
		const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
		if (result.precomp) {
			arena._ingestPrecomp(result.precomp as Record<string, Record<string, string>>);
		}

		return arena;
	}
}

// ---------------------------------------------------------------------------
// HttpFxtrReadNamespace
// ---------------------------------------------------------------------------

/**
 * Read-only fxtr namespace backed by HTTP.
 *
 * Port of Python's HttpFxtrReadNamespace. Uses withFxtrScope internally
 * for scope lifecycle management.
 */
export class HttpFxtrReadNamespace implements FxtrReadNamespace {
	readonly scope: FxtrScope;
	private readonly _fetchFn: FetchFn;

	constructor(scope: FxtrScope, fetchFn: FetchFn) {
		this.scope = scope;
		this._fetchFn = fetchFn;
	}

	// Overloaded retrieve
	async retrieve(key: string, opts?: RetrieveOpts): Promise<FxtrID>;
	async retrieve<T extends Entity>(
		key: string,
		loadCodec: EntityLoadCodec<T>,
		opts?: RetrieveOpts,
	): Promise<EntityRef<T>>;
	async retrieve<T extends Entity>(
		key: string,
		loadCodecOrOpts?: EntityLoadCodec<T> | RetrieveOpts,
		maybeOpts?: RetrieveOpts,
	): Promise<FxtrID | EntityRef<T>> {
		const { loadCodec, opts } = this._parseRetrieveArgs(loadCodecOrOpts, maybeOpts);
		const result = await this._retrieveImpl(key, loadCodec, opts, false);
		return result as FxtrID | EntityRef<T>;
	}

	// Overloaded tryRetrieve
	async tryRetrieve(key: string, opts?: RetrieveOpts): Promise<FxtrID | null>;
	async tryRetrieve<T extends Entity>(
		key: string,
		loadCodec: EntityLoadCodec<T>,
		opts?: RetrieveOpts,
	): Promise<EntityRef<T> | null>;
	async tryRetrieve<T extends Entity>(
		key: string,
		loadCodecOrOpts?: EntityLoadCodec<T> | RetrieveOpts,
		maybeOpts?: RetrieveOpts,
	): Promise<FxtrID | EntityRef<T> | null> {
		const { loadCodec, opts } = this._parseRetrieveArgs(loadCodecOrOpts, maybeOpts);
		return this._retrieveImpl(key, loadCodec, opts, true);
	}

	async listPins(prefix?: string): Promise<ReadonlyMap<string, FxtrID>> {
		const params = prefix ? `?prefix=${encodeURIComponent(prefix)}` : "";
		const resp = await this._fetchFn(`/pins${params}`);
		if (!resp.ok) {
			throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
		}
		const data = (await resp.json()) as Record<string, string>;
		const result = new Map<string, FxtrID>();
		for (const [name, idStr] of Object.entries(data)) {
			result.set(name, FxtrID.fromIdString(idStr));
		}
		return result;
	}

	/**
	 * Fetch an entity by its FxtrID directly (via /data/ endpoint) and
	 * attach it into the namespace scope. Returns the ID if found, null otherwise.
	 *
	 * Use this to ensure an entity is loaded into the scope when you have
	 * its ID but not a pin name (e.g. when restoring from a URL).
	 */
	async tryRetrieveById(entityId: FxtrID): Promise<FxtrID | null> {
		const deepFetchable = new HttpDeepFetchable(this._fetchFn);
		const arena = await deepFetchable.tryLookup(entityId);
		if (!arena) return null;
		this.scope[FXTR_SCOPE_INTERNALS].unsafeAttachFromExternalArena(entityId, arena);
		arena.close();
		return entityId;
	}

	// -- Internals --

	private _parseRetrieveArgs<T extends Entity>(
		loadCodecOrOpts: EntityLoadCodec<T> | RetrieveOpts | undefined,
		maybeOpts: RetrieveOpts | undefined,
	): { loadCodec: EntityLoadCodec<T> | null; opts: RetrieveOpts } {
		if (loadCodecOrOpts === undefined) {
			return { loadCodec: null, opts: {} };
		}
		// Distinguish EntityLoadCodec from RetrieveOpts by checking for
		// the supportsEntityType method
		if ("supportsEntityType" in loadCodecOrOpts) {
			return {
				loadCodec: loadCodecOrOpts as EntityLoadCodec<T>,
				opts: maybeOpts ?? {},
			};
		}
		return { loadCodec: null, opts: loadCodecOrOpts as RetrieveOpts };
	}

	private async _retrieveImpl<T extends Entity>(
		key: string,
		loadCodec: EntityLoadCodec<T> | null,
		opts: RetrieveOpts,
		allowNotFound: boolean,
	): Promise<FxtrID | EntityRef<T> | null> {
		const resp = await this._fetchFn(`/pin/${key}`);
		if (resp.status === 404) {
			if (allowNotFound) return null;
			throw new Error(`Pin '${key}' not found`);
		}
		if (!resp.ok) {
			throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
		}

		const buf = await resp.arrayBuffer();
		const result = decode(new Uint8Array(buf)) as Record<string, unknown>;
		const fxtrId = FxtrID.fromIdString(result.id as string);

		// Create a per-call arena, populate precomp, attach to scope, then close.
		// unsafeLazilyAttachFromArena copies the arena via copyToNewArena internally,
		// so the original can be closed immediately after attaching.
		const arena = new HttpReadableArena(this._fetchFn);
		arena._tracked.set(fxtrId.key(), fxtrId);
		if (result.precomp) {
			arena._ingestPrecomp(result.precomp as Record<string, Record<string, string>>);
		}
		this.scope[FXTR_SCOPE_INTERNALS].unsafeAttachFromExternalArena(fxtrId, arena);
		arena.close();

		// Handle intoScope
		const intoScope = opts.intoScope;
		if (intoScope !== undefined) {
			if (opts.copyIntoScope) {
				await intoScope.copyFromScope(fxtrId, this.scope);
			} else {
				intoScope.lift(fxtrId);
			}
		}

		if (loadCodec === null) {
			return fxtrId;
		}
		return new EntityRef(fxtrId, loadCodec);
	}
}

// ---------------------------------------------------------------------------
// createHttpReadNamespace / withHttpReadNamespace
// ---------------------------------------------------------------------------

/**
 * Create an HTTP-backed read namespace. The caller is responsible for
 * cleanup via `ns.scope.scheduleClose()` (sync) or `await ns.scope.dispose()` (async).
 *
 * This is the non-callback alternative to `withHttpReadNamespace`. Use it when
 * the namespace's lifetime is tied to something other than a single async
 * function (e.g., a React component tree).
 */
export function createHttpReadNamespace(fetchFn: FetchFn): HttpFxtrReadNamespace {
	const scope = createFxtrScope({ name: "http_read_ns" });
	const deepFetchable = new HttpDeepFetchable(fetchFn);
	scope[FXTR_SCOPE_INTERNALS].deepFetchSources.add(deepFetchable);
	return new HttpFxtrReadNamespace(scope, fetchFn);
}

/**
 * Create an HTTP-backed read namespace and run a callback with it.
 *
 * The namespace's scope is cleaned up after the callback completes.
 * This is the TS equivalent of Python's `async with HttpFxtrReadNamespace(client) as ns:`.
 */
export async function withHttpReadNamespace<R>(
	fetchFn: FetchFn,
	callback: (ns: FxtrReadNamespace) => Promise<R>,
): Promise<R> {
	const ns = createHttpReadNamespace(fetchFn);
	try {
		return await callback(ns);
	} finally {
		await ns.scope.dispose();
	}
}
