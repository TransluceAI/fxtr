/**
 * Rendered when the dispatched card throws during render —
 * either a real bug in the rendered component, or the
 * synthetic throw from the static helpers' `errored` terminal
 * phase (the state machine couldn't resolve a renderer; the
 * helper's `ErrorThrower` raises so this fallback can show the
 * error message).
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";

export function CardErrorFallback({
	stub,
	error,
}: {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly error: unknown;
}) {
	const message = error instanceof Error ? error.message : String(error);
	return (
		<div
			style={{
				border: "1px solid #e5b5b5",
				borderRadius: "6px",
				padding: "10px 12px",
				background: "#fff5f5",
				color: "#600",
				display: "flex",
				flexDirection: "column",
				gap: "4px",
			}}
		>
			<div style={{ fontSize: "10px", color: "#a55", fontFamily: "monospace" }}>
				! · {stub.shortId}
			</div>
			<div style={{ fontSize: "12px" }}>{message}</div>
		</div>
	);
}
