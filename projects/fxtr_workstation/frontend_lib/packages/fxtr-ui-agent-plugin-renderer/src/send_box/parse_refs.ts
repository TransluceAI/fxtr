/**
 * Token parser for the ``@[shortid]`` syntax shared between the
 * send-box editor (which decorates each match with a chip widget +
 * uses the list of shortIds to build the ``attached`` array at send
 * time) and the receive-side ``CallbackNotificationStreamEntry``
 * renderer (which splices inline ``<RefLink>`` instances into the
 * displayed text).
 *
 * The source of truth for a draft message is the editor's plain
 * text — copy / paste / undo all operate on the text directly, and
 * the tokens we emit here are simple regex matches over that text.
 * The same logic runs on the receive side so the rendered card and
 * the sent text agree.
 *
 * Shortid character class: ``[A-Za-z0-9._:-]``. Covers the
 * workstation's actual short_id charset (alphanumeric plus a few
 * separators) with room for future expansion; rejects whitespace
 * and special characters so e.g. ``@[]`` and ``@[ ]`` don't match.
 */

export const REF_TOKEN_RE = /@\[([A-Za-z0-9._:-]+)\]/g;

export type MessageToken =
	| { readonly kind: "text"; readonly value: string }
	| {
			readonly kind: "ref";
			readonly shortId: string;
			/** Inclusive start offset of the full ``@[shortid]`` token in the source string. */
			readonly start: number;
			/** Exclusive end offset of the full token (start + match length). */
			readonly end: number;
	  };

/**
 * Split ``doc`` into an ordered list of text + ref tokens. Adjacent
 * tokens are emitted as separate entries; empty text segments
 * between back-to-back refs are skipped (so the caller doesn't get
 * spurious empty ``text`` tokens).
 *
 * The regex uses the ``g`` flag and is consulted by re-creating its
 * ``lastIndex`` per call — pure-function semantics from the caller's
 * point of view.
 */
export function tokenizeMessage(doc: string): readonly MessageToken[] {
	const out: MessageToken[] = [];
	// Re-create a fresh regex per call so we don't share `lastIndex`
	// across callers. Cheaper than `new RegExp(...)` literally —
	// `re.exec` from a copied source.
	const re = new RegExp(REF_TOKEN_RE.source, "g");
	let cursor = 0;
	let m: RegExpExecArray | null;
	// biome-ignore lint/suspicious/noAssignInExpressions: idiomatic exec loop
	while ((m = re.exec(doc)) !== null) {
		const shortId = m[1];
		if (shortId === undefined) continue; // capture group always matches; satisfies the type
		const start = m.index;
		const end = start + m[0].length;
		if (start > cursor) {
			out.push({ kind: "text", value: doc.slice(cursor, start) });
		}
		out.push({ kind: "ref", shortId, start, end });
		cursor = end;
	}
	if (cursor < doc.length) {
		out.push({ kind: "text", value: doc.slice(cursor) });
	}
	return out;
}

/**
 * Walk ``doc`` and return every matched shortId in document order.
 * Duplicates are preserved (the same shortId can appear multiple
 * times in a message — the ``attached`` list mirrors the
 * occurrences).
 */
export function collectShortIds(doc: string): readonly string[] {
	const out: string[] = [];
	for (const tok of tokenizeMessage(doc)) {
		if (tok.kind === "ref") out.push(tok.shortId);
	}
	return out;
}
