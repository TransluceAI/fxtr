import type { EntityRefStub } from "@fxtr-ui/core";

export function BuiltinCard(props: { stub: EntityRefStub }) {
	return (
		<div
			style={{
				padding: 8,
				borderRadius: 6,
				background: "#f0f0ff",
				border: "1px solid #bbc",
				color: "#449",
				fontSize: 11,
			}}
		>
			builtin entity:{" "}
			<code style={{ fontFamily: "ui-monospace, monospace" }}>{props.stub.shortId}</code>
		</div>
	);
}
