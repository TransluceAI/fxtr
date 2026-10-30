/**
 * The connector's entity fetch + decode + precomp store.
 *
 * design/03 weighed reusing `HttpReadableArena` vs extracting the
 * fetch/decode core. We **extract** it: the arena gates `load` on prior
 * "tracking" and pulls in the whole `ReadableArena`/scope surface we
 * don't need here (the connector has no scope), so a small fetcher is
 * lighter and avoids a dependency on `@fxtr/http-client` entirely — this
 * module needs only `@fxtr/core`'s `fxtrCborLoads`.
 *
 * `GET /data/{id_string}` returns a CBOR envelope `{bytes, precomp}`
 * (`asgi_app.py`): `bytes` is the entity's fxtr-CBOR; `precomp` maps each
 * seed/closure id_string to its `{unwraps_to?, ctx_node?, backstep?}`
 * provenance edges. The envelope itself is tagless, so `fxtrCborLoads`
 * decodes it as a plain map (byte strings → `Uint8Array`); the inner
 * `bytes` decode produces the `FxtrCbor` tree with `Raw*FxtrID` leaves.
 *
 * Precomp accumulates across loads (it's the input to local entity
 * metadata, design/03 §Metadata); entity payloads are *not* cached
 * (content-addressed; refetched on demand — overview persistence rule).
 */

import { type FxtrCbor, fxtrCborLoads } from "@fxtr/core";

/** A fetch-compatible function (matches the standard `fetch`). */
export type FetchFn = (url: string, init?: RequestInit) => Promise<Response>;

/** Provenance edges the server precomputes for an id (all id_strings). */
export interface PrecompEntry {
	readonly unwraps_to?: string;
	readonly ctx_node?: string;
	readonly backstep?: string;
}

/** Thrown when `/data/{id}` can't be loaded. */
export class EntityLoadError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "EntityLoadError";
	}
}

/** Join a base URL and a path, leaving an already-absolute path alone. */
function joinUrl(baseUrl: string, path: string): string {
	if (path.startsWith("http://") || path.startsWith("https://")) return path;
	const base = baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
	return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

export class EntityFetcher {
	private readonly precomp = new Map<string, PrecompEntry>();

	constructor(
		private readonly baseUrl: string,
		private readonly fetchFn: FetchFn,
	) {}

	/**
	 * Fetch + decode `/data/{idString}` into its `FxtrCbor` tree,
	 * ingesting the response's precomp closure as a side effect.
	 *
	 * @throws EntityLoadError on a non-2xx response (404 → not found,
	 *   503 → server scope closed, else the status + body).
	 */
	async load(idString: string): Promise<FxtrCbor> {
		const resp = await this.fetchFn(joinUrl(this.baseUrl, `/data/${idString}`));
		if (resp.status === 404) throw new EntityLoadError(`entity ${idString} not found on server`);
		if (resp.status === 503) throw new EntityLoadError("server scope is closed");
		if (!resp.ok) {
			throw new EntityLoadError(`GET /data/${idString} failed: HTTP ${resp.status}`);
		}
		const envelope = fxtrCborLoads(new Uint8Array(await resp.arrayBuffer())) as {
			bytes?: unknown;
			precomp?: Record<string, PrecompEntry>;
		};
		if (envelope.precomp) {
			for (const [id, entry] of Object.entries(envelope.precomp)) this.precomp.set(id, entry);
		}
		if (!(envelope.bytes instanceof Uint8Array)) {
			throw new EntityLoadError(`/data/${idString} envelope missing 'bytes'`);
		}
		return fxtrCborLoads(envelope.bytes);
	}

	/** The precomp edges for an id, if seen in any load's closure. */
	precompFor(idString: string): PrecompEntry | undefined {
		return this.precomp.get(idString);
	}
}
