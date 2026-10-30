/**
 * List-based escape codec, shared across connectors (it operates on
 * the abstract `@fxtr-ui/core` stubs via caller-supplied callbacks, so
 * no connector-specific identity leaks in). Originally the workstation
 * `/client` codec; relocated here when a second connector
 * (`@fxtr-ui/connector-http`) needed it. The HTTP connector adds a
 * "resolved mode" (entity → id_string, service → descriptor, callback
 * → uuid) on top of the same machinery.
 *
 * TS port of `fxtr_workstation/wire_escape.py`. Wire shape:
 *
 * - Every literal JSON list `[a, b, c]` is encoded as `[[a, b, c]]`.
 * - A 2-or-3-element list whose first element is a reserved tag
 *   string is a tagged special:
 *   - `["entity", "<payload>"]` — entity reference. Payload is a
 *     string: a server short_id (shortId mode) or the full
 *     `id_string` (resolved mode).
 *   - `["service", <payload>]` — service reference. Payload is a
 *     short_id **string** (shortId mode) or a self-describing
 *     **descriptor object** `{kind, url, ...}` (resolved mode); the
 *     decoder routes the two to `lookupService` /
 *     `lookupServiceDescriptor`.
 *   - `["bytes", "<base64>"]` — raw bytes (standard base64).
 *   - `["callback", <payload>]` — send-side callback handle (the
 *     wire form of `CallbackHandleStub`). Payload is a slot int
 *     (shortId mode) or an inbox uuid string (resolved mode).
 *     Send-only: the encoder emits this when a `CallbackHandleStub`
 *     appears in invoke args; the decoder always rejects it (the
 *     server never sends this tag to the client).
 *   - `["inbox", <slot:int>, <offset:int>]` — receive-side
 *     callback cursor (the wire form of `CallbackInboxStub`).
 *     Decoded only when the caller supplies `lookupInbox`;
 *     otherwise the tag is rejected.
 *   - `["broken_inbox", <slot:int>]` — an inbox reference that
 *     is known broken (cross-session smuggled stub the flush walk
 *     substituted with the session's dead-inbox-sentinel).
 *     `slot=0` is the universal dead-sentinel value. The server
 *     stores wire state opaquely so this tag is purely a
 *     frontend convention — clients writing the tag agree on its
 *     meaning; the server just round-trips the JSON.
 *   - `["op_result", <slot:int>]` — receive side of a load /
 *     invoke / metadata op. Slot integer must be > 0.
 *   - `["broken_op_result", <slot:int>]` — a slot reference that
 *     is known broken (cross-session smuggled stub, foreign-removed
 *     slot we still reference, manually constructed stub, etc).
 *     `slot=0` is the universal dead-sentinel value; `slot>0`
 *     identifies a real-but-broken slot from the receiving
 *     session's namespace.
 * - Primitives (`null`, boolean, number, string) pass through.
 * - Objects are walked; keys must be strings.
 *
 * Pure functions: no I/O, no registry mutations. Callers thread
 * connector-side state through the callbacks.
 */

import {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	OpResultStub,
	ServiceRefStub,
} from "./stubs";
import type { InvokeArgValue, ResultValue, StateValue } from "./types";

/**
 * Wire-escaped form: pure JSON values shaped by the list-escape
 * convention. Every literal list is wrapped as a single-element list
 * (`[a, b] => [[a, b]]`); tagged forms `["entity", id]` / `["service",
 * id]` / `["bytes", b64]` / `["callback", slot]` /
 * `["inbox", slot, offset]` / `["op_result", slot]` /
 * `["broken_op_result", slot]` encode stubs and bytes.
 */
export type WireValue =
	| null
	| boolean
	| number
	| string
	| readonly WireValue[]
	| { readonly [key: string]: WireValue };

/** Reserved tag string for entity references. */
export const TAG_ENTITY = "entity";
/** Reserved tag string for service references. */
export const TAG_SERVICE = "service";
/** Reserved tag string for raw bytes (base64 payload). */
export const TAG_BYTES = "bytes";
/** Reserved tag string for send-side callback handles. */
export const TAG_CALLBACK = "callback";
/** Reserved tag string for receive-side callback inbox cursors. */
export const TAG_INBOX = "inbox";
/**
 * Reserved tag string for broken-inbox references — a callback
 * inbox the frontend knows is unrecoverable (cross-session smuggling
 * caught at flush time, etc.). `slot=0` is the universal
 * dead-inbox-sentinel. Frontend-convention only; the server stores
 * wire state opaquely and doesn't interpret this tag. See
 * `plans/phase0/part3_frontend_ui/stage6_callback_inboxes.md`.
 */
export const TAG_BROKEN_INBOX = "broken_inbox";
/** Reserved tag string for op-result slot references. */
export const TAG_OP_RESULT = "op_result";
/**
 * Reserved tag string for broken op-result slot references — a
 * slot we know is unrecoverable. `slot=0` is the universal
 * dead-sentinel; `slot>0` identifies a real broken slot from the
 * receiving session's namespace. See
 * `plans/phase0/part3_frontend_ui/stage2_entity_loading.md` (the
 * "Broken stubs" section) for the full lifecycle.
 */
export const TAG_BROKEN_OP_RESULT = "broken_op_result";

/** All reserved tag strings recognized by the codec. */
export const RESERVED_TAGS: ReadonlySet<string> = new Set([
	TAG_ENTITY,
	TAG_SERVICE,
	TAG_BYTES,
	TAG_CALLBACK,
	TAG_INBOX,
	TAG_BROKEN_INBOX,
	TAG_OP_RESULT,
	TAG_BROKEN_OP_RESULT,
]);

/** Raised by the codec on any encode / decode failure. */
export class WireEscapeError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "WireEscapeError";
	}
}

/**
 * Callbacks the encoder uses to resolve each stub instance to its
 * connector-scoped wire form at encode time. Kept as opts rather
 * than methods on the stub class so the codec stays decoupled from
 * any particular connector's interning strategy.
 */
export interface EscapeOptions {
	/**
	 * Resolve an entity stub to its wire payload (a string): the
	 * server-allocated short_id in shortId mode, or the full
	 * content-addressed `id_string` in resolved mode. Either way the
	 * payload is a string, so the entity tag shape is identical across
	 * modes.
	 */
	encodeEntity: (stub: EntityRefStub<unknown>) => string;
	/**
	 * Resolve a service stub to its wire payload. In shortId mode this
	 * is a short_id **string** (`["service", "s7"]`); in resolved mode
	 * it is a self-describing **descriptor object** (`["service",
	 * {kind, url, ...}]`). The decoder routes the two shapes to
	 * `lookupService` / `lookupServiceDescriptor` respectively.
	 */
	encodeService: (stub: ServiceRefStub<unknown>) => WireValue;
	/**
	 * Resolve an op-result stub to its full wire form — either
	 * `["op_result", slot]` for a healthy slot or
	 * `["broken_op_result", slot]` for a slot the connector knows
	 * is broken (e.g. the slot in `brokenSlots`, or the
	 * session-scoped dead-sentinel at `slot=0`). The caller decides
	 * which form to emit; the codec just shells out. Optional for
	 * call sites that don't carry op-result stubs (e.g. the
	 * codec-only unit tests) — the encoder throws if a stub is
	 * encountered without this callback.
	 */
	encodeOpResult?: (stub: OpResultStub<unknown>) => WireValue;
	/**
	 * Resolve a callback inbox stub to its wire form — typically
	 * `["inbox", slot, offset]` for a healthy stub, or a "broken
	 * inbox" variant for a cross-session-smuggled stub the flush
	 * walk has substituted with the dead-sentinel. Optional; the
	 * encoder throws if an inbox stub is encountered without this
	 * callback (e.g. payload-decode contexts that shouldn't carry
	 * client-side stubs).
	 */
	encodeInbox?: (stub: CallbackInboxStub<unknown>) => WireValue;
	/**
	 * Resolve a callback handle stub to its wire form — typically
	 * `["callback", slot]`. The session's impl can also enforce
	 * cross-session smuggling rules here (throw if the handle
	 * belongs to a different session). Optional; the encoder throws
	 * if a handle is encountered without this callback.
	 */
	encodeCallbackHandle?: (stub: CallbackHandleStub<unknown>) => WireValue;
}

/**
 * Encode a structured value (tree of primitives, containers, and
 * stub instances) into the list-escape wire form for transmission
 * over `/client`. Entity / service leaves are mapped to their
 * tagged forms via `opts`; `Uint8Array` leaves become
 * `["bytes", base64]`; client-only stub kinds route through their
 * `encode*` opts; every literal list is wrapped in a single-element
 * list so tagged forms remain unambiguous.
 *
 * Accepts the broadest of the structured-value families
 * (`StateValue | InvokeArgValue`) since the codec is shared across
 * both state-encoding and invoke-arg-encoding sites; the per-site
 * opts determine which stub kinds are legal in that context (an
 * inbox stub in invoke args, for instance, never reaches `encodeInbox`
 * because the invoke-arg type rejects it at the type level, and the
 * runtime check in `escapeInvokeArgs` is the defense-in-depth).
 *
 * Throws `WireEscapeError` on any value type the codec doesn't know
 * how to handle, or whose `encode*` opt was not supplied.
 */
export function escapeWire(value: StateValue | InvokeArgValue, opts: EscapeOptions): WireValue {
	if (
		value === null ||
		typeof value === "boolean" ||
		typeof value === "number" ||
		typeof value === "string"
	) {
		return value;
	}
	if (value instanceof Uint8Array) {
		return [TAG_BYTES, encodeBase64(value)];
	}
	if (value instanceof EntityRefStub) {
		return [TAG_ENTITY, opts.encodeEntity(value)];
	}
	if (value instanceof ServiceRefStub) {
		return [TAG_SERVICE, opts.encodeService(value)];
	}
	if (value instanceof OpResultStub) {
		if (opts.encodeOpResult === undefined) {
			throw new WireEscapeError(
				"escape: encountered OpResultStub but encodeOpResult was not supplied (call site lacks a session context)",
			);
		}
		return opts.encodeOpResult(value);
	}
	if (value instanceof CallbackInboxStub) {
		if (opts.encodeInbox === undefined) {
			throw new WireEscapeError(
				"escape: encountered CallbackInboxStub but encodeInbox was not supplied (call site lacks a session context)",
			);
		}
		return opts.encodeInbox(value);
	}
	if (value instanceof CallbackHandleStub) {
		if (opts.encodeCallbackHandle === undefined) {
			throw new WireEscapeError(
				"escape: encountered CallbackHandleStub but encodeCallbackHandle was not supplied (call site lacks a session context)",
			);
		}
		return opts.encodeCallbackHandle(value);
	}
	if (Array.isArray(value)) {
		const inner: WireValue[] = [];
		for (const item of value) inner.push(escapeWire(item, opts));
		// Wrap every literal list as a single-element list; the
		// decoder peels this layer off. Tagged forms have a different
		// shape (a known reserved string as element 0).
		return [inner];
	}
	if (typeof value === "object") {
		const out: Record<string, WireValue> = {};
		for (const key of Object.keys(value)) {
			const v = (value as Record<string, StateValue | InvokeArgValue | undefined>)[key];
			// `StateValue` doesn't include `undefined`, but
			// consumer code that types user state with optional fields
			// (`{ message?: string }`) can produce dicts with explicit-
			// undefined values via spread or partial updates. Treat
			// them as absent — same as `JSON.stringify` and as never-
			// set fields. Skipping here keeps the wire form consistent
			// regardless of how the consumer phrased the absence.
			if (v === undefined) continue;
			out[key] = escapeWire(v, opts);
		}
		return out;
	}
	throw new WireEscapeError(`escape: unsupported value type: ${typeof value} (${String(value)})`);
}

/**
 * Callbacks the decoder uses to materialize tagged wire leaves back
 * into the caller's value types. The lookups are expected to intern,
 * so two occurrences of the same identifier decode to the same stub
 * instance (the reducer relies on this for cheap referential-equality
 * comparisons).
 *
 * The service tag has two payload shapes (see `EscapeOptions`): a
 * **string** short_id (shortId mode) routes to `lookupService`, a
 * **descriptor object** (resolved mode) routes to
 * `lookupServiceDescriptor`. A given decode context supplies whichever
 * it expects to see; the decoder throws a `WireEscapeError` if a
 * service tag arrives whose shape has no matching callback. (Entity
 * payloads are always strings — short_id or `id_string` — so a single
 * `lookupEntity` covers both modes; a layered 3-element
 * `["entity", data, prov]` tag passes both strings to that same callback.)
 *
 * The presence or absence of `lookupOpResult` / `lookupInbox` (the
 * two client-only-stub lookups) controls the return type of
 * `unescapeWire`: omitting them narrows the result to `ResultValue`
 * (no client-only stubs); supplying them widens to `StateValue`.
 *
 * Note: the codec deliberately has no `lookupCallback` — the
 * `["callback", X]` tag is only ever *sent* by the client (as an
 * invoke arg encoding `CallbackHandleStub`), never received. The
 * decoder always rejects this tag with a `WireEscapeError`; the
 * server emitting it to a client would be a protocol bug.
 */
export interface ResultUnescapeOptions {
	/**
	 * Resolve an entity payload into a stub. The payload is a short_id or
	 * `id_string` string; the optional second argument carries the
	 * **provenance** half of a layered ref (the 3-element
	 * `["entity", data, prov]` form — only the stateless HTTP wire sends it).
	 * A 2-element `["entity", X]` tag calls this with one argument (the
	 * workstation wire, whose `@N` provenance is already folded into the
	 * short_id string); a 3-element tag calls it with both.
	 */
	lookupEntity: (idString: string, provIdString?: string) => EntityRefStub<unknown>;
	/**
	 * Resolve a service short_id **string** back into a stub (shortId
	 * mode). Optional: a resolved-mode context that only ever sees
	 * descriptor objects supplies `lookupServiceDescriptor` instead.
	 */
	lookupService?: (shortId: string) => ServiceRefStub<unknown>;
	/**
	 * Resolve a service **descriptor object** back into a stub (resolved
	 * mode). Optional: a shortId-mode context that only ever sees
	 * service short_id strings supplies `lookupService` instead. The
	 * `descriptor` is the raw wire payload (a `{kind, url, ...}` object).
	 */
	lookupServiceDescriptor?: (descriptor: WireValue) => ServiceRefStub<unknown>;
}

/**
 * Extends `ResultUnescapeOptions` with the two client-only-stub
 * lookups (`lookupOpResult`, `lookupInbox`). Supplied at state
 * envelope decode sites; omitted at result/payload decode sites.
 */
export interface StateUnescapeOptions extends ResultUnescapeOptions {
	/**
	 * Resolve an op-result slot integer back into a stub instance.
	 * Called for both `["op_result", slot]` (normal) and
	 * `["broken_op_result", slot]` (broken) tags; `broken=true`
	 * triggers the session-side brokenness side effects (adding the
	 * slot to `brokenSlots` for `slot>0`, returning the
	 * dead-sentinel for `slot===0`).
	 */
	lookupOpResult: (slot: number, broken: boolean) => OpResultStub<unknown>;
	/**
	 * Resolve a callback inbox tag back into a `CallbackInboxStub`
	 * instance. Called for both `["inbox", slot, offset]` (normal)
	 * and `["broken_inbox", slot]` (broken) tags. For the broken
	 * variant, `offset` is `0` (the wire form carries no offset —
	 * a broken inbox has no meaningful cursor position). The
	 * session typically returns its dead-inbox-sentinel for
	 * `slot === 0` + `broken === true`. Stubs are not interned —
	 * inbox identity is value-typed by `(slot, offset)`.
	 */
	lookupInbox: (slot: number, offset: number, broken: boolean) => CallbackInboxStub<unknown>;
}

/** Legacy alias; prefer the explicit `ResultUnescapeOptions` /
 * `StateUnescapeOptions` types at new call sites. */
export type UnescapeOptions = ResultUnescapeOptions | StateUnescapeOptions;

/**
 * Decode a wire-escaped value back into its structured form. Tagged
 * leaves are routed through the `opts` callbacks; literal-list
 * wrappers (`[[...]]`) are unwrapped; primitives pass through.
 *
 * Overloaded so the return type tracks which stub kinds are
 * legal in the decode context: omitting `lookupOpResult` /
 * `lookupInbox` narrows to `ResultValue` (no client-only stubs);
 * supplying them widens to `StateValue`.
 *
 * Throws `WireEscapeError` on any malformed input — unknown tags,
 * wrong tag arity, invalid base64, or a list that isn't a recognized
 * tagged form or the single-element literal-list escape.
 */
export function unescapeWire(value: WireValue, opts: StateUnescapeOptions): StateValue;
export function unescapeWire(value: WireValue, opts: ResultUnescapeOptions): ResultValue;
export function unescapeWire(value: WireValue, opts: UnescapeOptions): StateValue {
	if (
		value === null ||
		typeof value === "boolean" ||
		typeof value === "number" ||
		typeof value === "string"
	) {
		return value;
	}
	if (Array.isArray(value)) {
		// Two valid shapes:
		//   1. Tagged: [tag, payload] (or [tag, payload, ...]) where
		//      tag ∈ RESERVED_TAGS.
		//   2. Literal-list escape: [inner_list] (length-1, the inner
		//      element is itself a list); the inner list's items are
		//      decoded recursively.
		if (value.length >= 1 && typeof value[0] === "string" && RESERVED_TAGS.has(value[0])) {
			return decodeTagged(value, opts);
		}
		if (value.length !== 1 || !Array.isArray(value[0])) {
			throw new WireEscapeError(
				`unescape: malformed list (expected tagged form or single-element list-of-list escape): ${jsonPreview(value)}`,
			);
		}
		const inner = value[0];
		const out: StateValue[] = [];
		for (const item of inner) out.push(unescapeWire(item, opts as StateUnescapeOptions));
		return out;
	}
	if (typeof value === "object") {
		const out: Record<string, StateValue> = {};
		for (const key of Object.keys(value)) {
			const v = (value as Record<string, WireValue>)[key] as WireValue;
			out[key] = unescapeWire(v, opts as StateUnescapeOptions);
		}
		return out;
	}
	throw new WireEscapeError(`unescape: unsupported value type: ${typeof value} (${String(value)})`);
}

/**
 * Decode a list whose first element is a known reserved tag. The
 * caller has already verified the tag membership; this function
 * validates arity / payload type per tag and routes to the
 * appropriate lookup or base64 decode.
 */
function decodeTagged(value: readonly WireValue[], opts: UnescapeOptions): StateValue {
	const tag = value[0];
	if (tag === TAG_ENTITY) {
		// Bare ["entity", X] or layered ["entity", data, prov]. Strict arity —
		// exactly 2 or 3, never longer (no silently-ignored trailing
		// elements); every element a string.
		if (value.length === 2 && typeof value[1] === "string") {
			return opts.lookupEntity(value[1]);
		}
		if (value.length === 3 && typeof value[1] === "string" && typeof value[2] === "string") {
			return opts.lookupEntity(value[1], value[2]);
		}
		throw new WireEscapeError(
			`unescape: malformed entity tag (expected ["entity", <data>] or ["entity", <data>, <prov>]): ${jsonPreview(value)}`,
		);
	}
	if (tag === TAG_SERVICE) {
		if (value.length !== 2) {
			throw new WireEscapeError(
				`unescape: malformed service tag (expected ["service", <short-id | descriptor>]): ${jsonPreview(value)}`,
			);
		}
		const payload = value[1];
		// shortId mode: payload is a short_id string → lookupService.
		if (typeof payload === "string") {
			if (opts.lookupService === undefined) {
				throw new WireEscapeError(
					`unescape: service short_id requires lookupService (shortId mode): ${jsonPreview(value)}`,
				);
			}
			return opts.lookupService(payload);
		}
		// resolved mode: payload is a descriptor object → lookupServiceDescriptor.
		if (payload !== null && typeof payload === "object" && !Array.isArray(payload)) {
			if (opts.lookupServiceDescriptor === undefined) {
				throw new WireEscapeError(
					`unescape: service descriptor requires lookupServiceDescriptor (resolved mode): ${jsonPreview(value)}`,
				);
			}
			return opts.lookupServiceDescriptor(payload);
		}
		throw new WireEscapeError(
			`unescape: malformed service payload (expected a short_id string or a descriptor object): ${jsonPreview(value)}`,
		);
	}
	if (tag === TAG_BYTES) {
		if (value.length !== 2 || typeof value[1] !== "string") {
			throw new WireEscapeError(
				`unescape: malformed bytes tag (expected ["bytes", <base64>]): ${jsonPreview(value)}`,
			);
		}
		return decodeBase64(value[1]);
	}
	if (tag === TAG_CALLBACK) {
		// `["callback", slot]` is a *send-side* encoding — the client
		// puts these into invoke args via `CallbackHandleStub`. The
		// server never sends this tag to the client, so any decode
		// path that reaches here is a protocol bug. Reject unconditionally.
		throw new WireEscapeError(
			`unescape: ["callback", slot] is a send-side-only tag — the server should never send it to the client. Got: ${jsonPreview(value)}`,
		);
	}
	if (tag === TAG_INBOX) {
		if (
			value.length !== 3 ||
			typeof value[1] !== "number" ||
			!Number.isInteger(value[1]) ||
			typeof value[2] !== "number" ||
			!Number.isInteger(value[2])
		) {
			throw new WireEscapeError(
				`unescape: malformed inbox tag (expected ["inbox", <slot:int>, <offset:int>]): ${jsonPreview(value)}`,
			);
		}
		const stateOpts = opts as StateUnescapeOptions;
		if (stateOpts.lookupInbox === undefined) {
			throw new WireEscapeError(
				`unescape: inbox tag requires a session context (lookupInbox was not supplied): ${jsonPreview(value)}`,
			);
		}
		return stateOpts.lookupInbox(value[1], value[2], /* broken */ false);
	}
	if (tag === TAG_BROKEN_INBOX) {
		if (value.length !== 2 || typeof value[1] !== "number" || !Number.isInteger(value[1])) {
			throw new WireEscapeError(
				`unescape: malformed broken_inbox tag (expected ["broken_inbox", <slot:int>]): ${jsonPreview(value)}`,
			);
		}
		if (value[1] < 0) {
			throw new WireEscapeError(
				`unescape: ["broken_inbox", slot] requires slot >= 0 (got ${value[1]}): ${jsonPreview(value)}`,
			);
		}
		const stateOpts = opts as StateUnescapeOptions;
		if (stateOpts.lookupInbox === undefined) {
			throw new WireEscapeError(
				`unescape: broken_inbox tag requires a session context (lookupInbox was not supplied): ${jsonPreview(value)}`,
			);
		}
		// Broken inbox has no meaningful offset — pass 0 by convention.
		// The session returns its dead-inbox-sentinel for slot === 0.
		return stateOpts.lookupInbox(value[1], 0, /* broken */ true);
	}
	if (tag === TAG_OP_RESULT) {
		const slot = decodeOpResultSlot(value, TAG_OP_RESULT);
		if (slot <= 0) {
			// `op_result` with slot 0 is malformed — slot 0 is reserved
			// for the dead-sentinel which always travels under the
			// `broken_op_result` tag. Real op-result slots are server-
			// allocated and strictly positive.
			throw new WireEscapeError(
				`unescape: ["op_result", slot] requires slot > 0 (got ${slot}): ${jsonPreview(value)}`,
			);
		}
		const stateOpts = opts as StateUnescapeOptions;
		if (stateOpts.lookupOpResult === undefined) {
			throw new WireEscapeError(
				`unescape: op_result tag requires a session context (lookupOpResult was not supplied): ${jsonPreview(value)}`,
			);
		}
		return stateOpts.lookupOpResult(slot, /* broken */ false);
	}
	if (tag === TAG_BROKEN_OP_RESULT) {
		const slot = decodeOpResultSlot(value, TAG_BROKEN_OP_RESULT);
		if (slot < 0) {
			throw new WireEscapeError(
				`unescape: ["broken_op_result", slot] requires slot >= 0 (got ${slot}): ${jsonPreview(value)}`,
			);
		}
		const stateOpts = opts as StateUnescapeOptions;
		if (stateOpts.lookupOpResult === undefined) {
			throw new WireEscapeError(
				`unescape: broken_op_result tag requires a session context (lookupOpResult was not supplied): ${jsonPreview(value)}`,
			);
		}
		return stateOpts.lookupOpResult(slot, /* broken */ true);
	}
	// RESERVED_TAGS.has check above narrows to one of the cases here;
	// any future addition needs an explicit branch.
	throw new WireEscapeError(`unescape: unknown reserved tag: ${String(tag)}`);
}

/**
 * Shared shape-check for the op-result-style tags: `[tag, slot:int]`.
 * Returns the slot integer; throws on bad shape (signed integers are
 * allowed at this layer — the caller does the slot-value bounds
 * check appropriate to the tag).
 */
function decodeOpResultSlot(value: readonly WireValue[], tag: string): number {
	if (value.length !== 2 || typeof value[1] !== "number" || !Number.isInteger(value[1])) {
		throw new WireEscapeError(
			`unescape: malformed ${tag} tag (expected [${JSON.stringify(tag)}, <slot:int>]): ${jsonPreview(value)}`,
		);
	}
	return value[1];
}

// --- base64 helpers --------------------------------------------------
//
// Standard base64 with strict validation. Uses `btoa` / `atob`, which
// are globals in Node 16+ and in every browser; included in the
// tsconfig `DOM` lib for typing.

/** The character-class half of base64 validation (see `isValidBase64`). */
const BASE64_VALID = /^[A-Za-z0-9+/]*={0,2}$/;

/** Validates standard base64 (RFC 4648): `A-Za-z0-9+/` with `=` padding.
 *
 * Deliberately a bare character-class regex + a length check rather
 * than the natural block-structured `(?:[A-Za-z0-9+/]{4})*(?:…)?`
 * form: V8's backtracking engine recurses once per iteration of a
 * quantified GROUP, so the block form overflows the call stack on
 * multi-megabyte payloads (a compiled renderer module was the first
 * real case). The two checks accept exactly the same strings: `=`
 * can't appear in the character class, so padding is trailing-only
 * and capped at 2, and the length gate forces whole 4-char blocks. */
function isValidBase64(s: string): boolean {
	return s.length % 4 === 0 && BASE64_VALID.test(s);
}

/** Encode bytes as standard base64. */
function encodeBase64(bytes: Uint8Array): string {
	let binary = "";
	for (const b of bytes) binary += String.fromCharCode(b);
	return btoa(binary);
}

/** Decode standard base64 into a fresh `Uint8Array`. Throws `WireEscapeError` on malformed input. */
function decodeBase64(s: string): Uint8Array {
	if (!isValidBase64(s)) {
		throw new WireEscapeError(`unescape: invalid base64 payload: ${jsonPreview(s)}`);
	}
	const binary = atob(s);
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
	return bytes;
}

// --- Invoke-arg encoder ---------------------------------------------
//
// The wire form of an `InvokeOp` carries `args: WireValue[]` and
// `kwargs: { [k: string]: WireValue }`. Each element / value is the
// `escapeWire` form of a user-supplied `InvokeArgValue`. The helper
// below wraps the per-arg walk and *up-front* rejects
// `OpResultStub` and `CallbackInboxStub` anywhere in the input —
// op-result stubs aren't a valid arg (the user must extract the
// underlying value first), and the receive-side `CallbackInboxStub`
// is also rejected (consumers pass `inbox.handle`, a
// `CallbackHandleStub`, instead). The type system already encodes
// these rules via `InvokeArgValue`; the runtime check is
// defense-in-depth.

/**
 * Encode an invoke's positional + keyword arguments for the wire.
 *
 * Each positional arg / kwarg value is walked through `escapeWire`
 * using the supplied `opts`. The same options object is reused for
 * every value, so connector-scoped context (entity / service /
 * callback-handle encoding) lands consistently across all args.
 *
 * Rejects `OpResultStub` and `CallbackInboxStub` at any depth (top
 * level, nested in a dict, nested in a list). `CallbackHandleStub`
 * is allowed and encodes to `["callback", slot]` via
 * `opts.encodeCallbackHandle` — the connector's impl also enforces
 * cross-session smuggling rules at that point.
 *
 * Non-string kwarg keys throw — mirrors `escapeWire`'s object key
 * handling.
 *
 * Empty / `undefined` kwargs returns an empty object — matches the
 * Python `InvokeOp.kwargs` default.
 *
 * Throws `WireEscapeError` on any rejection or any underlying
 * `escapeWire` failure (e.g. unsupported leaf type, missing
 * encode-opt callback).
 */
export function escapeInvokeArgs(
	args: readonly InvokeArgValue[],
	kwargs: { readonly [key: string]: InvokeArgValue } | undefined,
	opts: EscapeOptions,
): { args: WireValue[]; kwargs: { [key: string]: WireValue } } {
	for (let i = 0; i < args.length; i++) {
		const arg = args[i] as InvokeArgValue;
		assertInvokeArgValueAllowed(arg, `args[${i}]`);
	}
	if (kwargs !== undefined) {
		for (const key of Object.keys(kwargs)) {
			if (typeof key !== "string") {
				throw new WireEscapeError(
					`escapeInvokeArgs: kwargs keys must be strings; got ${String(key)}`,
				);
			}
			const value = kwargs[key] as InvokeArgValue;
			assertInvokeArgValueAllowed(value, `kwargs.${key}`);
		}
	}
	const encodedArgs: WireValue[] = [];
	for (const arg of args) encodedArgs.push(escapeWire(arg, opts));
	const encodedKwargs: { [key: string]: WireValue } = {};
	if (kwargs !== undefined) {
		for (const key of Object.keys(kwargs)) {
			const value = kwargs[key] as InvokeArgValue;
			encodedKwargs[key] = escapeWire(value, opts);
		}
	}
	return { args: encodedArgs, kwargs: encodedKwargs };
}

/**
 * Walk an invoke arg value and throw `WireEscapeError` if any leaf
 * is an `OpResultStub` or `CallbackInboxStub`. `CallbackHandleStub`
 * is allowed. Used by `escapeInvokeArgs` to surface invalid arg
 * leaves with a path pointing at the offending location rather than
 * letting `escapeWire` throw a generic message.
 *
 * Typed loose as `unknown` (and cast inside) because the type
 * system's `InvokeArgValue` should already exclude the forbidden
 * stubs at call sites — this is defense-in-depth, primarily catching
 * callers who bypass TS with `as InvokeArgValue` or who feed values
 * from untyped sources.
 */
function assertInvokeArgValueAllowed(value: unknown, path: string): void {
	if (value === null || typeof value !== "object") return;
	if (value instanceof Uint8Array) return;
	if (value instanceof EntityRefStub || value instanceof ServiceRefStub) return;
	if (value instanceof CallbackHandleStub) return;
	if (value instanceof OpResultStub) {
		throw new WireEscapeError(
			`escapeInvokeArgs: OpResultStub is not allowed in invoke args (at ${path}); read the slot's result and pass the resolved value instead.`,
		);
	}
	if (value instanceof CallbackInboxStub) {
		throw new WireEscapeError(
			`escapeInvokeArgs: CallbackInboxStub is not allowed in invoke args (at ${path}); pass the send-side handle via \`inbox.handle\` instead.`,
		);
	}
	if (Array.isArray(value)) {
		for (let i = 0; i < value.length; i++) {
			assertInvokeArgValueAllowed(value[i], `${path}[${i}]`);
		}
		return;
	}
	const obj = value as { readonly [key: string]: unknown };
	for (const key of Object.keys(obj)) {
		assertInvokeArgValueAllowed(obj[key], `${path}.${key}`);
	}
}

/** Short preview of a value for error messages. Truncates to ~80 chars. */
function jsonPreview(value: unknown): string {
	let s: string;
	try {
		s = JSON.stringify(value);
	} catch {
		s = String(value);
	}
	if (s === undefined) s = String(value);
	if (s.length > 80) return `${s.slice(0, 77)}...`;
	return s;
}
