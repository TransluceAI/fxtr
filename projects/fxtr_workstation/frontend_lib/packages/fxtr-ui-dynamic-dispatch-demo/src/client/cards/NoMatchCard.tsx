import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";

export function NoMatchCard(props: { stub: EntityRefStub | ServiceRefStub }) {
	return (
		<div
			style={{
				padding: 12,
				borderRadius: 8,
				background: "#fff5f5",
				border: "1px dashed #d88",
				color: "#a44",
			}}
		>
			no renderer for{" "}
			<code style={{ fontFamily: "ui-monospace, monospace" }}>{props.stub.shortId}</code>
		</div>
	);
}
