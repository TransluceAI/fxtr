import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";

export function CardErrorFallback(props: {
	stub: EntityRefStub | ServiceRefStub;
	error: unknown;
}) {
	const message = props.error instanceof Error ? props.error.message : String(props.error);
	return (
		<div
			style={{
				padding: 12,
				borderRadius: 8,
				background: "#fff0f0",
				border: "1px solid #c44",
				color: "#a00",
			}}
		>
			<div style={{ fontWeight: 600, marginBottom: 4 }}>render error</div>
			<div style={{ fontSize: 11 }}>
				<code style={{ fontFamily: "ui-monospace, monospace" }}>{props.stub.shortId}</code>:{" "}
				{message}
			</div>
		</div>
	);
}
