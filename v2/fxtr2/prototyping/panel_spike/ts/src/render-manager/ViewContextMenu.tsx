/**
 * ViewContextMenu - Context menu for view-related actions.
 *
 * Uses base-ui ContextMenu component. Handles different contexts:
 * - placeholder: Move entity view here, Open new view here
 * - child-owning: Move to new tab, Open another in new tab
 * - root: Open another view in new tab
 * - ref: Open in new tab, Jump to existing view
 *
 * Shift+right-click bypasses the custom menu for browser default.
 */

import React, { useCallback, useState } from 'react';
import { ContextMenu } from '@base-ui/react/context-menu';
import { useViewRegistry } from './contexts';
import type { EntityId, ViewId, SlotId, PanelDirection } from './ViewRegistry';

// Copied from @base-ui/react/utils/types.ts (doesn't seem to be importable)
type BaseUIEvent<E extends React.SyntheticEvent<Element, Event>> = E & {
  preventBaseUIHandler: () => void;
  readonly baseUIHandlerPrevented?: boolean;
};

// =============================================================================
// Context Types
// =============================================================================

export type MenuContext =
  | { type: 'placeholder'; entityId: EntityId; slotId: SlotId }
  | { type: 'child-owning'; entityId: EntityId; viewId: ViewId }
  | { type: 'root'; entityId: EntityId; viewId: ViewId }
  | { type: 'ref'; entityId: EntityId };

// =============================================================================
// Styles
// =============================================================================

const menuPopupStyle: React.CSSProperties = {
  background: '#2a2a2a',
  border: '1px solid #444',
  borderRadius: 6,
  padding: '4px 0',
  minWidth: 220,
  boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
  zIndex: 1000,
};

const menuItemStyle: React.CSSProperties = {
  padding: '8px 12px',
  cursor: 'pointer',
  color: '#ccc',
  fontSize: 13,
};

const menuItemHoverStyle: React.CSSProperties = {
  ...menuItemStyle,
  background: '#3a3a3a',
};

const menuItemDisabledStyle: React.CSSProperties = {
  ...menuItemStyle,
  color: '#666',
  cursor: 'default',
};

const groupLabelStyle: React.CSSProperties = {
  padding: '8px 12px 4px 12px',
  color: '#888',
  fontSize: 11,
  fontWeight: 'bold',
  textTransform: 'uppercase',
  letterSpacing: '0.5px',
};

const separatorStyle: React.CSSProperties = {
  height: 1,
  background: '#444',
  margin: '4px 0',
};

const hintStyle: React.CSSProperties = {
  padding: '8px 12px',
  color: '#555',
  fontSize: 11,
  fontStyle: 'italic',
  borderTop: '1px solid #333',
  marginTop: 4,
};

// =============================================================================
// MenuItem Component
// =============================================================================

interface MenuItemProps {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}

function MenuItem({ onClick, disabled, children }: MenuItemProps) {
  const [isHovered, setIsHovered] = useState(false);

  const style = disabled
    ? menuItemDisabledStyle
    : isHovered
      ? menuItemHoverStyle
      : menuItemStyle;

  return (
    <ContextMenu.Item
      onClick={disabled ? undefined : onClick}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      style={style}
    >
      {children}
    </ContextMenu.Item>
  );
}

// =============================================================================
// DirectionGroup Component
// =============================================================================

type ActionVerb = 'move' | 'open' | 'duplicate';

const DIRECTIONS: { direction: PanelDirection; symbol: string; suffix: string }[] = [
  { direction: 'within', symbol: '·', suffix: 'new tab' },
  { direction: 'right', symbol: '→', suffix: 'right' },
  { direction: 'left', symbol: '←', suffix: 'left' },
  { direction: 'above', symbol: '↑', suffix: 'up' },
  { direction: 'below', symbol: '↓', suffix: 'down' },
];

function getDirectionLabel(verb: ActionVerb, symbol: string, suffix: string): React.ReactNode {
  if (suffix === 'new tab') {
    // "here" case
    if (verb === 'move') return <>{symbol} <b>move</b> to new tab</>;
    if (verb === 'open') return <>{symbol} <b>open</b> in new tab</>;
    return <>{symbol} <b>duplicate</b> in new tab</>;
  }
  // directional case
  return <>{symbol} <b>{verb}</b> {suffix}</>;
}

interface DirectionGroupProps {
  label: string;
  verb: ActionVerb;
  onSelect: (direction: PanelDirection) => void;
}

function DirectionGroup({ label, verb, onSelect }: DirectionGroupProps) {
  return (
    <>
      <div style={groupLabelStyle}>{label}</div>
      {DIRECTIONS.map(({ direction, symbol, suffix }) => (
        <MenuItem key={direction} onClick={() => onSelect(direction)}>
          {getDirectionLabel(verb, symbol, suffix)}
        </MenuItem>
      ))}
    </>
  );
}

function Separator() {
  return <div style={separatorStyle} />;
}

// =============================================================================
// ViewContextMenu Component
// =============================================================================

export interface ViewContextMenuProps {
  context: MenuContext;
  children: React.ReactNode;
}

export function ViewContextMenu({ context, children }: ViewContextMenuProps) {
  const viewRegistry = useViewRegistry();

  const handleContextMenu = useCallback((e: BaseUIEvent<React.MouseEvent>) => {
    if (e.altKey) {
      // Let browser handle it - stop the context menu from intercepting
      e.preventBaseUIHandler();
      e.stopPropagation();
      return;
    }
  }, []);

  // Get existing views for entity (for ref context)
  const existingViews = context.type === 'ref'
    ? viewRegistry.getViewsForEntity(context.entityId)
    : [];

  // Handlers for different actions
  const handleMoveHere = useCallback(() => {
    if (context.type !== 'placeholder') return;
    const views = viewRegistry.getViewsForEntity(context.entityId);
    if (views.length === 1) {
      viewRegistry.moveViewToSlot(views[0].viewId, context.slotId);
    }
  }, [context, viewRegistry]);

  const handleOpenNewHere = useCallback(() => {
    if (context.type !== 'placeholder') return;
    viewRegistry.replacePlaceholderWithNewView(context.slotId);
  }, [context, viewRegistry]);

  const handleMoveToNewTab = useCallback((direction: PanelDirection) => {
    if (context.type !== 'child-owning') return;
    const position = viewRegistry.getPositionFromDirection(direction);
    viewRegistry.moveViewToNewPanel(context.viewId, position);
  }, [context, viewRegistry]);

  const handleDuplicateInTab = useCallback((direction: PanelDirection) => {
    if (context.type !== 'child-owning' && context.type !== 'root') return;
    const position = viewRegistry.getPositionFromDirection(direction);
    viewRegistry.duplicateViewInPanel(context.viewId, position);
  }, [context, viewRegistry]);

  const handleOpenInNewTab = useCallback((direction: PanelDirection) => {
    if (context.type !== 'ref') return;
    const position = viewRegistry.getPositionFromDirection(direction);
    viewRegistry.createNewViewInPanel(context.entityId, position);
  }, [context, viewRegistry]);

  const handleJumpToExisting = useCallback(() => {
    if (context.type !== 'ref') return;
    viewRegistry.jumpToEntity(context.entityId);
  }, [context, viewRegistry]);

  const handleCloseSubview = useCallback(() => {
    if (context.type !== 'child-owning') return;
    viewRegistry.closeSubview(context.viewId);
  }, [context, viewRegistry]);

  // Check if "Move here" should be enabled (only if exactly one view exists)
  const canMoveHere = context.type === 'placeholder'
    && viewRegistry.getViewsForEntity(context.entityId).length === 1;

  // Check if "Close subview" should be shown (only if other views exist)
  const canCloseSubview = context.type === 'child-owning'
    && viewRegistry.getViewsForEntity(context.entityId).length > 1;

  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger
        onContextMenu={handleContextMenu}
        style={{ display: 'contents' }}
      >
        {children}
      </ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Positioner style={{ zIndex: 1000 }}>
          <ContextMenu.Popup style={menuPopupStyle}>
            {/* Placeholder context */}
            {context.type === 'placeholder' && (
              <>
                <MenuItem
                  onClick={handleMoveHere}
                  disabled={!canMoveHere}
                >
                  Move entity view here
                  {!canMoveHere && ' (ambiguous; too many views)'}
                </MenuItem>
                <MenuItem onClick={handleOpenNewHere}>
                  Open new entity view here
                </MenuItem>
              </>
            )}

            {/* Child-owning context */}
            {context.type === 'child-owning' && (
              <>
                <DirectionGroup
                  label="Move entity view"
                  verb="move"
                  onSelect={handleMoveToNewTab}
                />
                <Separator />
                <DirectionGroup
                  label="Duplicate entity view"
                  verb="duplicate"
                  onSelect={handleDuplicateInTab}
                />
                {canCloseSubview && (
                  <>
                    <Separator />
                    <MenuItem onClick={handleCloseSubview}>
                      Close subview (point to external copy)
                    </MenuItem>
                  </>
                )}
              </>
            )}

            {/* Root context */}
            {context.type === 'root' && (
              <DirectionGroup
                label="Duplicate entity view"
                verb="duplicate"
                onSelect={handleDuplicateInTab}
              />
            )}

            {/* Ref context */}
            {context.type === 'ref' && (
              <>
                <MenuItem
                  onClick={handleJumpToExisting}
                  disabled={existingViews.length === 0}
                >
                  Jump to existing view
                  {existingViews.length === 0 && ' (none exists)'}
                </MenuItem>
                <Separator />
                <DirectionGroup
                  label={existingViews.length > 0 ? 'Duplicate view' : 'Open view'}
                  verb={existingViews.length > 0 ? 'duplicate' : 'open'}
                  onSelect={handleOpenInNewTab}
                />
              </>
            )}

            {/* Hint for browser menu */}
            <div style={hintStyle}>
              (hold alt for browser menu)
            </div>
          </ContextMenu.Popup>
        </ContextMenu.Positioner>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
