/**
 * DraggableRefHandle — drag-only wrapper for use *inside* a
 * FullPanel.
 *
 * Use when you want a specific element (e.g. a title or id
 * badge) to act as a grip for dragging the ref elsewhere,
 * without picking up the rest of `RefLink`'s interactions (no
 * hover preview, no right-click menu, no click-to-navigate —
 * the ref is already fully visible).
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { CSSProperties, ReactNode } from "react";
import { useDraggableRef } from "./useDraggableRef.js";

export interface DraggableRefHandleProps {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly children: ReactNode;
}

const STYLE: CSSProperties = {
	cursor: "grab",
	userSelect: "none",
};

export function DraggableRefHandle({ stub, children }: DraggableRefHandleProps) {
	const { draggable, onDragStart } = useDraggableRef(stub);
	return (
		<span
			draggable={draggable}
			onDragStart={onDragStart}
			style={STYLE}
			title="Drag to open in a new panel"
		>
			{children}
		</span>
	);
}
