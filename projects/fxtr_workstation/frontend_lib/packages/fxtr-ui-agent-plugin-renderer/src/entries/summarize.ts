/**
 * One-line summary helpers per event type. Each renderer's collapsed
 * `<CollapsibleCard>` reads the corresponding helper to produce its
 * inline summary string.
 *
 * The shared `summarizeValue` helper produces short
 * representations of a `ResultValue`:
 *
 * - primitives by value (with string truncation),
 * - `EntityRefStub` / `ServiceRefStub` as `<entity shortId>` /
 *   `<service shortId>`,
 * - lists as `list[N]`,
 * - dicts as `{K keys}`,
 * - byte arrays as `bytes(N)`.
 *
 * Anything more elaborate is intentionally avoided — the goal is a
 * glance-level signal that helps the user decide whether to expand
 * the block.
 */

import { EntityRefStub, type ResultValue, ServiceRefStub } from "@fxtr-ui/core";

import type {
	CallbackNotificationValue,
	InvokeErrorValue,
	InvokeResultValue,
	ScriptExecutionValue,
	ScriptOpRecord,
} from "../data_shapes.js";
import { OP_KIND_PRESENT_TO_USER } from "../data_shapes.js";

/** Default truncation width for any "first N chars" rule below. */
const SUMMARY_TRUNCATE = 60;

/**
 * Truncate `s` to at most `n` characters, collapsing newlines to
 * spaces and appending an ellipsis when the original exceeded `n`.
 * Returns the empty string for empty input.
 */
export function truncate(s: string, n: number = SUMMARY_TRUNCATE): string {
	const flat = s.replace(/\s+/g, " ").trim();
	if (flat.length === 0) return "";
	if (flat.length <= n) return flat;
	return `${flat.slice(0, n)}…`;
}

/**
 * Short representation of a `ResultValue`. Targeted at one-line
 * collapsed-card summaries; not a wire codec.
 *
 * Containers report their cardinality rather than their contents
 * (we'd burn a lot of vertical space recursing and the user can
 * expand the card if they want detail). Refs surface their shortId.
 */
export function summarizeValue(value: ResultValue): string {
	if (value === null) return "null";
	if (value === undefined) return "undefined";
	switch (typeof value) {
		case "string":
			return `"${truncate(value, 40)}"`;
		case "number":
		case "boolean":
			return String(value);
	}
	if (value instanceof EntityRefStub) return `<entity ${value.shortId}>`;
	if (value instanceof ServiceRefStub) return `<service ${value.shortId}>`;
	if (value instanceof Uint8Array) return `bytes(${value.length})`;
	if (Array.isArray(value)) return `list[${value.length}]`;
	if (typeof value === "object") {
		const keys = Object.keys(value as Record<string, unknown>);
		return `{${keys.length} keys}`;
	}
	return String(value);
}

/**
 * Summary for a `script_execution` entry. Branches on the three
 * terminal shapes:
 *
 * - error non-null → `errored: <first 60 chars>`
 * - rejected non-null → `rejected: <first 60 chars>`
 * - otherwise → `ran (<N ops>)` where N counts non-`present_to_user`
 *   ops (the presented ones live in their own sibling block).
 */
export function summarizeScript(v: ScriptExecutionValue): string {
	if (v.error !== null) return `errored: ${truncate(v.error)}`;
	if (v.rejected !== null) return `rejected: ${truncate(v.rejected)}`;
	const nonPresented = v.ops.filter(
		(op: ScriptOpRecord) => op.kind !== OP_KIND_PRESENT_TO_USER,
	).length;
	return `ran (${nonPresented} op${nonPresented === 1 ? "" : "s"})`;
}

/** Summary for an `invoke_result` entry: `<slot_key>:<slot_n> → <repr>`. */
export function summarizeInvokeResult(v: InvokeResultValue): string {
	return `${v.slot_key}:${v.slot_n} → ${summarizeValue(v.payload)}`;
}

/** Summary for an `invoke_error` entry: `<slot_key>:<slot_n>: <error first line>`. */
export function summarizeInvokeError(v: InvokeErrorValue): string {
	return `${v.slot_key}:${v.slot_n}: ${truncate(v.message)}`;
}

/**
 * Summary for a `callback_notification` entry.
 *
 * For the reserved `workstation_inbox` slot — used by the
 * `send`-from-frontend round-trip — surface the message text
 * (structured `{message, attached}` payloads' `message` field, or
 * the bare-string payload). For other slot keys, show
 * `<slot_key>:<slot_n>: <repr>`.
 */
export function summarizeCallbackNotif(v: CallbackNotificationValue): string {
	if (v.slot_key === "workstation_inbox") {
		const p = v.payload;
		if (typeof p === "string") return truncate(p);
		if (p !== null && typeof p === "object" && !Array.isArray(p)) {
			const message = (p as Record<string, ResultValue>).message;
			if (typeof message === "string") return truncate(message);
		}
		return summarizeValue(p);
	}
	return `${v.slot_key}:${v.slot_n}: ${summarizeValue(v.payload)}`;
}
