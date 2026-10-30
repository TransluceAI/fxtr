/**
 * Shared chrome + style primitives used by every per-event-type
 * StreamEntry renderer.
 *
 * The owning panel renders the timestamp prefix + the per-entry
 * gap affordance (``Load more``); the actual card body comes from
 * the dispatched per-event-type renderer. We export a tiny set of
 * styled wrappers + helper components so the renderers stay
 * consistent without each re-defining its own card chrome.
 */

import {
	type CSSProperties,
	type ReactNode,
	type SyntheticEvent,
	useCallback,
	useState,
} from "react";

export const cardStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
	padding: "var(--sp-3) var(--sp-4)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-raised)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
};

export const cardHeaderStyle: CSSProperties = {
	display: "flex",
	gap: "var(--sp-2)",
	alignItems: "baseline",
};

export const cardTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
};

export const cardSubTagStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
};

export const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
};

export const errorTextStyle: CSSProperties = {
	color: "var(--text-danger, #b3261e)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

export const codeBlockStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
	padding: "var(--sp-2) var(--sp-3)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input)",
};

export const sectionLabelStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
};

/**
 * Header row with a short uppercase tag + an optional secondary
 * mono-font detail (e.g. ``slot_key:slot_n``).
 */
export function StreamCardHeader(props: {
	tag: string;
	detail?: ReactNode;
	tagStyle?: CSSProperties;
}) {
	return (
		<div style={cardHeaderStyle}>
			<span style={props.tagStyle ?? cardTagStyle}>{props.tag}</span>
			{props.detail !== undefined ? <span style={cardSubTagStyle}>{props.detail}</span> : null}
		</div>
	);
}

// ---------------------------------------------------------------------------
// CollapsibleCard — primitive used by every entry renderer
// ---------------------------------------------------------------------------

const collapsibleSummaryStyle: CSSProperties = {
	cursor: "pointer",
	display: "flex",
	alignItems: "baseline",
	gap: "var(--sp-2)",
	listStyle: "none",
};

const collapsibleTriangleStyle: CSSProperties = {
	display: "inline-block",
	width: 0,
	height: 0,
	borderLeft: "4px solid transparent",
	borderRight: "4px solid transparent",
	borderTop: "5px solid var(--text-muted)",
	transition: "transform 0.1s",
	transformOrigin: "center 30%",
	flex: "0 0 auto",
};

const collapsibleTriangleClosedStyle: CSSProperties = {
	transform: "rotate(-90deg)",
};

const collapsibleSummaryTextStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
	flex: 1,
	minWidth: 0,
};

const collapsibleBodyStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2)",
	marginTop: "var(--sp-2)",
};

// Per-key fallback styles for the emphasis variant. Picked up by the
// presented-to-user companion block on `script_execution` entries.
const cardStyleEmphasis: CSSProperties = {
	...cardStyle,
	border: "2px solid var(--text-accent, #2563eb)",
	background: "var(--surface-accent-soft, var(--surface-sunken))",
};

const cardTagStyleEmphasis: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.06em",
	fontWeight: 600,
	color: "var(--text-accent, #2563eb)",
};

/**
 * Props for the ``CollapsibleCard`` primitive.
 *
 * - ``tag`` / ``detail`` mirror ``StreamCardHeader``.
 * - ``summary`` is the one-line text shown after the tag while the
 *   card is collapsed; hidden once expanded.
 * - ``defaultOpen`` seeds the initial open / closed state; defaults
 *   to ``false`` (closed). Open state is component-local React
 *   state, not session-persisted.
 * - ``emphasis`` switches the card chrome to the accent-bordered
 *   variant — used by the ``script_execution`` "presented to user"
 *   companion block.
 * - ``tagStyle`` overrides the tag span's style (e.g. red text for
 *   ``invoke error``). Takes precedence over ``emphasis``'s tag
 *   styling.
 */
export interface CollapsibleCardProps {
	readonly tag: string;
	readonly detail?: ReactNode;
	readonly summary?: string;
	readonly defaultOpen?: boolean;
	readonly emphasis?: boolean;
	readonly tagStyle?: CSSProperties;
	readonly children: ReactNode;
}

/**
 * Collapsible card wrapper used by every entry renderer. Uses
 * native ``<details>`` / ``<summary>`` for keyboard / a11y
 * semantics; the triangle and "click anywhere on the row to
 * toggle" affordance are custom CSS on top.
 *
 * The summary text appears inline only while the card is closed;
 * once open, the body's own sections speak for themselves.
 */
export function CollapsibleCard(props: CollapsibleCardProps) {
	const {
		tag,
		detail,
		summary,
		defaultOpen = false,
		emphasis = false,
		tagStyle: tagStyleOverride,
		children,
	} = props;
	const [open, setOpen] = useState(defaultOpen);
	const onToggle = useCallback((e: SyntheticEvent<HTMLDetailsElement>) => {
		setOpen(e.currentTarget.open);
	}, []);
	const triangleStyle = open
		? collapsibleTriangleStyle
		: { ...collapsibleTriangleStyle, ...collapsibleTriangleClosedStyle };
	const tagStyle = tagStyleOverride ?? (emphasis ? cardTagStyleEmphasis : cardTagStyle);
	const cardChrome = emphasis ? cardStyleEmphasis : cardStyle;
	return (
		<details open={open} onToggle={onToggle} style={cardChrome}>
			<summary style={collapsibleSummaryStyle}>
				<span style={triangleStyle} aria-hidden="true" />
				<span style={tagStyle}>{tag}</span>
				{detail !== undefined ? <span style={cardSubTagStyle}>{detail}</span> : null}
				{!open && summary !== undefined && summary.length > 0 ? (
					<span style={collapsibleSummaryTextStyle}>{summary}</span>
				) : null}
			</summary>
			<div style={collapsibleBodyStyle}>{children}</div>
		</details>
	);
}

/**
 * Format a unix-seconds timestamp as a short ``HH:MM:SS`` string
 * for the stream's row prefix. Returns an empty string when the
 * timestamp is non-finite so the row prefix can collapse cleanly.
 */
export function formatTimestamp(ts: number): string {
	if (!Number.isFinite(ts)) return "";
	const d = new Date(ts * 1000);
	const hh = d.getHours().toString().padStart(2, "0");
	const mm = d.getMinutes().toString().padStart(2, "0");
	const ss = d.getSeconds().toString().padStart(2, "0");
	return `${hh}:${mm}:${ss}`;
}
