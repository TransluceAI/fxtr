/**
 * Default service-side FullPanel fallback. Rendered as a dockview
 * panel body when a ServiceRefStub is dispatched at `FullPanel`
 * but no consumer-supplied renderer is registered for its type.
 *
 * Shows the short id + "no renderer configured" message, then
 * delegates to `ServiceMetadataView` for the metadata + `$fxtr.ctx`
 * / `$fxtr.type` invoke-result display. Per-mount slot state rides
 * in the dispatched `ownStorage` atom (the metadata view's own
 * storage shape — `{ metadata, ctxInvoke, typeInvoke }`).
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";
import type { FullPanelProps } from "../dispatch/keys.js";
import { ServiceMetadataBody } from "../metadata/ServiceMetadataView.js";

export type ServiceFullPanelFallbackProps = { stub: ServiceRefStub } & FullPanelProps;

export function ServiceFullPanelFallback(props: ServiceFullPanelFallbackProps) {
	const { stub, ownStorage } = props;
	return (
		<div className="fw-panel__body" style={body}>
			<div style={heading}>Service</div>
			<div style={shortIdStyle}>{stub.shortId}</div>
			<div style={message}>renderer not configured for this service type</div>
			<ServiceMetadataBody stub={stub} storage={ownStorage} />
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
const message: CSSProperties = {
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-muted)",
};
