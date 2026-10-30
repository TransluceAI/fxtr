/**
 * DraggableEntityHandle — drag-only wrapper for use *inside* a FullPanel.
 *
 * Use when you want a specific element (e.g. a title or id badge) to
 * act as a grip for dragging the entity elsewhere, without picking up
 * the rest of `EntityLink`'s interactions (no hover preview, no
 * right-click menu, no click-to-navigate — the entity is already fully
 * visible).
 */

import type { FxtrID } from "@fxtr/core";
import type { CSSProperties, ReactNode } from "react";
import { useDraggableEntity } from "./useDraggableEntity.js";

export interface DraggableEntityHandleProps {
	entity: FxtrID;
	children: ReactNode;
}

const STYLE: CSSProperties = {
	cursor: "grab",
	userSelect: "none",
};

export function DraggableEntityHandle({ entity, children }: DraggableEntityHandleProps) {
	const { draggable, onDragStart } = useDraggableEntity(entity);
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
