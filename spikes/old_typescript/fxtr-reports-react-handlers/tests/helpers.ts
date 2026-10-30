/**
 * Test helpers — re-export internal utilities for testing.
 */

import type { FxtrCbor } from "@fxtr/core";

/** Type guard for node tuples: [type, props, children] */
export function isNodeTuple(
	value: FxtrCbor,
): value is readonly [string, Record<string, FxtrCbor>, readonly FxtrCbor[]] {
	return (
		Array.isArray(value) &&
		value.length === 3 &&
		typeof value[0] === "string" &&
		typeof value[1] === "object" &&
		value[1] !== null &&
		!Array.isArray(value[1]) &&
		Array.isArray(value[2])
	);
}
