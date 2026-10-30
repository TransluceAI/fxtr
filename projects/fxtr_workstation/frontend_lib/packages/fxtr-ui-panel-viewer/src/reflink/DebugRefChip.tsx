/**
 * DebugRefChip — inline chip for rendering a workstation short id
 * inside a debug surface (workstation debug panel, per-session
 * debug panel, etc.).
 *
 * Composes the same interaction hooks as `RefLink` (click, right-
 * click, drag) EXCEPT hover preview: hover dispatches through
 * `HoverPreview`, which would track the ref in the dispatcher
 * session — circular for the debug panels, which themselves
 * surface the dispatcher session as a row.
 *
 * The visible body is a fixed monospace pill with a kind tag
 * (`E:` / `S:`) and the short id. No dispatch on the at-rest
 * rendering — that's the load-bearing distinction from `RefLink`.
 *
 * Used from inside panels that do NOT provide `PanelContext`
 * (workstation debug, per-session debug). As a result the
 * interaction hooks see `usePanelContext() === null` and route
 * clicks through `restoreIntoNewPanel`; the chip therefore always
 * opens a new panel, never replaces in source.
 */

import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { useCallback } from "react";
import { type PanelViewEntry, entryKind } from "../types.js";
import { useDraggableRef } from "./useDraggableRef.js";
import { useRefClick } from "./useRefClick.js";
import { useRefContextMenu } from "./useRefContextMenu.js";

export interface DebugRefChipProps {
	readonly entry: PanelViewEntry;
}

const TITLE = "Click to open in new panel, right-click for menu, drag to open in new panel";

const CHIP_STYLE: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2, 6px)",
	padding: "var(--sp-1, 2px) var(--sp-2, 6px)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-sunken)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
	cursor: "pointer",
	userSelect: "none",
	lineHeight: 1.2,
	verticalAlign: "baseline",
};

const TAG_STYLE: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
	fontFamily: "var(--font-prose)",
};

const SHORT_ID_STYLE: CSSProperties = {
	color: "var(--text-primary)",
};

export function DebugRefChip({ entry }: DebugRefChipProps): ReactNode {
	const stub = entry.stub;

	const ContextMenuWrap = useRefContextMenu(stub);
	const { onClick } = useRefClick(stub);
	const { draggable, onDragStart } = useDraggableRef(stub);

	const onKeyDown = useCallback((e: ReactKeyboardEvent) => {
		if (e.key === "Enter" || e.key === " ") {
			e.preventDefault();
			e.stopPropagation();
			// Synthesize a click — the existing useRefClick handler
			// reads `event.preventDefault` / `stopPropagation` on a
			// `MouseEvent`-shaped argument; keyboard activation
			// reuses the same path through a click forwarder.
			(e.currentTarget as HTMLSpanElement).click();
		}
	}, []);

	const tag = entryKind(entry) === "entity" ? "E" : "S";

	return (
		<ContextMenuWrap>
			<span
				role="button"
				tabIndex={0}
				draggable={draggable}
				onDragStart={onDragStart}
				onClick={onClick}
				onKeyDown={onKeyDown}
				title={TITLE}
				style={CHIP_STYLE}
			>
				<span style={TAG_STYLE}>{tag}</span>
				<span style={SHORT_ID_STYLE}>{stub.shortId}</span>
			</span>
		</ContextMenuWrap>
	);
}
