/**
 * React content rendered into each chip widget's DOM placeholder
 * via portal. Reads ``useRefStubResolver()`` and forwards to
 * ``<RefLink as="inline">`` so the chip inherits all the shell's
 * link interactivity (click → navigate, hover preview, context
 * menu, drag-out). When the shortId can't be resolved we render
 * a muted literal ``@[shortid]`` so the user sees that the
 * reference is unresolved.
 *
 * The resolved chip is wrapped in a styled ``<span>`` with a
 * rounded border + subtle background tint so it visually reads as
 * a discrete object embedded in the surrounding text — important
 * because the dispatched ``InlineLink`` content can otherwise look
 * a lot like plain prose (some link renderers are just colored
 * text). The wrapper has ``display: inline-flex`` + a small inline
 * margin so adjacent chips don't sit flush against each other.
 */

import { RefLink, useRefStubResolver } from "@fxtr-ui/workstation-dispatch";
import type { CSSProperties } from "react";

const chipWrapperStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	padding: "0 4px",
	margin: "0 2px",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input, 4px)",
	background: "var(--surface-raised)",
	verticalAlign: "baseline",
	lineHeight: 1.2,
};

const unresolvedStyle: CSSProperties = {
	...chipWrapperStyle,
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	border: "1px dashed var(--border-subtle)",
	background: "transparent",
};

export function ChipContent({ shortId }: { readonly shortId: string }) {
	const resolver = useRefStubResolver();
	if (resolver === null) {
		return <span style={unresolvedStyle}>@[{shortId}]</span>;
	}
	const kind = resolver.classifyShortId(shortId);
	if (kind === "entity") {
		return (
			<span style={chipWrapperStyle}>
				<RefLink as="inline" stub={resolver.entityRef(shortId)} />
			</span>
		);
	}
	if (kind === "service") {
		return (
			<span style={chipWrapperStyle}>
				<RefLink as="inline" stub={resolver.serviceRef(shortId)} />
			</span>
		);
	}
	return <span style={unresolvedStyle}>@[{shortId}]</span>;
}
