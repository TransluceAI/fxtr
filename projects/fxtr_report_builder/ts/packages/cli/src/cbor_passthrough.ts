/**
 * Tag-preserving CBOR encode/decode.
 *
 * The Python side ships ref-typed input values as raw CBOR bytes
 * (encoded by `fxtr_cbor_dumps` with tag numbers 33000 / 33001 /
 * 33002 for marker / weak / strong refs). The TS side never needs
 * to interpret these tags — it treats them as opaque values that
 * round-trip verbatim through MDX expansion and back into the
 * output CBOR.
 *
 * The `cbor-x` library represents unknown tags as `Tag` instances;
 * these are JSON-incompatible objects that the MDX runtime sees as
 * "some opaque value", embeds in JSX props, and the flatten pass
 * carries through to the output tuple tree. Re-encoding then
 * preserves the tag numbers + payloads.
 */

import { Decoder, Encoder, Tag } from "cbor-x";

/**
 * Lazy decoder/encoder instances. Configured for:
 *
 * - `tagUint8Array: false`, `useRecords: false`: emit plain
 *   primitives / objects / arrays without library-specific shapes.
 * - `useTag259ForMaps`: irrelevant for our shapes.
 * - The decoder leaves unknown tags as `Tag` instances by default.
 * - The encoder writes through `Tag` instances by default.
 */
const decoder = new Decoder({
	useRecords: false,
	mapsAsObjects: true,
});

const encoder = new Encoder({
	useRecords: false,
	mapsAsObjects: true,
});

/** Decode CBOR bytes; unknown tags become `Tag` instances. */
export function decodeCbor(bytes: Uint8Array): unknown {
	return decoder.decode(bytes);
}

/** Encode a value to CBOR bytes; `Tag` instances are written through. */
export function encodeCbor(value: unknown): Uint8Array {
	return encoder.encode(value);
}

export { Tag };
