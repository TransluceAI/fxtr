/**
 * Pure-function builder for the ``send`` invoke argument.
 *
 * Extracted from ``<SendBox>``'s onSend callback so it can be unit-
 * tested without spinning up the React editor + jotai store.
 *
 * - No ref tokens in the draft → return the bare string (backwards
 *   compatible with peers that pre-date the chip feature).
 * - One or more ref tokens → return ``{message, attached}`` where
 *   ``attached`` is the ordered list of connector-interned
 *   ``EntityRefStub`` / ``ServiceRefStub`` for every matched
 *   shortId. Unknown shortIds are dropped from ``attached`` and
 *   ``console.warn``ed (their literal text stays in ``message``).
 * - Null resolver (used outside a shell) → always return the bare
 *   string. The literal ``@[...]`` text rides through unchanged.
 */

import type { EntityRefStub, InvokeArgValue, ServiceRefStub } from "@fxtr-ui/core";
import type { RefStubResolver } from "@fxtr-ui/workstation-dispatch";

import { collectShortIds } from "./parse_refs.js";

export function buildSendPayload(draft: string, resolver: RefStubResolver | null): InvokeArgValue {
	if (resolver === null) {
		return draft;
	}
	const shortIds = collectShortIds(draft);
	const attached: (EntityRefStub | ServiceRefStub)[] = [];
	for (const shortId of shortIds) {
		const kind = resolver.classifyShortId(shortId);
		if (kind === "entity") {
			attached.push(resolver.entityRef(shortId));
		} else if (kind === "service") {
			attached.push(resolver.serviceRef(shortId));
		} else {
			console.warn("[fxtr-ui agent-plugin] unresolved shortId in send:", shortId);
		}
	}
	if (attached.length === 0) return draft;
	return { message: draft, attached } as unknown as InvokeArgValue;
}
