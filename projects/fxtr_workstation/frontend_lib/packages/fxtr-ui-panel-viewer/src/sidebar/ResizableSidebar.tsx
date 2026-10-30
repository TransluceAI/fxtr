/**
 * `ResizableSidebar` — container with a draggable resize handle
 * on its right edge.
 *
 * Controlled: `width` + `onWidthChange` instead of an internal
 * `useState`. Lets the shell persist the width into layout
 * storage alongside the rest of its state.
 *
 * Ported from the old `fxtr-workstation-core/src/sidebar/ResizableSidebar.tsx`
 * (which was itself a port from thingwatcher's
 * `frontend/components/Sidebar/ResizableSidebar.tsx`). Hover
 * color pulls from the design-system `--accent` token via
 * `resizable-sidebar.css` (already bundled in the package's
 * theme).
 */

import { type ReactNode, useCallback, useEffect, useRef } from "react";

export interface ResizableSidebarProps {
	readonly children: ReactNode;
	readonly width: number;
	readonly minWidth?: number;
	readonly maxWidth?: number;
	readonly onWidthChange: (width: number) => void;
}

export function ResizableSidebar({
	children,
	width,
	minWidth = 150,
	maxWidth = 480,
	onWidthChange,
}: ResizableSidebarProps) {
	const isDragging = useRef(false);
	const startX = useRef(0);
	const startWidth = useRef(0);

	const handleMouseDown = useCallback(
		(e: React.MouseEvent) => {
			e.preventDefault();
			isDragging.current = true;
			startX.current = e.clientX;
			startWidth.current = width;
			document.body.style.cursor = "col-resize";
			document.body.style.userSelect = "none";
		},
		[width],
	);

	useEffect(() => {
		const handleMouseMove = (e: MouseEvent) => {
			if (!isDragging.current) return;
			const delta = e.clientX - startX.current;
			const newWidth = Math.min(maxWidth, Math.max(minWidth, startWidth.current + delta));
			onWidthChange(newWidth);
		};

		const handleMouseUp = () => {
			if (isDragging.current) {
				isDragging.current = false;
				document.body.style.cursor = "";
				document.body.style.userSelect = "";
			}
		};

		document.addEventListener("mousemove", handleMouseMove);
		document.addEventListener("mouseup", handleMouseUp);

		return () => {
			document.removeEventListener("mousemove", handleMouseMove);
			document.removeEventListener("mouseup", handleMouseUp);
		};
	}, [minWidth, maxWidth, onWidthChange]);

	return (
		<div className="fw-resizable-sidebar" style={{ width }}>
			{children}
			{/* biome-ignore lint/a11y/useFocusableInteractive: keyboard-resize support not yet implemented; mouse-only handle */}
			<div
				className="fw-resizable-sidebar__handle"
				onMouseDown={handleMouseDown}
				role="separator"
				aria-orientation="vertical"
				aria-label="Resize sidebar"
			/>
		</div>
	);
}
