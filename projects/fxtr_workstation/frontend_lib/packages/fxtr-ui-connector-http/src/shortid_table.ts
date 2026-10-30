/**
 * The connector's persisted shortId ↔ stable-identifier table.
 *
 * Nothing shortId-shaped crosses the *server* wire — the server speaks
 * `id_string`s and self-describing descriptors. But the UI layer and
 * persisted session state use workstation-style short IDs, so the
 * connector mints and interns them client-side and keeps this table to
 * map back. The table is **load-bearing for rehydration** (persisted
 * state stores `["entity", shortId]` / `["service", shortId]` tags), so
 * it lives in the same `Storage` backend as the sessions + layout and
 * ages with them (design_scratch §7).
 *
 * Allocation is a **faithful port of the workstation's `short_ids.py`** so
 * the IDs the UI sees are the same shape a real workstation would mint:
 *
 *   - **Entities**: keyed by a `ProvenancedKey` `(data, prov)` (both bare
 *     local id_strings; `prov` is `null` for an unprovenanced entity). All
 *     keys with the same `data` share a *bucket*. The short ID is a
 *     *prefix of the data id_string* (so it carries the real tag char and
 *     stays debuggable against the content-addressed identity): prefixable
 *     data tags (`d`/`c`/`n`) take the first {@link ENTITY_PREFIX_LEN}
 *     chars, extended one char at a time on collision with another data
 *     id; non-prefixable tags (`.`/`!`/`p`/`u`) take the full id_string.
 *     Provenance is rendered as a suffix on the bucket prefix (the
 *     workstation's `@N`/`@0`/`@u` family — see {@link allocateEntityShortId}):
 *     the first key in a bucket claims the bare prefix; a later
 *     `(data, null)` claims `@0`; the self-`u` form `(X, u(X))` claims
 *     `@u`; any other new prov claims an ascending `@N` (prefixable) or a
 *     composite `@{shortId-of-(prov,null)}` (non-prefixable). Lookups
 *     never parse the suffix — they answer from the table.
 *   - **Services**: an ascending `s{N}` counter, one per *canonical*
 *     descriptor (two descriptors differing only in `service_type_id` —
 *     or in relative-vs-absolute URL spelling — share one `s{N}`,
 *     design/01 Q-J). Because this table is persisted across reloads
 *     (unlike the workstation's run-lifetime counter), the high-water
 *     mark is persisted too and restored on rehydrate so post-reload
 *     allocations keep ascending without colliding.
 *
 * **Persistence carries only the entries** (shortId → stable identifier),
 * not the per-bucket allocation state: the buckets + the `@N` high-water
 * marks + the prefixable-prefix claims are **rebuilt from the entries** on
 * construction (the same way the service counter is recovered), so the
 * persisted shape stays minimal and there is one source of truth.
 *
 * `classifyShortId` and all lookups are answered from the table, never
 * by parsing the shortId.
 */

import { type ServiceDescriptor, canonicalizeDescriptor } from "./descriptor";

/**
 * A table entry: what stable identifier a shortId stands for. An entity
 * entry carries the bare data `idString`; `provIdString` is the bare
 * provenance-node id_string, present **iff** the entity is layered.
 */
export type ShortIdEntry =
	| { readonly kind: "entity"; readonly idString: string; readonly provIdString?: string }
	| { readonly kind: "service"; readonly descriptor: ServiceDescriptor };

/** The provenance-aware reverse-lookup result for an entity shortId. */
export interface EntityKey {
	/** Bare data-level id_string. */
	readonly dataIdString: string;
	/** Bare provenance-node id_string, or `null` if unprovenanced. */
	readonly provIdString: string | null;
}

/** The persisted JSON shape (one `Storage` value). */
export interface SerializedShortIdTable {
	readonly version: 1;
	readonly entries: { readonly [shortId: string]: ShortIdEntry };
	/** Ascending service-counter high-water mark (next `s{N}` to mint). */
	readonly serviceCounter?: number;
}

/**
 * Default entity prefix length for prefixable (`d`/`c`/`n`) data tags —
 * matches the workstation's `entity_short_id_prefix_len = 7` (the tag
 * char plus six content chars). Extended on collision.
 */
const ENTITY_PREFIX_LEN = 7;

/** Data tags whose `id_string` is a long, prefix-able string. */
const PREFIXABLE_TAGS: ReadonlySet<string> = new Set(["d", "c", "n"]);

/**
 * Per-data-id bucket for entity short-id allocation. All keys sharing a
 * data id live here, distinguished by their `prov` half (`null` keyed
 * directly in the `Map`). Mirrors `short_ids.py`'s `_EntityBucket`.
 */
interface EntityBucket {
	readonly prefix: string;
	readonly isPrefixable: boolean;
	/** prov id_string (or `null`) → allocated shortId. */
	readonly shortIdsByProv: Map<string | null, string>;
	/** Next `@N` to allocate (prefixable buckets only); always ≥ 2. */
	nextSuffix: number;
}

export class ShortIdTable {
	private readonly byShortId = new Map<string, ShortIdEntry>();
	/** Per-data-id buckets (the entity forward map lives in each bucket). */
	private readonly entityBuckets = new Map<string, EntityBucket>();
	/** Claimed prefixable prefixes → data id, for collision extension. */
	private readonly prefixablePrefixToDataId = new Map<string, string>();
	private readonly serviceToShortId = new Map<string, string>(); // canonicalKey → shortId
	/** Next `s{N}` to mint; restored from the persisted high-water mark. */
	private serviceCounter = 0;

	/**
	 * @param baseUrl Connector base URL, used to resolve relative
	 *   descriptor URLs to absolute during canonicalization.
	 * @param persisted A previously-serialized table to rehydrate from.
	 */
	constructor(
		private readonly baseUrl: string,
		persisted?: SerializedShortIdTable,
	) {
		if (persisted) this.rehydrate(persisted);
	}

	/**
	 * Intern (or look up) the shortId for an entity. `provIdString`
	 * (the bare provenance-node id_string) makes it a layered key; omit
	 * it (or pass `null`) for an unprovenanced entity.
	 */
	shortIdForEntity(dataIdString: string, provIdString?: string | null): string {
		return this.assignEntity(dataIdString, provIdString ?? null);
	}

	/** Intern (or look up) the shortId for a service descriptor. */
	shortIdForService(descriptor: ServiceDescriptor): string {
		const key = canonicalizeDescriptor(descriptor, this.baseUrl);
		const existing = this.serviceToShortId.get(key);
		if (existing !== undefined) {
			// Same identity (canonical key) — retain the richer descriptor
			// if this occurrence carries a `service_type_id` the stored one
			// lacked (a non-identity annotation, so the shortId is unchanged).
			const entry = this.byShortId.get(existing);
			if (
				entry?.kind === "service" &&
				entry.descriptor.service_type_id === undefined &&
				descriptor.service_type_id !== undefined
			) {
				this.byShortId.set(existing, { kind: "service", descriptor });
			}
			return existing;
		}
		const shortId = `s${this.serviceCounter++}`;
		this.byShortId.set(shortId, { kind: "service", descriptor });
		this.serviceToShortId.set(key, shortId);
		return shortId;
	}

	/** Resolve a shortId to its entry, or `undefined` if unknown. */
	lookup(shortId: string): ShortIdEntry | undefined {
		return this.byShortId.get(shortId);
	}

	/**
	 * Resolve an entity shortId to its `(data, prov)` key, or `undefined`
	 * if the shortId is unknown or names a service. The provenance-aware
	 * reverse lookup the connector uses to learn whether a ref is layered.
	 */
	entityKeyFor(shortId: string): EntityKey | undefined {
		const entry = this.byShortId.get(shortId);
		if (entry?.kind !== "entity") return undefined;
		return { dataIdString: entry.idString, provIdString: entry.provIdString ?? null };
	}

	/**
	 * Classify a shortId as naming an entity or a service, answered
	 * authoritatively from the table (not by parsing the shortId).
	 * `null` for an unknown shortId.
	 */
	classifyShortId(shortId: string): "entity" | "service" | null {
		return this.byShortId.get(shortId)?.kind ?? null;
	}

	/** Serialize for persistence through the connector's `Storage` handle. */
	toJSON(): SerializedShortIdTable {
		const entries: { [shortId: string]: ShortIdEntry } = {};
		for (const [shortId, entry] of this.byShortId) entries[shortId] = entry;
		return { version: 1, entries, serviceCounter: this.serviceCounter };
	}

	// ----------------------------------------------------------------
	// Entity allocation (faithful port of short_ids.py)
	// ----------------------------------------------------------------

	/** Port of `_assign_entity`: intern `(data, prov)`, allocating on miss. */
	private assignEntity(dataIdString: string, prov: string | null): string {
		let bucket = this.entityBuckets.get(dataIdString);
		if (bucket === undefined) bucket = this.allocateBucket(dataIdString);
		const existing = bucket.shortIdsByProv.get(prov);
		if (existing !== undefined) return existing;
		const shortId = this.allocateEntityShortId(bucket, dataIdString, prov);
		bucket.shortIdsByProv.set(prov, shortId);
		this.byShortId.set(
			shortId,
			prov === null
				? { kind: "entity", idString: dataIdString }
				: { kind: "entity", idString: dataIdString, provIdString: prov },
		);
		return shortId;
	}

	/**
	 * Port of `_allocate_bucket`: mint a fresh per-data-id bucket. For a
	 * prefixable data id, find a prefix that doesn't collide with another
	 * bucket (extending one char at a time); non-prefixable uses the full
	 * id_string.
	 */
	private allocateBucket(dataIdString: string): EntityBucket {
		const tag = dataIdString[0] ?? "";
		if (PREFIXABLE_TAGS.has(tag)) {
			const start = Math.min(ENTITY_PREFIX_LEN, dataIdString.length);
			for (let n = start; n <= dataIdString.length; n++) {
				const candidate = dataIdString.slice(0, n);
				if (!this.prefixablePrefixToDataId.has(candidate)) {
					this.prefixablePrefixToDataId.set(candidate, dataIdString);
					const bucket: EntityBucket = {
						prefix: candidate,
						isPrefixable: true,
						shortIdsByProv: new Map(),
						nextSuffix: 2,
					};
					this.entityBuckets.set(dataIdString, bucket);
					return bucket;
				}
			}
			// The full id_string is unique, so the loop terminates — reaching
			// here means it was already claimed by a *different* data id, which
			// is impossible for content-addressed ids.
			throw new Error(`unable to allocate prefixable shortId for ${dataIdString}`);
		}
		const bucket: EntityBucket = {
			prefix: dataIdString,
			isPrefixable: false,
			shortIdsByProv: new Map(),
			nextSuffix: 2,
		};
		this.entityBuckets.set(dataIdString, bucket);
		return bucket;
	}

	/**
	 * Port of `_allocate_entity_short_id`: compute a fresh shortId for a
	 * `(data, prov)` key in its bucket (caller verified it isn't present).
	 * The first key in a bucket claims the bare prefix regardless of its
	 * prov; later keys take `@0` (a late `(data, null)`), `@u` (the self-`u`
	 * form `(X, u(X))`), an ascending `@N` (prefixable), or a composite
	 * `@{prov-shortId}` (non-prefixable, recursing on `(prov, null)`).
	 */
	private allocateEntityShortId(
		bucket: EntityBucket,
		dataIdString: string,
		prov: string | null,
	): string {
		// The self-`u` form: prov is `u(data)`, whose id_string is `"u" +
		// data.local_id_string()` — i.e. the bare data id with a `u` prefix.
		const isSelfU = prov !== null && prov === `u${dataIdString}`;
		if (bucket.isPrefixable) {
			if (bucket.shortIdsByProv.size === 0) return bucket.prefix; // first: bare (@1 implicit)
			if (prov === null) return `${bucket.prefix}@0`;
			if (isSelfU) return `${bucket.prefix}@u`;
			return `${bucket.prefix}@${bucket.nextSuffix++}`;
		}
		// Non-prefixable.
		if (prov === null) return bucket.prefix;
		if (isSelfU) return `${bucket.prefix}@u`;
		// The prov half's `(prov, null)` shortId composes into ours.
		const provShortId = this.assignEntity(prov, null);
		return `${bucket.prefix}@${provShortId}`;
	}

	// ----------------------------------------------------------------
	// Rehydration (rebuild buckets + counters from the persisted entries)
	// ----------------------------------------------------------------

	private rehydrate(persisted: SerializedShortIdTable): void {
		let maxServiceN = -1;
		// `@N` high-water per bucket, accumulated as we scan entity entries.
		const maxSuffixByData = new Map<string, number>();
		for (const [shortId, entry] of Object.entries(persisted.entries)) {
			this.byShortId.set(shortId, entry);
			if (entry.kind === "service") {
				this.serviceToShortId.set(canonicalizeDescriptor(entry.descriptor, this.baseUrl), shortId);
				const m = /^s(\d+)$/.exec(shortId);
				if (m) maxServiceN = Math.max(maxServiceN, Number(m[1]));
				continue;
			}
			const dataIdString = entry.idString;
			const prov = entry.provIdString ?? null;
			const isPrefixable = PREFIXABLE_TAGS.has(dataIdString[0] ?? "");
			// All entries in a bucket share the before-`@` prefix; for a
			// non-prefixable bucket the prefix is the full data id_string
			// (whose own text may contain no allocation `@`).
			const prefix = isPrefixable ? (shortId.split("@")[0] ?? shortId) : dataIdString;
			let bucket = this.entityBuckets.get(dataIdString);
			if (bucket === undefined) {
				bucket = { prefix, isPrefixable, shortIdsByProv: new Map(), nextSuffix: 2 };
				this.entityBuckets.set(dataIdString, bucket);
				if (isPrefixable) this.prefixablePrefixToDataId.set(prefix, dataIdString);
			}
			bucket.shortIdsByProv.set(prov, shortId);
			// Track the max numeric `@N` suffix to restore `nextSuffix`.
			const suffixMatch = /@(\d+)$/.exec(shortId);
			if (suffixMatch) {
				const n = Number(suffixMatch[1]);
				maxSuffixByData.set(dataIdString, Math.max(maxSuffixByData.get(dataIdString) ?? 1, n));
			}
		}
		for (const [dataIdString, maxN] of maxSuffixByData) {
			const bucket = this.entityBuckets.get(dataIdString);
			if (bucket) bucket.nextSuffix = Math.max(2, maxN + 1);
		}
		// Continue the ascending service counter past every rehydrated `s{N}`
		// (and past the persisted high-water mark, defensively).
		this.serviceCounter = Math.max(persisted.serviceCounter ?? 0, maxServiceN + 1);
	}
}
