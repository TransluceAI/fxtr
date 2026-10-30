/**
 * Renderer for the `echo` GET service type — a FullPanel that lets you
 * type a value and invoke `echo(value)` over the connector, showing the
 * result. Demonstrates dispatching on a *service* type (the dispatcher
 * discovers it via the service's `$fxtr.type` method) and driving an
 * invoke from inside a dynamically-loaded renderer:
 * `ownStorage`'s function-form setter hands us a `ctx` whose
 * `allocateInvoke(serviceStub, method, args)` returns an `OpResultStub`
 * we observe via `.result`.
 *
 * Compiled to ESM at backend startup; default export is the 2-level map
 * `[subtype_key][dispatch_key_uuid] → Component`.
 */

import type { FxtrSessionAtom, OpResultStub, ResultStatus, ServiceRefStub } from "@fxtr-ui/core";
import { FullPanel } from "@fxtr-ui/workstation-dispatch";
import { useAtom, useAtomValue } from "jotai";
import { useState } from "react";

type EchoState = { readonly slot: OpResultStub<unknown>; readonly sent: string } | null;

interface ServiceRendererProps {
	readonly stub: ServiceRefStub;
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

function EchoPanel({ stub, ownStorage }: ServiceRendererProps) {
	const [state, setState] = useAtom(ownStorage as FxtrSessionAtom<EchoState>);
	const [draft, setDraft] = useState("hello from the echo renderer");

	const onInvoke = (): void => {
		setState((_prev, ctx) => ({ slot: ctx.allocateInvoke(stub, "echo", [draft]), sent: draft }));
	};

	return (
		<div style={panelStyle}>
			<h2 style={{ margin: 0, fontSize: 18 }}>echo service</h2>
			<p style={mutedStyle}>
				A GET service invoked from a dynamically-loaded renderer. Type a value and invoke
				<code style={codeStyle}> echo(value)</code> — the connector issues the request and the
				result fills the slot below.
			</p>
			<div style={{ display: "flex", gap: 8 }}>
				<input
					value={draft}
					onChange={(e) => setDraft(e.target.value)}
					style={inputStyle}
					placeholder="value to echo"
				/>
				<button type="button" onClick={onInvoke} style={btnStyle}>
					Invoke echo
				</button>
			</div>
			{state !== null ? <ResultLine slot={state.slot} sent={state.sent} /> : null}
		</div>
	);
}

function ResultLine({ slot, sent }: { readonly slot: OpResultStub<unknown>; readonly sent: string }) {
	const result = useAtomValue(slot.result) as ResultStatus<unknown>;
	if (result.status === "pending") {
		return <div style={resultStyle}>…invoking</div>;
	}
	if (result.status === "errored") {
		return <div style={{ ...resultStyle, color: "#b00020" }}>error: {result.message}</div>;
	}
	return (
		<div style={resultStyle}>
			<code style={codeStyle}>echo({JSON.stringify(sent)})</code> → {JSON.stringify(result.value)}
		</div>
	);
}

const panelStyle = {
	padding: 16,
	height: "100%",
	overflow: "auto",
	display: "flex",
	flexDirection: "column" as const,
	gap: 12,
	color: "#234",
};
const mutedStyle = { margin: 0, fontSize: 12, color: "#789", lineHeight: 1.5 };
const inputStyle = { flex: 1, padding: "4px 8px", border: "1px solid #bcd", borderRadius: 6, fontSize: 13 };
const btnStyle = {
	padding: "4px 12px",
	border: "1px solid #5a8",
	borderRadius: 6,
	background: "#eafaf2",
	color: "#163",
	cursor: "pointer",
	fontSize: 13,
};
const resultStyle = {
	padding: "8px 10px",
	borderRadius: 6,
	background: "#f4f8ff",
	border: "1px solid #d6e4ff",
	fontSize: 13,
};
const codeStyle = { fontFamily: "ui-monospace, monospace", fontSize: 12 };

export default {
	default: {
		[FullPanel.uuid]: EchoPanel,
	},
};
