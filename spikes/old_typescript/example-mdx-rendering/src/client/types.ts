/**
 * Shared types and helpers for rendering FxtrCbor component trees on the client.
 */

import type { FxtrCbor } from "@fxtr/core";

/**
 * Check if a FxtrCbor value is a component tree node tuple [type, props, children].
 */
export function isNodeTuple(
	value: FxtrCbor,
): value is readonly [string, { readonly [key: string]: FxtrCbor }, readonly FxtrCbor[]] {
	return (
		Array.isArray(value) &&
		value.length === 3 &&
		typeof value[0] === "string" &&
		typeof value[1] === "object" &&
		value[1] !== null &&
		!ArrayBuffer.isView(value[1]) &&
		Array.isArray(value[2])
	);
}
