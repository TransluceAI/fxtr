/**
 * Typed metadata shapes returned by
 * `FxtrSessionUpdateCtx.allocateGetEntityMetadata` /
 * `allocateGetServiceMetadata`.
 *
 * These mirror the fields on the workstation's
 * `GetEntityMetadataOp` / `GetServiceMetadataOp` result events. The
 * shapes live in core rather than the workstation connector because
 * the concepts they describe (FxtrIDs, provenance) are fxtr-domain —
 * any future connector exposing entity metadata would surface the
 * same fields.
 *
 * All ref fields decode through the connector's `unescapeWire` to
 * `EntityRefStub` instances; consumers can pass them straight back
 * into `ctx.allocateLoad(...)` to load the targeted entity.
 *
 * Optional-by-default: the workstation can add fields without
 * protocol churn, and not every applicable field is populated for
 * every entity (e.g. `provenance_parent` only appears for layered
 * FxtrIDs; `context_data` only for `c`-tag data IDs; etc.).
 * `id_string` is the only field guaranteed to be present.
 */

import type { EntityRefStub } from "./stubs";

export interface EntityMetadata {
	/**
	 * Local-only id-string of the data half of the targeted layered
	 * FxtrID. The provenance half is surfaced via
	 * `provenance_parent_id_string` when present, so emitting the
	 * full layered id-string would be redundant.
	 */
	readonly id_string: string;
	/**
	 * Ref of `ProvenancedKey(data, None)` — the bare data half of
	 * the targeted record. Always populated on the wire.
	 */
	readonly unprovenanced?: EntityRefStub;
	/**
	 * Ref of `ProvenancedKey(prov, None)` — the bare provenance half
	 * of the targeted record. Populated when the targeted key has a
	 * non-None `prov_parent`.
	 */
	readonly provenance_parent?: EntityRefStub;
	/**
	 * Full id-string of the provenance parent. Populated alongside
	 * `provenance_parent`.
	 */
	readonly provenance_parent_id_string?: string;
	/**
	 * Ref of the layered `(unwrapped, data)` form. Populated when
	 * the data ID has a provenance-node tag (`c`/`n`/`p`/`u`) and
	 * the arena's `unwraps_to` chain is known.
	 */
	readonly unwraps_to?: EntityRefStub;
	/**
	 * Local-only id-string of `unwraps_to`. Populated alongside.
	 */
	readonly unwraps_to_id_string?: string;
	/**
	 * Ref of `ProvenancedKey(c-data, None)`. Populated when the data
	 * ID has tag `c` (the c-node's context data).
	 */
	readonly context_data?: EntityRefStub;
	/**
	 * Ref of `ProvenancedKey(p-prefix, None)`. Populated when the
	 * data ID has tag `p` and the path has length ≥ 1 (the p-node
	 * with the last path component dropped).
	 */
	readonly backstep?: EntityRefStub;
}

export interface ServiceMetadata {
	/**
	 * Id-string of the service's `service_type_id`. Currently the
	 * only field on service metadata; the shape exists so future
	 * fields slot in without API churn.
	 */
	readonly type_id_string: string;
}
