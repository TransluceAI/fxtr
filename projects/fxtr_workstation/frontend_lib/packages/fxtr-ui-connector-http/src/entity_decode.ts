/**
 * Client-side decode of a `/data` entity payload into the same
 * `ResultValue` shape the workstation produces.
 *
 * The workstation pipeline is `load_cbor(id)` → `escape(...)` →
 * (wire) → `unescapeWire(...)`. The stateless connector replaces the
 * middle with a single local walk over the already-decoded CBOR tree:
 *
 *   GET /data/{id} → {bytes, precomp}
 *     └─ fxtrCborLoads(bytes)        # FxtrCbor tree, Raw*FxtrID at ref leaves
 *          └─ cborToResultValue(...) # Raw* → interned EntityRefStub
 *
 * There is **no JSON round trip** (unlike the wire codec): real lists
 * stay lists (no `[[...]]` wrapping), bytes stay `Uint8Array` (no
 * base64). `$fxtr.ctx` / `$fxtr.type` fields survive as ordinary dict
 * entries whose ref values become stubs — exactly how the dynamic
 * dispatcher finds an entity's type. Entity data never carries service
 * refs, so the output's only stub kind is `EntityRefStub`.
 *
 * Parity contract (design/03): for the same entity served by a
 * workstation and by the Python stateless backend,
 * `unescapeWire(workstation LoadResultEvent.payload)` and
 * `cborToResultValue(fxtrCborLoads(/data bytes))` are structurally
 * equal (stub equality = shortId-table-mediated identity).
 */

import type { EntityRefStub, ResultValue } from "@fxtr-ui/core";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
} from "@fxtr/core";

/**
 * The id_string of the trivial `u`-node wrapping a bare data `id_string`.
 *
 * A `u`-node is synthetic provenance (it just "unwraps to" its child); it is
 * never a stored entity, so it must be constructed locally rather than fetched.
 * Equivalent to `` `u${dataIdString}` `` (see `FxtrID._computeIdString`), but
 * routed through `FxtrID` so the format stays single-sourced.
 */
export function uNodeIdStringFor(dataIdString: string): string {
	return FxtrID.fromTagAndPayload("u", FxtrID.fromIdString(dataIdString).rawDataId).idString();
}

/** Callbacks `cborToResultValue` uses to intern ref leaves. */
export interface CborDecodeOptions {
	/**
	 * Intern (or mint) the entity-ref stub for an entity's data
	 * `id_string`. The connector implements this as
	 * `entityRef(shortIdTable.shortIdForEntity(idString))`, allocating
	 * shortIds as a side effect.
	 */
	entityRefForIdString: (idString: string) => EntityRefStub<unknown>;
}

/**
 * Walk a decoded `FxtrCbor` tree into a `ResultValue`: every
 * `RawWeakFxtrID` / `RawStrongFxtrID` / `RawFxtrEntityMarkerID` leaf
 * becomes an interned `EntityRefStub` (via `opts.entityRefForIdString`
 * on the leaf's `innerId.idString()`); `Uint8Array` and primitives pass
 * through; lists and dicts recurse.
 */
export function cborToResultValue(cbor: FxtrCbor, opts: CborDecodeOptions): ResultValue {
	// Ref leaves — all three tag variants map to an entity stub. Checked
	// before the generic object branch (they are objects too).
	if (
		cbor instanceof RawWeakFxtrID ||
		cbor instanceof RawStrongFxtrID ||
		cbor instanceof RawFxtrEntityMarkerID
	) {
		return opts.entityRefForIdString(cbor.innerId.idString());
	}
	if (cbor instanceof Uint8Array) return cbor;
	if (
		cbor === null ||
		typeof cbor === "boolean" ||
		typeof cbor === "number" ||
		typeof cbor === "string"
	) {
		return cbor;
	}
	if (Array.isArray(cbor)) {
		return cbor.map((item) => cborToResultValue(item, opts));
	}
	const obj = cbor as { readonly [key: string]: FxtrCbor };
	const out: { [key: string]: ResultValue } = {};
	for (const key of Object.keys(obj)) {
		out[key] = cborToResultValue(obj[key] as FxtrCbor, opts);
	}
	return out;
}
