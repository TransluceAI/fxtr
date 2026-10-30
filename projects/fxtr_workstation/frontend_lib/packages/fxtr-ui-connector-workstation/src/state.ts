/**
 * Structural state walks for the workstation reducer: collect stubs
 * (for pin diffing + preserve-events derivation) and diff two trees
 * into a JSON Patch (for the `MutateSessionRequest` wire form).
 *
 * Both walks treat the structured state as the `StateValue`
 * union from `@fxtr-ui/core`: containers (object / array),
 * primitives, `Uint8Array`, and stub class instances at leaves.
 *
 * The walks are kept simple and per-mutation. We re-encode the
 * affected subtrees during diffing rather than persisting a wire form
 * alongside the structured form; trees are small enough that the
 * extra work doesn't matter, and the simplicity buys us less state to
 * keep coherent across mutation / rollback.
 *
 * `collectStubsInState` surfaces entity / service short_ids (for
 * Track/Untrack diffs) and op-result slot integers (for
 * Preserve/Unpreserve diffs + the `committedSlotSet` index). The
 * dead-sentinel (`slot === 0`) is excluded from the collected slot
 * set — it carries no semantic identity beyond "this is broken." See
 * `stage2_entity_loading.md` and
 * `design_notes/04_session_state_layout.md`.
 */

import {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	type EscapeOptions,
	OpResultStub,
	ServiceRefStub,
	type StateValue,
	type WireValue,
	escapeWire,
} from "@fxtr-ui/core";
import { WorkstationCallbackInboxStub, WorkstationOpResultStub } from "./stubs";

/**
 * RFC 6902 JSON Patch operation. Only `add` / `remove` / `replace`
 * are produced by the reducer; `move` / `copy` / `test` are defined
 * by the RFC but unused here.
 *
 * `path` is an RFC 6901 JSON Pointer (`/foo/bar/0`). `value` is a
 * wire-escaped value.
 */
export type JsonPatchOp =
	| { readonly op: "add"; readonly path: string; readonly value: WireValue }
	| { readonly op: "remove"; readonly path: string }
	| { readonly op: "replace"; readonly path: string; readonly value: WireValue };

/**
 * Set of stub identities collected from a structured state walk.
 *
 * - `entityShortIds` / `serviceShortIds` — short_ids of all
 *   `EntityRefStub` / `ServiceRefStub` instances. Used by the
 *   reducer to derive `Track*Op` / `Untrack*Op` diffs.
 * - `opResultSlots` — slot integers of all
 *   `WorkstationOpResultStub` instances (excluding the
 *   `slot === 0` dead-sentinel — it has no semantic identity in
 *   preserve-set / committed-slot-set derivations).
 * - `inboxSlotOffsets` — for each callback slot referenced by
 *   `WorkstationCallbackInboxStub` instances, the **minimum
 *   offset** observed across all stubs for that slot. The
 *   `slot === 0` dead-inbox is excluded. The min-offset is what
 *   the chunk-3 preserve diff uses to compute "preserve callback
 *   events from index `min_offset` onward"; the keyset is what
 *   chunk 2's `computeOps` uses to derive
 *   `CreateCallback` / `CloseCallback` diffs.
 */
export interface CollectedStubs {
	readonly entityShortIds: ReadonlySet<string>;
	readonly serviceShortIds: ReadonlySet<string>;
	readonly opResultSlots: ReadonlySet<number>;
	readonly inboxSlotOffsets: ReadonlyMap<number, number>;
}

/**
 * Walk a structured-state tree once and collect every stub's
 * identity. Containers recurse; entity / service stub leaves
 * contribute their `shortId`; `WorkstationOpResultStub` leaves
 * contribute their `slot` (except the dead-sentinel at
 * `slot === 0`); `WorkstationCallbackInboxStub` leaves contribute
 * `(slot, offset)` aggregated to per-slot min-offset (except the
 * inbox dead-sentinel at `slot === 0`); primitives / bytes are
 * no-ops.
 */
export function collectStubsInState(value: StateValue): CollectedStubs {
	const entityShortIds = new Set<string>();
	const serviceShortIds = new Set<string>();
	const opResultSlots = new Set<number>();
	const inboxSlotOffsets = new Map<number, number>();
	walkCollect(value, entityShortIds, serviceShortIds, opResultSlots, inboxSlotOffsets);
	return { entityShortIds, serviceShortIds, opResultSlots, inboxSlotOffsets };
}

function walkCollect(
	value: StateValue,
	entityShortIds: Set<string>,
	serviceShortIds: Set<string>,
	opResultSlots: Set<number>,
	inboxSlotOffsets: Map<number, number>,
): void {
	if (value === null || value === undefined) return;
	if (typeof value !== "object") return;
	if (value instanceof Uint8Array) return;
	if (value instanceof EntityRefStub) {
		entityShortIds.add(value.shortId);
		return;
	}
	if (value instanceof ServiceRefStub) {
		serviceShortIds.add(value.shortId);
		return;
	}
	if (value instanceof WorkstationOpResultStub) {
		if (value.slot !== 0) opResultSlots.add(value.slot);
		return;
	}
	if (value instanceof WorkstationCallbackInboxStub) {
		if (value.slot !== 0) {
			const prev = inboxSlotOffsets.get(value.slot);
			if (prev === undefined || value.offset < prev) {
				inboxSlotOffsets.set(value.slot, value.offset);
			}
		}
		return;
	}
	if (value instanceof OpResultStub || value instanceof CallbackInboxStub) {
		// Foreign-subclass stubs we don't recognize. Shouldn't occur in
		// practice — only the workstation concrete classes live in
		// this connector's state. Pass through as inert leaves.
		return;
	}
	if (Array.isArray(value)) {
		for (const item of value) {
			walkCollect(item, entityShortIds, serviceShortIds, opResultSlots, inboxSlotOffsets);
		}
		return;
	}
	for (const key of Object.keys(value)) {
		const v = (value as Record<string, StateValue | undefined>)[key];
		// Skip explicit-undefined fields; treat them as absent. See the
		// matching comment in `escapeWire`'s object branch.
		if (v === undefined) continue;
		walkCollect(v, entityShortIds, serviceShortIds, opResultSlots, inboxSlotOffsets);
	}
}

/**
 * Compute a JSON Patch (RFC 6902) representing the changes needed to
 * transform `prev` into `next`. Patch values are wire-escaped via
 * `opts` (so they're ready to ship inside `MutateSessionRequest`).
 *
 * Emits only `add` / `remove` / `replace`. Array diffs are naive:
 * any difference within shared indices recurses, and length
 * differences emit per-index `add` (for `-` append) or `remove` from
 * the tail. Object diffs walk the union of keys.
 *
 * Stub equality: entity / service refs compare by `shortId`;
 * `WorkstationOpResultStub` compares by `slot` integer (the
 * session interns by slot, so identity-equality holds in practice,
 * but slot-equality is the wire-level invariant);
 * `CallbackInboxStub` instances compare by reference. Two distinct
 * `Uint8Array`s compare by byte content. Otherwise we fall back to
 * strict equality.
 *
 * TODO: a future optimization could fuse this with
 * `collectStubsInState` into a single walk that emits patches and
 * collects refs at once. Kept separate today because they have
 * different shapes and trees are tiny.
 */
export function diffStateToPatch(
	prev: StateValue,
	next: StateValue,
	opts: EscapeOptions,
): JsonPatchOp[] {
	const ops: JsonPatchOp[] = [];
	diffWalk(prev, next, [], ops, opts);
	return ops;
}

function diffWalk(
	prev: StateValue,
	next: StateValue,
	path: readonly string[],
	ops: JsonPatchOp[],
	opts: EscapeOptions,
): void {
	if (leafEqual(prev, next)) return;

	const prevContainer = isPlainContainer(prev);
	const nextContainer = isPlainContainer(next);
	if (!prevContainer || !nextContainer || Array.isArray(prev) !== Array.isArray(next)) {
		ops.push({ op: "replace", path: jsonPointer(path), value: escapeWire(next, opts) });
		return;
	}

	if (Array.isArray(prev) && Array.isArray(next)) {
		// The wire form wraps literal arrays as `[[items…]]` (the
		// list-escape codec's single-element-of-list rule). JSON Patch
		// paths target the wire form server-side, so descending into
		// the array's elements requires a `/0` segment to step past the
		// wrap before indexing. Without this, `add /foo/-` would append
		// at the OUTER (length-1) array, producing `[[a], b]` instead
		// of `[[a, b]]` and breaking the wrap invariant.
		const arrayPath = [...path, "0"];
		const minLen = Math.min(prev.length, next.length);
		for (let i = 0; i < minLen; i++) {
			diffWalk(prev[i] as StateValue, next[i] as StateValue, [...arrayPath, String(i)], ops, opts);
		}
		if (next.length > prev.length) {
			for (let i = prev.length; i < next.length; i++) {
				ops.push({
					op: "add",
					path: jsonPointer([...arrayPath, "-"]),
					value: escapeWire(next[i] as StateValue, opts),
				});
			}
		} else if (prev.length > next.length) {
			// Remove from the tail; indices stay valid because each
			// removal shrinks the array.
			for (let i = prev.length - 1; i >= next.length; i--) {
				ops.push({ op: "remove", path: jsonPointer([...arrayPath, String(i)]) });
			}
		}
		return;
	}

	// Both are plain objects. Treat explicit-`undefined` values as
	// absent (same convention as `escapeWire` / `walkCollect`) so a
	// field that goes from `"foo"` to `undefined` diffs as a remove
	// rather than diffing into the value itself.
	const prevObj = prev as Record<string, StateValue | undefined>;
	const nextObj = next as Record<string, StateValue | undefined>;
	const present = (obj: Record<string, StateValue | undefined>, key: string): boolean =>
		key in obj && obj[key] !== undefined;
	const keys = new Set<string>([...Object.keys(prevObj), ...Object.keys(nextObj)]);
	for (const key of keys) {
		const inPrev = present(prevObj, key);
		const inNext = present(nextObj, key);
		if (!inPrev && !inNext) {
			// Both sides explicit-undefined → no diff.
			continue;
		}
		if (!inPrev) {
			ops.push({
				op: "add",
				path: jsonPointer([...path, key]),
				value: escapeWire(nextObj[key] as StateValue, opts),
			});
		} else if (!inNext) {
			ops.push({ op: "remove", path: jsonPointer([...path, key]) });
		} else {
			diffWalk(prevObj[key] as StateValue, nextObj[key] as StateValue, [...path, key], ops, opts);
		}
	}
}

function leafEqual(a: StateValue, b: StateValue): boolean {
	if (a === b) return true;
	if (a instanceof EntityRefStub && b instanceof EntityRefStub) {
		return a.shortId === b.shortId;
	}
	if (a instanceof ServiceRefStub && b instanceof ServiceRefStub) {
		return a.shortId === b.shortId;
	}
	if (a instanceof WorkstationOpResultStub && b instanceof WorkstationOpResultStub) {
		// Compare by slot integer. Session interning normally gives
		// identity equality for the same slot (caught by `a === b`
		// above), but the diff walk is asked to treat any two stubs
		// with the same slot as the same value — the slot is the
		// only identity that matters on the wire.
		return a.slot === b.slot;
	}
	if (a instanceof WorkstationCallbackInboxStub && b instanceof WorkstationCallbackInboxStub) {
		// Cursor identity is `(slot, offset)` — two stubs with the same
		// slot but different offsets diff as different (the wire form
		// `["inbox", slot, offset]` carries both). No interning, so
		// fresh decodes / `sliced()` mints always need this structural
		// comparison.
		return a.slot === b.slot && a.offset === b.offset;
	}
	// Other `OpResultStub` / `CallbackInboxStub` subclasses (foreign
	// connectors) have no public slot surface for us to inspect; fall
	// back to identity-only (caught above). Distinct instances are
	// treated as different.
	if (a instanceof Uint8Array && b instanceof Uint8Array) {
		if (a.byteLength !== b.byteLength) return false;
		for (let i = 0; i < a.byteLength; i++) {
			if (a[i] !== b[i]) return false;
		}
		return true;
	}
	return false;
}

function isPlainContainer(
	value: StateValue,
): value is readonly StateValue[] | Readonly<Record<string, StateValue>> {
	if (value === null || typeof value !== "object") return false;
	if (value instanceof Uint8Array) return false;
	if (value instanceof EntityRefStub) return false;
	if (value instanceof ServiceRefStub) return false;
	if (value instanceof OpResultStub) return false;
	if (value instanceof CallbackInboxStub) return false;
	if (value instanceof CallbackHandleStub) return false;
	return true;
}

/** RFC 6901 JSON Pointer for a path. Escapes `~` and `/` per spec. */
function jsonPointer(path: readonly string[]): string {
	if (path.length === 0) return "";
	let out = "";
	for (const part of path) {
		out += "/";
		// Order matters: `~` first, then `/`. Otherwise an escaped `/`
		// (`~1`) would have its `~` re-escaped.
		out += part.replace(/~/g, "~0").replace(/\//g, "~1");
	}
	return out;
}
