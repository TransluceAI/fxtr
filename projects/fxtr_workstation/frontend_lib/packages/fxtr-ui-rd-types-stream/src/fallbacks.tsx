/**
 * Default fallback components for the :data:`StreamEntry` dispatch
 * key. Rendered by a dispatcher when a stream entry is mid-resolve
 * (Loading), has no registered renderer for its type (NoMatch), or
 * the resolved component threw during render (Error).
 *
 * Visual intent: every fallback uses the same outer card chrome
 * (rounded corners, subtle border, padded body) as a typical entry
 * renderer would, so a stream rendered with one or two unregistered
 * types stays visually coherent — the fallback cards sit alongside
 * the registered ones without breaking the rhythm.
 *
 * Style tokens follow the workstation-dispatch convention
 * (CSS custom properties like ``--sp-2`` / ``--border-subtle``);
 * consumers can re-theme by overriding those at any ancestor.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties } from "react";

const cardStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1)",
	padding: "var(--sp-3) var(--sp-4)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-raised)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
};
const tagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
};
const idStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	color: "var(--text-primary)",
};
const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
};
const errorTagStyle: CSSProperties = {
	...tagStyle,
	color: "var(--text-danger, #b3261e)",
};
const errorBodyStyle: CSSProperties = {
	color: "var(--text-danger, #b3261e)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

/**
 * Header row used by all three fallback cards: a small uppercase
 * tag + the stub's short id, so a stream of fallback cards still
 * conveys which ref each card stands for.
 */
function FallbackHeader(props: { tag: string; shortId: string; tagStyle?: CSSProperties }) {
	return (
		<div style={{ display: "flex", gap: "var(--sp-2)", alignItems: "baseline" }}>
			<span style={props.tagStyle ?? tagStyle}>{props.tag}</span>
			<span style={idStyle}>{props.shortId}</span>
		</div>
	);
}

/**
 * Stream-entry loading fallback. Shown while the dispatcher's
 * state machine is still resolving the entry's type-to-renderer
 * mapping (or while the resolved component is mounting).
 */
export function StreamEntryLoadingFallback(props: { stub: EntityRefStub | ServiceRefStub }) {
	return (
		<div style={cardStyle}>
			<FallbackHeader tag="loading" shortId={props.stub.shortId} />
			<div style={mutedStyle}>…</div>
		</div>
	);
}

/**
 * Stream-entry no-match fallback. Shown when the dispatcher's
 * resolution arrived at "no registered renderer for this type" —
 * the stub's type isn't covered by the consumer's registry / the
 * remote dispatch service had no answer. The card still surfaces
 * the stub identity so the operator can recognize it.
 */
export function StreamEntryNoMatchFallback(props: { stub: EntityRefStub | ServiceRefStub }) {
	return (
		<div style={cardStyle}>
			<FallbackHeader tag="stream entry" shortId={props.stub.shortId} />
			<div style={mutedStyle}>no renderer registered for this type</div>
		</div>
	);
}

/**
 * Stream-entry error fallback. Rendered by the dispatcher's
 * per-entry error boundary when the resolved component throws
 * during render. The thrown error's ``message`` is surfaced
 * verbatim so the operator can see what went wrong without
 * digging into devtools.
 */
export function StreamEntryErrorFallback(props: {
	stub: EntityRefStub | ServiceRefStub;
	error: unknown;
}) {
	const message =
		props.error instanceof Error
			? props.error.message
			: typeof props.error === "string"
				? props.error
				: String(props.error);
	return (
		<div style={cardStyle}>
			<FallbackHeader
				tag="stream entry error"
				shortId={props.stub.shortId}
				tagStyle={errorTagStyle}
			/>
			<div style={errorBodyStyle}>{message}</div>
		</div>
	);
}
