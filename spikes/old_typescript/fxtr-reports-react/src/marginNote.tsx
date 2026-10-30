/**
 * Margin note portal: <AttachMarginNote> renders children into the sidebar
 * region of the ReportShell layout.
 */

import { type ReactNode, type RefObject, createContext, useContext } from "react";
import { createPortal } from "react-dom";

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

/** Ref to the sidebar DOM node that margin notes portal into. */
export const MarginNoteTargetContext = createContext<RefObject<HTMLDivElement | null> | null>(null);

// ---------------------------------------------------------------------------
// AttachMarginNote
// ---------------------------------------------------------------------------

/**
 * Portals its children into the report shell's margin note sidebar.
 * If called outside a ReportShell, renders children inline as a fallback.
 */
export function AttachMarginNote({ children }: { children: ReactNode }) {
	const targetRef = useContext(MarginNoteTargetContext);
	const target = targetRef?.current;

	if (target) {
		return createPortal(children, target);
	}

	// Fallback: render inline
	return <>{children}</>;
}
