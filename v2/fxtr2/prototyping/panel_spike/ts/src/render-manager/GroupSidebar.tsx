/**
 * GroupSidebar - Displays a navigable list of all panel groups and their panels.
 *
 * Shows outside dockview on the left. Entity panels display using the ref renderer,
 * non-entity panels show just their title. Clicking a panel focuses it.
 */

import React from 'react';
import { PinOff, X } from 'lucide-react';
import type { GroupLayout, PanelLayout, EntityId } from './ViewRegistry';
import { useDataBundle } from './contexts';
import { useRegisteredEntityRenderer } from './registry';
import { getData } from '../data/ownership';
import type { EntityType } from '../data/schema';
import { EntitySidebarTooltip, TooltipProvider } from './HoverPreview';

export interface GroupSidebarProps {
  layout: GroupLayout[];
  onPanelClick: (panelId: string) => void;
  onPanelClose: (panelId: string) => void;
  onPanelDoubleClick: (panelId: string) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}

/**
 * Sidebar component showing all groups and panels.
 */
export function GroupSidebar({ layout, onPanelClick, onPanelClose, onPanelDoubleClick, collapsed, onToggleCollapsed }: GroupSidebarProps) {
  return (
    <TooltipProvider>
      <div
        style={{
          height: '100%',
          overflow: 'auto',
          background: '#1a1a2e',
          borderRight: '1px solid #333',
          padding: collapsed ? '8px 4px' : 8,
        }}
      >
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: collapsed ? 'center' : 'space-between',
          marginBottom: collapsed ? 0 : 12
        }}>
          {!collapsed && (
            <div style={{ fontSize: 12, color: '#888', fontWeight: 'bold' }}>
              Panels
            </div>
          )}
          <button
            onClick={onToggleCollapsed}
            style={{
              background: 'transparent',
              border: 'none',
              color: '#888',
              cursor: 'pointer',
              fontSize: 14,
              padding: '2px 6px',
              borderRadius: 4,
            }}
            onMouseEnter={(e) => { e.currentTarget.style.background = '#333'; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {collapsed ? '▶' : '◀'}
          </button>
        </div>
        {!collapsed && layout.map((group, index) => (
          <GroupSection
            key={group.groupId}
            group={group}
            index={index}
            onPanelClick={onPanelClick}
            onPanelClose={onPanelClose}
            onPanelDoubleClick={onPanelDoubleClick}
          />
        ))}
      </div>
    </TooltipProvider>
  );
}

interface GroupSectionProps {
  group: GroupLayout;
  index: number;
  onPanelClick: (panelId: string) => void;
  onPanelClose: (panelId: string) => void;
  onPanelDoubleClick: (panelId: string) => void;
}

function GroupSection({ group, index, onPanelClick, onPanelClose, onPanelDoubleClick }: GroupSectionProps) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div
        style={{
          fontSize: 11,
          color: '#666',
          marginBottom: 4,
          padding: '4px 8px',
          background: '#252540',
          borderRadius: 4,
        }}
      >
        Group {index + 1}
      </div>
      {group.panels.map((panel) => (
        <SidebarPanelItem
          key={panel.panelId}
          panel={panel}
          onClick={() => onPanelClick(panel.panelId)}
          onClose={() => onPanelClose(panel.panelId)}
          onDoubleClick={() => onPanelDoubleClick(panel.panelId)}
        />
      ))}
    </div>
  );
}

interface SidebarPanelItemProps {
  panel: PanelLayout;
  onClick: () => void;
  onClose: () => void;
  onDoubleClick: () => void;
}

function SidebarPanelItem({ panel, onClick, onClose, onDoubleClick }: SidebarPanelItemProps) {
  const data = useDataBundle();

  // Look up entity type if this is an entity panel
  let entityType: EntityType | undefined;
  if (panel.entityId) {
    const entity = getData(data, panel.entityId);
    entityType = entity?.type;
  }

  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation(); // Don't trigger onClick
    onClose();
  };

  const content = (
    <div
      onClick={onClick}
      onDoubleClick={onDoubleClick}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        padding: entityType ? '0 4px 0 0' : '4px 8px',
        marginBottom: 2,
        cursor: 'pointer',
        borderRadius: 4,
        background: panel.isActive ? '#333355' : 'transparent',
        borderLeft: panel.isActive ? '3px solid #58a6ff' : '3px solid transparent',
        fontStyle: panel.isPreview ? 'italic' : 'normal',
      }}
      onMouseEnter={(e) => {
        if (!panel.isActive) {
          e.currentTarget.style.background = '#252540';
        }
      }}
      onMouseLeave={(e) => {
        if (!panel.isActive) {
          e.currentTarget.style.background = 'transparent';
        }
      }}
    >
      {/* PinOff icon for preview panels */}
      {panel.isPreview && (
        <PinOff size={12} style={{ color: '#888', flexShrink: 0, marginLeft: 4 }} />
      )}
      {/* Panel label */}
      <div style={{ flex: 1, minWidth: 0 }}>
        {panel.entityId && entityType ? (
          <EntityPanelLabel entityId={panel.entityId} entityType={entityType} />
        ) : (
          <span style={{ fontSize: 12, color: '#aaa' }}>{panel.title}</span>
        )}
      </div>
      {/* Close button */}
      <button
        onClick={handleClose}
        style={{
          background: 'transparent',
          border: 'none',
          padding: 2,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: 2,
          flexShrink: 0,
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = '#444'; }}
        onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
        title="Close panel"
      >
        <X size={12} style={{ color: '#888' }} />
      </button>
    </div>
  );

  // Wrap entity panels with tooltip preview
  if (panel.entityId) {
    return (
      <EntitySidebarTooltip entityId={panel.entityId}>
        {content}
      </EntitySidebarTooltip>
    );
  }

  return content;
}

interface EntityPanelLabelProps {
  entityId: EntityId;
  entityType: EntityType;
}

/**
 * Renders the entity using the ref renderer (without click/drag behavior).
 */
function EntityPanelLabel({ entityId, entityType }: EntityPanelLabelProps) {
  const RefRenderer = useRegisteredEntityRenderer(entityType, 'ref');

  return (
    <div style={{ pointerEvents: 'none' }}>
      <RefRenderer entityId={entityId} isOwned={false} />
    </div>
  );
}
