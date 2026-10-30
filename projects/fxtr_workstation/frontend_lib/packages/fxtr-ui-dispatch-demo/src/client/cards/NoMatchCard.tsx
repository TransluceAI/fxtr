/**
 * Rendered when the static dispatcher reached `resolved` but
 * the resolved type-id-string has no entry in the registry's
 * `lookupMap` — i.e. the load + metadata round-trip succeeded
 * and we know what kind of entity / service this is, but we
 * don't have a renderer registered for that kind.
 *
 * Surfaces the resolved type-id-string via
 * `useDispatchProgressState` so it's easy to copy-paste into
 * the registry to add a renderer.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import {
	type StaticEntityDispatchState,
	type StaticServiceDispatchState,
	useDispatchProgressState,
} from "@fxtr-ui/react-dispatch";

export function NoMatchCard({
	stub,
}: {
	readonly stub: EntityRefStub | ServiceRefStub;
}) {
	const state = useDispatchProgressState<StaticEntityDispatchState | StaticServiceDispatchState>();
	const typeIdString = state.phase === "resolved" ? state.typeIdString : "(unknown)";
	return (
		<div
			style={{
				border: "1px solid #cce",
				borderRadius: "6px",
				padding: "8px 10px",
				background: "#f5f5fa",
				color: "#445",
				display: "flex",
				flexDirection: "column",
				gap: "3px",
				fontSize: "11px",
			}}
		>
			<div style={{ fontFamily: "monospace", color: "#88a" }}>no renderer · {stub.shortId}</div>
			<div style={{ fontFamily: "monospace", wordBreak: "break-all" }}>
				type id: <strong>{typeIdString}</strong>
			</div>
			<div style={{ color: "#666" }}>
				(resolved, but the dispatch registry's lookup map has no entry for this type)
			</div>
		</div>
	);
}
