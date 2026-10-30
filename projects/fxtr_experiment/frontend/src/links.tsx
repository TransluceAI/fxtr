/**
 * Link-slot renderers for the experiment-workflow capability service:
 * a minimal labeled pill (BlockLink / InlineLink) + a one-line hover
 * card. Deliberately basic — the FullPanel is the product.
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import type { DispatchedComponent } from "@fxtr-ui/react-dispatch";
import type { HoverPreviewProps, LinkProps } from "@fxtr-ui/workstation-dispatch";
import type { CSSProperties } from "react";

function Pill() {
	return (
		<span style={pillStyle}>
			<span style={pillTagStyle}>workflow</span>
			<span>experiment dashboard</span>
		</span>
	);
}

export const DashboardBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = () => <Pill />;
export const DashboardInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = () => <Pill />;
export const DashboardHoverPreview: DispatchedComponent<ServiceRefStub, HoverPreviewProps> = () => (
	<div style={hoverStyle}>
		the experiment workflow's user capability — open it in a panel for the dashboard
	</div>
);

const pillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	fontSize: 12,
};
const pillTagStyle: CSSProperties = { fontWeight: 600 };
const hoverStyle: CSSProperties = {
	padding: 8,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	fontSize: 12,
	maxWidth: 260,
};
