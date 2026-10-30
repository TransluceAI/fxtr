/**
 * Generic fallback renderer for entities with no specific handler.
 */

import type { FxtrID } from "@fxtr/core";

export function GenericFallback({ entityId }: { entityId: FxtrID }) {
	return (
		<div
			style={{
				border: "1px dashed #ccc",
				padding: "12px",
				borderRadius: "4px",
				color: "#666",
			}}
		>
			No renderer for entity: <code>{entityId.idString()}</code>
		</div>
	);
}
