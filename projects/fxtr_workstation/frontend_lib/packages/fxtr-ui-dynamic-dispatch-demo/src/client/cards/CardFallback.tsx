import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";

export function CardFallback(props: { stub: EntityRefStub | ServiceRefStub }) {
	return (
		<div
			style={{
				padding: 12,
				borderRadius: 8,
				background: "#f5f5f5",
				border: "1px dashed #ccc",
				color: "#666",
				fontStyle: "italic",
			}}
		>
			loading… <span style={{ fontSize: 11, opacity: 0.7 }}>({props.stub.shortId})</span>
		</div>
	);
}
