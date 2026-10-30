/**
 * Minimal renderer-facing parser for ``application/x-fxtr-ref`` drag
 * payloads.
 *
 * The shell's own drag-payload is a larger JSON shape (see
 * ``@fxtr-ui/workstation/reflink/drag.ts`` — ``LinkDragPayload`` /
 * ``SnapshotDragPayload``) carrying breadcrumb trails, chain
 * snapshots, etc. Renderer modules don't need any of that — they
 * just want to know "what (kind, shortId) is being dragged in here?".
 *
 * Both shell payload variants put the target ref under the same
 * ``target: {kind: "entity"|"service", shortId: string}`` field, so
 * this parser doesn't need to know which variant it's looking at —
 * it only reads ``parsed.target``.
 */

import { DRAG_MIME_FXTR_REF } from "./constants.js";

export interface RefDragData {
	readonly kind: "entity" | "service";
	readonly shortId: string;
}

/**
 * Parse a ``DataTransfer`` whose ``DRAG_MIME_FXTR_REF`` slot carries
 * a shell-format ref drag payload. Returns the bare ``{kind, shortId}``
 * of the dragged target, or ``null`` if the data transfer doesn't
 * carry a ref drag or the payload is malformed.
 *
 * Safe to call from a ``drop`` handler. Per the HTML spec, calling
 * ``getData`` on a ``dragover`` event throws / returns the empty
 * string on most browsers; consumers should use
 * ``dataTransfer.types.includes(DRAG_MIME_FXTR_REF)`` during
 * ``dragover`` (cheaper, doesn't require getData).
 */
export function parseRefDragData(dataTransfer: DataTransfer): RefDragData | null {
	const raw = dataTransfer.getData(DRAG_MIME_FXTR_REF);
	if (!raw) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	if (parsed === null || typeof parsed !== "object") return null;
	const target = (parsed as { target?: unknown }).target;
	if (target === null || typeof target !== "object") return null;
	const t = target as { kind?: unknown; shortId?: unknown };
	if (t.kind !== "entity" && t.kind !== "service") return null;
	if (typeof t.shortId !== "string" || t.shortId.length === 0) return null;
	return { kind: t.kind, shortId: t.shortId };
}
