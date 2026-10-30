/**
 * Bare-bones test of all dependencies.
 * This file is meant to be deleted after verifying imports work.
 */

import React, { useSyncExternalStore, useCallback, useState, useEffect } from 'react';

// Dockview imports (CSS loaded via index.html)
import {
  DockviewReact,
  DockviewReadyEvent,
  IDockviewPanelProps,
  IDockviewHeaderActionsProps,
} from 'dockview';

// React reverse portal imports
import { createHtmlPortalNode, InPortal, OutPortal } from 'react-reverse-portal';

// Base UI imports - PreviewCard
import { PreviewCard } from '@base-ui/react/preview-card';

// ============================================================================
// 1. useSyncExternalStore demo - simple counter store
// ============================================================================

type Listener = () => void;

const createCounterStore = () => {
  let count = 0;
  const listeners = new Set<Listener>();

  return {
    subscribe: (listener: Listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => count,
    increment: () => {
      count++;
      listeners.forEach((l) => l());
    },
  };
};

const counterStore = createCounterStore();

function SyncExternalStoreDemo() {
  const count = useSyncExternalStore(
    counterStore.subscribe,
    counterStore.getSnapshot
  );

  return (
    <div style={{ padding: 8, background: '#2d2d2d', marginBottom: 8 }}>
      <h3>useSyncExternalStore Demo</h3>
      <p>Count: {count}</p>
      <button onClick={counterStore.increment}>Increment</button>
    </div>
  );
}

// ============================================================================
// 2. React Reverse Portal demo
// ============================================================================

function ReversePortalDemo() {
  const [portalNode] = useState(() => createHtmlPortalNode());
  const [showInAlt, setShowInAlt] = useState(false);

  return (
    <div style={{ padding: 8, background: '#2d2d2d', marginBottom: 8 }}>
      <h3>React Reverse Portal Demo</h3>
      <InPortal node={portalNode}>
        <div style={{ padding: 8, background: '#4a4a4a' }}>
          Portaled content (preserves state when moved)
        </div>
      </InPortal>

      <button onClick={() => setShowInAlt(!showInAlt)}>
        Move to {showInAlt ? 'primary' : 'alternate'} location
      </button>

      <div style={{ display: 'flex', gap: 16, marginTop: 8 }}>
        <div style={{ border: '1px solid #666', padding: 8, minWidth: 150 }}>
          <p>Primary:</p>
          {!showInAlt && <OutPortal node={portalNode} />}
        </div>
        <div style={{ border: '1px solid #666', padding: 8, minWidth: 150 }}>
          <p>Alternate:</p>
          {showInAlt && <OutPortal node={portalNode} />}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// 3. Base UI PreviewCard demo
// ============================================================================

function PreviewCardDemo() {
  return (
    <div style={{ padding: 8, background: '#2d2d2d', marginBottom: 8 }}>
      <h3>Base UI PreviewCard Demo</h3>
      <PreviewCard.Root>
        <PreviewCard.Trigger
          href="https://example.com"
          style={{ color: '#58a6ff', textDecoration: 'underline', cursor: 'pointer' }}
        >
          Hover over me for preview
        </PreviewCard.Trigger>
        <PreviewCard.Portal>
          <PreviewCard.Positioner>
            <PreviewCard.Popup
              style={{
                background: '#3d3d3d',
                border: '1px solid #555',
                borderRadius: 4,
                padding: 12,
                maxWidth: 300,
              }}
            >
              <PreviewCard.Arrow style={{ fill: '#3d3d3d' }} />
              <p style={{ margin: 0 }}>This is a preview card popup!</p>
            </PreviewCard.Popup>
          </PreviewCard.Positioner>
        </PreviewCard.Portal>
      </PreviewCard.Root>
    </div>
  );
}

// ============================================================================
// 4. Infinite Tower HoverableLink
// ============================================================================

// Generate a color based on depth for visual distinction
const getDepthColor = (depth: number): string => {
  const colors = ['#58a6ff', '#f0a', '#0f0', '#ff0', '#0ff', '#f80', '#80f'];
  return colors[depth % colors.length];
};

const getDepthBg = (depth: number): string => {
  const base = 0x3d;
  const offset = (depth * 10) % 40;
  const val = base + offset;
  return `#${val.toString(16)}${val.toString(16)}${val.toString(16)}`;
};

interface HoverableLinkProps {
  setKeptOpen?: (open: boolean) => void;
  depth?: number;
  label?: string;
}

const HoverableLink: React.FC<HoverableLinkProps> = ({
  setKeptOpen,
  depth = 0,
  label = 'Hover me',
}) => {
  const [open, setOpen] = useState(false);
  const [keptOpenByChild, setKeptOpenByChild] = useState(false);
  const [keptOpenByHover, setKeptOpenByHover] = useState(false);

  const isOpen = open || keptOpenByChild || keptOpenByHover;

  // Propagate our open state up to parent
  useEffect(() => {
    setKeptOpen?.(isOpen);
  }, [isOpen, setKeptOpen]);

  return (
    <PreviewCard.Root open={isOpen} onOpenChange={setOpen}>
      <PreviewCard.Trigger
        delay={200}
        href="#"
        style={{
          color: getDepthColor(depth),
          textDecoration: 'underline',
          cursor: 'pointer',
        }}
      >
        {label} (depth {depth})
      </PreviewCard.Trigger>
      <PreviewCard.Portal>
        <PreviewCard.Positioner side="top" alignOffset={10}>
          <PreviewCard.Popup
            onMouseEnter={() => setKeptOpenByHover(true)}
            onMouseLeave={() => setKeptOpenByHover(false)}
            style={{
              background: getDepthBg(depth),
              border: `1px solid ${getDepthColor(depth)}`,
              borderRadius: 4,
              padding: 12,
              maxWidth: 280,
            }}
          >
            <PreviewCard.Arrow style={{ fill: getDepthBg(depth) }} />
            <p style={{ margin: '0 0 8px 0' }}>
              Preview at depth {depth}
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <HoverableLink
                setKeptOpen={setKeptOpenByChild}
                depth={depth + 1}
                label="Go deeper (A)"
              />
              <HoverableLink
                setKeptOpen={setKeptOpenByChild}
                depth={depth + 1}
                label="Go deeper (B)"
              />
            </div>
          </PreviewCard.Popup>
        </PreviewCard.Positioner>
      </PreviewCard.Portal>
    </PreviewCard.Root>
  );
};

// ============================================================================
// 5. Dockview demo
// ============================================================================

// Popout icon (open in new window)
const PopoutIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
    <polyline points="15 3 21 3 21 9" />
    <line x1="10" y1="14" x2="21" y2="3" />
  </svg>
);

// Pop-back icon (return to main window)
const PopbackIcon = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <polyline points="9 10 4 10 4 5" />
    <path d="M20 20v-7a4 4 0 0 0-4-4H4" />
  </svg>
);

const RightHeaderActions: React.FC<IDockviewHeaderActionsProps> = (props) => {
  const isPopout = props.api.location.type === 'popout';

  const handlePopout = () => {
    if (isPopout) {
      props.api.moveTo({ position: 'right' });
    } else {
      props.containerApi.addPopoutGroup(props.group);
    }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', paddingRight: 4 }}>
      <button
        onClick={handlePopout}
        title={isPopout ? 'Return to main window' : 'Pop out to new window'}
        style={{
          background: 'transparent',
          border: 'none',
          cursor: 'pointer',
          color: 'inherit',
          padding: 4,
          display: 'flex',
          alignItems: 'center',
          opacity: 0.7,
        }}
        onMouseEnter={(e) => (e.currentTarget.style.opacity = '1')}
        onMouseLeave={(e) => (e.currentTarget.style.opacity = '0.7')}
      >
        {isPopout ? <PopbackIcon /> : <PopoutIcon />}
      </button>
    </div>
  );
};

const PanelContent: React.FC<IDockviewPanelProps> = (props) => {
  return (
    <div style={{ padding: 8, height: '100%', background: '#252526' }}>
      <p>Panel: {props.api.title}</p>
      <HoverableLink label="Start the infinite tower" />
    </div>
  );
};

function DockviewDemo() {
  const onReady = useCallback((event: DockviewReadyEvent) => {
    event.api.addPanel({
      id: 'panel_1',
      component: 'default',
      title: 'Panel 1',
    });
    event.api.addPanel({
      id: 'panel_2',
      component: 'default',
      title: 'Panel 2',
      position: { referencePanel: 'panel_1', direction: 'right' },
    });
  }, []);

  return (
    <div style={{ padding: 8, background: '#2d2d2d', marginBottom: 8 }}>
      <h3>Dockview Demo</h3>
      <div style={{ height: 200, border: '1px solid #555' }}>
        <DockviewReact
          className="dockview-theme-abyss"
          onReady={onReady}
          components={{
            default: PanelContent,
          }}
          rightHeaderActionsComponent={RightHeaderActions}
        />
      </div>
    </div>
  );
}

// ============================================================================
// Main test component
// ============================================================================

export function DepsTest() {
  return (
    <div style={{ padding: 16 }}>
      <h2>Dependencies Test</h2>
      <p style={{ color: '#888' }}>
        All dependencies are working if you can see and interact with each section below.
      </p>
      <SyncExternalStoreDemo />
      <ReversePortalDemo />
      <PreviewCardDemo />
      <DockviewDemo />
    </div>
  );
}
