/**
 * Minimal `FullPanel` body rendered while the dispatcher is still
 * resolving the service's type. Distinct from
 * `ServiceFullPanelFallback` (the no-match terminal): the loading
 * body deliberately does NOT touch `ownStorage`, so the real
 * renderer can mount with a clean slate once the dispatcher
 * resolves. Writing the no-match body's metadata allocations
 * during the loading phase would clobber the eventual matched
 * renderer's `ownStorage` shape.
 *
 * Reads the static-helper's progress message via
 * `useDispatchProgressMessage` so the user sees the current
 * phase ("resolving renderer…").
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import { useDispatchProgressMessage } from "@fxtr-ui/react-dispatch";
import type { CSSProperties } from "react";
import type { FullPanelProps } from "../dispatch/keys.js";

export function ServiceFullPanelLoadingFallback(props: { stub: ServiceRefStub } & FullPanelProps) {
	const message = useDispatchProgressMessage();
	return (
		<div className="fw-panel__body" style={body}>
			<div style={heading}>Service</div>
			<div style={shortIdStyle}>{props.stub.shortId}</div>
			<div style={message_}>{message || "loading…"}</div>
		</div>
	);
}

const body: CSSProperties = {
	padding: "var(--sp-3, 12px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
};
const heading: CSSProperties = {
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body)",
	fontWeight: 600,
	color: "var(--text-primary)",
};
const shortIdStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
};
const message_: CSSProperties = {
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-muted)",
};
