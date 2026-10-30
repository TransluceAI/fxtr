/**
 * Rendered for builtin / anonymous entities — those whose
 * shortId starts with `.` (anonymous-node tag) or `!` (builtin
 * tag). These carry no loadable content; the static entity
 * dispatcher short-circuits to this without attempting a load.
 */

import type { EntityRefStub } from "@fxtr-ui/core";

export function BuiltinCard({ stub }: { readonly stub: EntityRefStub }) {
	return (
		<div
			style={{
				border: "1px solid #ccc",
				borderRadius: "6px",
				padding: "8px 10px",
				background: "#fffbe6",
				color: "#665",
				display: "flex",
				flexDirection: "column",
				gap: "2px",
				fontSize: "11px",
			}}
		>
			<div style={{ fontFamily: "monospace", color: "#888" }}>builtin</div>
			<div style={{ fontFamily: "monospace" }}>{stub.shortId}</div>
		</div>
	);
}
