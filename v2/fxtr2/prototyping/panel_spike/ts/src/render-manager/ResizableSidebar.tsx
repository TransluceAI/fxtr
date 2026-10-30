/**
 * ResizableSidebar - A container with a draggable resize handle on the right edge.
 */

import React, { useState, useCallback, useRef, useEffect } from 'react';

export interface ResizableSidebarProps {
  children: React.ReactNode;
  defaultWidth?: number;
  minWidth?: number;
  maxWidth?: number;
  collapsed?: boolean;
  collapsedWidth?: number;
}

export function ResizableSidebar({
  children,
  defaultWidth = 200,
  minWidth = 150,
  maxWidth = 400,
  collapsed = false,
  collapsedWidth = 32,
}: ResizableSidebarProps) {
  const [width, setWidth] = useState(defaultWidth);
  const isDragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (collapsed) return; // Don't allow resize when collapsed
      e.preventDefault();
      isDragging.current = true;
      startX.current = e.clientX;
      startWidth.current = width;
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    },
    [width, collapsed]
  );

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging.current) return;

      const delta = e.clientX - startX.current;
      const newWidth = Math.min(maxWidth, Math.max(minWidth, startWidth.current + delta));
      setWidth(newWidth);
    };

    const handleMouseUp = () => {
      if (isDragging.current) {
        isDragging.current = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [minWidth, maxWidth]);

  const displayWidth = collapsed ? collapsedWidth : width;

  return (
    <div
      style={{
        width: displayWidth,
        height: '100%',
        position: 'relative',
        flexShrink: 0,
        transition: 'width 0.15s ease-out',
      }}
    >
      {children}
      {/* Resize handle - hidden when collapsed */}
      {!collapsed && (
        <div
          onMouseDown={handleMouseDown}
          style={{
            position: 'absolute',
            top: 0,
            right: 0,
            width: 4,
            height: '100%',
            cursor: 'col-resize',
            background: 'transparent',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = '#58a6ff';
          }}
          onMouseLeave={(e) => {
            if (!isDragging.current) {
              e.currentTarget.style.background = 'transparent';
            }
          }}
        />
      )}
    </div>
  );
}
