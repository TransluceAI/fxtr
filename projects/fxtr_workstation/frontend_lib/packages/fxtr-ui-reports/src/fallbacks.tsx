/**
 * Default fallback components for the `Figure` dispatch key.
 *
 * - `FigureLoadingFallback` — small skeleton shown while the
 *   dispatch state machine is still resolving the type's renderer.
 *
 * - `FigureNoMatchFallback` — the headline default behavior:
 *   wraps the entity's `FullPanel` rendering in a shaded container
 *   with a `RefLink` header. So even when no consumer has
 *   registered a custom figure renderer for a type, the figure
 *   *still* renders meaningfully (the user sees the entity's
 *   full body, framed as a figure).
 *
 * - `FigureErrorFallback` — terminal "render failed" state. Same
 *   shape as the workstation's `DispatchErrorFallback` but lives
 *   here so we can theme it as a figure-shaped block.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { FullPanel, RefLink } from "@fxtr-ui/workstation-dispatch";
import type { CSSProperties } from "react";
import { figureBodyStyle, figureHeaderStyle, figureShellStyle } from "./handlers/shared.js";
import type { FigureProps } from "./keys.js";

// ---- NoMatch — the headline fallback ----

// The embedded FullPanel can be arbitrarily tall; cap it so a
// figure never blows up its parent's flow. The user gets a
// browser-native resize grip in the corner via `resize: vertical`
// to grow the body when they need to see more at once.
const figureFallbackBodyStyle: CSSProperties = {
	...figureBodyStyle,
	maxHeight: "480px",
	overflow: "auto",
	resize: "vertical",
	padding: 0,
};

const figureFallbackInnerStyle: CSSProperties = {
	padding: "var(--sp-3, 12px)",
};

/**
 * Default fallback for the `Figure` key when no consumer
 * renderer is registered for the entity's type.
 *
 * Renders the entity's `FullPanel` dispatch inside a shaded
 * container, with a `RefLink` header naming the entity. Threads
 * the same `ownStorage` atom into the nested `FullPanel` so the
 * figure's storage slot is shared with what the FullPanel would
 * have used had it been opened in its own panel.
 *
 * The body has a max-height with overflow scrolling, plus
 * `resize: vertical` so the user can drag the bottom-right
 * corner to grow the figure when the content warrants it.
 */
export function FigureNoMatchFallback(
	props: { stub: EntityRefStub | ServiceRefStub } & FigureProps,
) {
	const { stub, ownStorage } = props;
	return (
		<div style={figureShellStyle}>
			<div style={figureHeaderStyle}>
				<span>figure</span>
				<RefLink stub={stub} as="inline" />
			</div>
			<div style={figureFallbackBodyStyle}>
				<div style={figureFallbackInnerStyle}>
					<RenderDispatch stub={stub} as={FullPanel} ownStorage={ownStorage} />
				</div>
			</div>
		</div>
	);
}

// ---- Loading ----

const skeletonStyle: CSSProperties = {
	...figureShellStyle,
	minHeight: "120px",
	alignItems: "center",
	justifyContent: "center",
	color: "var(--text-muted, #66707a)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm, 13px)",
	fontStyle: "italic",
};

export function FigureLoadingFallback(
	_props: { stub: EntityRefStub | ServiceRefStub } & FigureProps,
) {
	return <div style={skeletonStyle}>loading figure…</div>;
}

// ---- Error ----

const errorShell: CSSProperties = {
	...figureShellStyle,
	borderColor: "var(--status-error, #c00)",
	background: "var(--status-error-bg, #fff5f5)",
};

const errorHeading: CSSProperties = {
	fontWeight: 600,
	color: "var(--status-error, #c00)",
	fontFamily: "var(--font-prose)",
};

const errorDetail: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm, 13px)",
	color: "var(--text-secondary, #4a5560)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

export interface FigureErrorFallbackProps {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly error: Error;
}

export function FigureErrorFallback(props: FigureErrorFallbackProps) {
	const { stub, error } = props;
	const message = error.message || String(error);
	return (
		<div style={errorShell}>
			<div style={figureHeaderStyle}>
				<span>figure</span>
				<RefLink stub={stub} as="inline" />
			</div>
			<div style={errorHeading}>render failed</div>
			<div style={errorDetail}>{message}</div>
		</div>
	);
}
