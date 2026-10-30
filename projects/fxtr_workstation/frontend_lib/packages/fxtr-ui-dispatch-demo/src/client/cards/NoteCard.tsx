/**
 * Rendered when the dispatcher resolves a stub as the demo's
 * `Note` entity type. Loads the entity's data via the
 * `makeLoadResultAtom` pattern, shows its `title` + `body`.
 */

import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import { useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { makeLoadResultAtom } from "@fxtr-ui/state-util";
import { useAtomValue } from "jotai";
import { useMemo } from "react";

interface NoteData {
	readonly title: string;
	readonly body: string;
	// `$fxtr.type` field also present on the wire but ignored
	// here — the dispatcher already used it to route us.
}

export function NoteCard({ stub }: { readonly stub: EntityRefStub }) {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<NoteData> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<NoteData>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const load = useAtomValue(resultAtom);
	return (
		<div
			style={{
				border: "1px solid #c5d5e5",
				borderRadius: "6px",
				padding: "10px 12px",
				background: "#f5faff",
				display: "flex",
				flexDirection: "column",
				gap: "4px",
			}}
		>
			<div style={{ fontSize: "10px", color: "#558", fontFamily: "monospace" }}>
				Note · {stub.shortId}
			</div>
			{load.kind === "filled" ? (
				<>
					<div style={{ fontWeight: 600, fontSize: "14px" }}>{load.value.title}</div>
					<div style={{ color: "#333" }}>{load.value.body}</div>
				</>
			) : load.kind === "errored" ? (
				<div style={{ color: "#c00" }}>load failed: {load.message}</div>
			) : (
				<div style={{ color: "#888", fontStyle: "italic" }}>loading body…</div>
			)}
		</div>
	);
}
