/**
 * EmbedApp - Single-thing viewer for embedding in VS Code webviews
 *
 * Receives thing key via:
 * - URL query param: ?key=<base64-encoded-key>
 * - postMessage: { type: 'showThing', key: string, args?: object }
 *
 * Always shows latest tick with streaming updates, supports custom rendering.
 */

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Toast } from '@base-ui/react/toast';
import { LowLevelNodeView } from './lib/LowLevelNodeView';
import { ThingUIEventsProvider, type ThingUIEventsContextValue } from './lib/ThingUIEventsContext';
import { HoverCloseContext, type HoverCloseContextValue } from './lib/HoverContext';
import { useThing, useThingWatcher, useTicks, useTriggerTick, useHardRecapture, useRendererRefreshTrigger, type ThingKeyInput } from './hooks';
import { ThingDropRegionProvider } from './lib/ThingDropRegionContext';

// =============================================================================
// Types
// =============================================================================

interface ShowThingMessage {
  type: 'showThing';
  key?: string;        // base64-encoded CBOR key
  keyJSON?: unknown;   // JSON-compatible key value
  args?: Record<string, unknown>;
}

// =============================================================================
// Styles
// =============================================================================

const styles = {
  container: {
    padding: '16px',
    paddingBottom: '48px',  // Space for status bar
    minHeight: '100%',
    background: '#1a1a2e',
  } as React.CSSProperties,
  empty: {
    color: '#888',
    fontStyle: 'italic' as const,
    textAlign: 'center' as const,
    paddingTop: '40px',
  } as React.CSSProperties,
  error: {
    color: '#f66',
    padding: '16px',
    background: '#2a1a1a',
    borderRadius: '8px',
  } as React.CSSProperties,
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    marginBottom: '16px',
    paddingBottom: '12px',
    borderBottom: '1px solid #333',
  } as React.CSSProperties,
  thingType: {
    background: '#4a4',
    color: '#fff',
    padding: '4px 10px',
    borderRadius: '4px',
    fontWeight: 'bold' as const,
    fontSize: '14px',
  } as React.CSSProperties,
  shortId: {
    color: '#8cf',
    fontSize: '14px',
  } as React.CSSProperties,
  key: {
    color: '#888',
    fontSize: '12px',
    fontFamily: 'monospace',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
    flex: 1,
  } as React.CSSProperties,
  // Status bar styles
  statusBar: {
    position: 'fixed' as const,
    bottom: 0,
    left: 0,
    right: 0,
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '6px 12px',
    background: '#16213e',
    borderTop: '1px solid #0f3460',
    fontSize: '12px',
    color: '#aaa',
  } as React.CSSProperties,
  liveBadge: {
    background: '#22c55e',
    color: '#000',
    padding: '2px 6px',
    borderRadius: '4px',
    fontSize: '11px',
    fontWeight: 700,
  } as React.CSSProperties,
  offlineBadge: {
    color: '#ef4444',
    border: '1px solid #ef4444',
    background: 'transparent',
    padding: '2px 6px',
    borderRadius: '4px',
    fontSize: '11px',
    fontWeight: 700,
  } as React.CSSProperties,
  tickInfo: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  } as React.CSSProperties,
  tickLevel: {
    padding: '2px 6px',
    borderRadius: '4px',
    fontSize: '11px',
    fontWeight: 600,
    background: '#333',
  } as React.CSSProperties,
  tickDesc: {
    color: '#888',
    flex: 1,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
  } as React.CSSProperties,
  captureButton: {
    background: '#1a3a5c',
    color: '#8cf',
    border: '1px solid #2a4a6c',
    padding: '4px 10px',
    borderRadius: '4px',
    fontSize: '11px',
    fontWeight: 600,
    cursor: 'pointer',
  } as React.CSSProperties,
  captureButtonHard: {
    background: '#3a2a1c',
    color: '#fc8',
    borderColor: '#5a4a3c',
  } as React.CSSProperties,
  refreshButton: {
    background: '#1a3a5c',
    color: '#8cf',
    border: '1px solid #2a4a6c',
    padding: '4px 10px',
    borderRadius: '4px',
    fontSize: '14px',
    fontWeight: 600,
    cursor: 'pointer',
  } as React.CSSProperties,
  // Toast styles
  toastViewport: {
    position: 'fixed' as const,
    bottom: '16px',
    right: '16px',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '8px',
    zIndex: 1000,
  } as React.CSSProperties,
  toast: {
    background: '#333',
    color: '#fff',
    padding: '12px 16px',
    borderRadius: '6px',
    boxShadow: '0 4px 12px rgba(0, 0, 0, 0.3)',
    fontSize: '14px',
  } as React.CSSProperties,
};

// =============================================================================
// URL Helpers
// =============================================================================

function getKeyFromURL(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get('key');
}

function updateURLWithKey(key: string) {
  const url = new URL(window.location.href);
  url.searchParams.set('key', key);
  window.history.replaceState({}, '', url.toString());
}

// =============================================================================
// Toast List Component
// =============================================================================

function ToastList() {
  const { toasts } = Toast.useToastManager();
  return (
    <>
      {toasts.map((toast) => (
        <Toast.Root key={toast.id} toast={toast} style={styles.toast}>
          <Toast.Title />
        </Toast.Root>
      ))}
    </>
  );
}

// =============================================================================
// EmbedApp Component
// =============================================================================

export function EmbedApp() {
  // State from URL or postMessage
  const [thingKey, setThingKey] = useState<string | null>(() => getKeyFromURL());
  const [keyJSON, setKeyJSON] = useState<unknown>(undefined);
  const [args, setArgs] = useState<Record<string, unknown>>({});

  // Build key input for resolving
  const keyInput: ThingKeyInput | null = useMemo(() => {
    if (thingKey !== null) {
      return { key: thingKey };
    } else if (keyJSON !== undefined) {
      return { keyJSON };
    }
    return null;
  }, [thingKey, keyJSON]);

  const hasThingKey = keyInput !== null;

  // Resolve keyJSON to actual key and get thing metadata for header
  const { thing: resolvedThing, error: resolveError } = useThing({
    keyInput: keyInput ?? { key: '' },
    pause: !hasThingKey,
    includeData: false,
  });

  // Update URL when thing key changes
  useEffect(() => {
    if (thingKey) {
      updateURLWithKey(thingKey);
    }
  }, [thingKey]);

  // Listen for postMessage
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const data = event.data as ShowThingMessage;
      if (data?.type === 'showThing') {
        // Update thing key
        if (data.key !== undefined) {
          setThingKey(data.key);
          setKeyJSON(undefined);
        } else if (data.keyJSON !== undefined) {
          setThingKey(null);
          setKeyJSON(data.keyJSON);
        }

        // Update args to pass to renderer
        setArgs(data.args ?? {});
      }
    };

    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, []);

  // Render empty state if no thing key
  if (!hasThingKey) {
    return (
      <div style={styles.container}>
        <div style={styles.empty}>
          Waiting for thing selection via postMessage or URL param...
        </div>
      </div>
    );
  }

  // Render error state
  if (resolveError) {
    return (
      <div style={styles.container}>
        <div style={styles.error}>
          Error: {resolveError.message}
        </div>
      </div>
    );
  }

  // Still resolving key
  if (!resolvedThing) {
    return (
      <div style={styles.container}>
        <div style={styles.empty}>Loading...</div>
      </div>
    );
  }

  return (
    <ThingDropRegionProvider>
      <Toast.Provider>
        <EmbedAppContent
          resolvedThing={resolvedThing}
          args={args}
        />
        <Toast.Portal>
          <Toast.Viewport style={styles.toastViewport}>
            <ToastList />
          </Toast.Viewport>
        </Toast.Portal>
      </Toast.Provider>
    </ThingDropRegionProvider>
  );
}

// =============================================================================
// EmbedAppContent (needs Toast context)
// =============================================================================

interface EmbedAppContentProps {
  resolvedThing: {
    key: string;
    keyJSON?: unknown;
    shortId: number;
    thingType: string;
  };
  args: Record<string, unknown>;
}

function EmbedAppContent({ resolvedThing, args }: EmbedAppContentProps) {
  const toastManager = Toast.useToastManager();

  // Tick state and subscription status
  const { latestTick, isSubscriptionActive } = useThingWatcher();
  const { ticks } = useTicks();

  // Find the latest tick data for type/desc display
  const latestTickData = useMemo(() => {
    if (latestTick === null) return null;
    return ticks.find(t => t.index === latestTick) ?? null;
  }, [ticks, latestTick]);

  // Capture tick hooks
  const [triggering, triggerTick] = useTriggerTick();
  const [recapturing, hardRecapture] = useHardRecapture();
  const isCapturing = triggering || recapturing;

  // Renderer refresh hook
  const triggerRendererRefresh = useRendererRefreshTrigger();

  // Track Alt/Option key for hard recapture mode
  const [isModifierHeld, setIsModifierHeld] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.altKey) setIsModifierHeld(true);
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (!e.altKey) setIsModifierHeld(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('keyup', handleKeyUp);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('keyup', handleKeyUp);
    };
  }, []);

  const handleCaptureTick = () => {
    if (isModifierHeld) {
      hardRecapture();
    } else {
      triggerTick('debug', 'manual capture');
    }
  };

  // Hover close state - stores callbacks from hover cards that should be called to close them
  const hoverCloseListenersRef = useRef<Set<() => void>>(new Set());

  const hoverCloseContextValue = useMemo((): HoverCloseContextValue => ({
    registerCloseListener: (callback: () => void) => {
      hoverCloseListenersRef.current.add(callback);
      return () => {
        hoverCloseListenersRef.current.delete(callback);
      };
    },
    closeAllHovers: () => {
      hoverCloseListenersRef.current.forEach((cb) => cb());
    },
  }), []);

  // Handler for clicking a thing - copy key to clipboard
  const handleClickThingBase = useCallback(async (key: string) => {
    try {
      await navigator.clipboard.writeText(key);
      toastManager.add({ title: 'Copied key to clipboard', timeout: 2000 });
    } catch (err) {
      console.error('Failed to copy to clipboard:', err);
      toastManager.add({ title: 'Failed to copy to clipboard', timeout: 2000 });
    }
  }, [toastManager]);

  // Wrap click handler to also close hovers
  const handleClickThing = useCallback((key: string) => {
    hoverCloseContextValue.closeAllHovers();
    handleClickThingBase(key);
  }, [hoverCloseContextValue, handleClickThingBase]);

  // Handler for opening thing in new window
  const handleOpenThingInNewPanel = useCallback((key: string) => {
    const url = new URL(window.location.href);
    url.searchParams.set('key', key);
    window.open(url.toString(), '_blank');
  }, []);

  // Build ThingUIEvents context value
  const thingUIEventsContextValue = useMemo((): ThingUIEventsContextValue => ({
    onClickThing: handleClickThing,
    onOpenThingInNewPanel: handleOpenThingInNewPanel,
    openInNewPanelLabel: 'window',
  }), [handleClickThing, handleOpenThingInNewPanel]);

  return (
    <HoverCloseContext.Provider value={hoverCloseContextValue}>
      <ThingUIEventsProvider value={thingUIEventsContextValue}>
        <div style={styles.container}>
          {/* Header with thing metadata */}
          <div style={styles.header}>
            <span style={styles.thingType}>{resolvedThing.thingType}</span>
            <span style={styles.shortId}>#{resolvedThing.shortId}</span>
            <span style={styles.key} title={resolvedThing.key}>
              {resolvedThing.keyJSON !== null && resolvedThing.keyJSON !== undefined
                ? JSON.stringify(resolvedThing.keyJSON)
                : resolvedThing.key.length > 40 ? `${resolvedThing.key.slice(0, 40)}...` : resolvedThing.key}
            </span>
          </div>

          {/* Renderer with args from postMessage */}
          <LowLevelNodeView
            thingKey={resolvedThing.key}
            isTopLevel={true}
            args={args}
          />
        </div>

        {/* Status bar */}
        <div style={styles.statusBar}>
          <span style={isSubscriptionActive ? styles.liveBadge : styles.offlineBadge}>
            {isSubscriptionActive ? 'LIVE' : 'OFFLINE'}
          </span>
          <span>Tick {latestTick ?? '?'}</span>
          {latestTickData && (
            <div style={styles.tickInfo}>
              <span style={styles.tickLevel} data-level={latestTickData.level}>
                {latestTickData.level}
              </span>
              <span style={styles.tickDesc}>
                {(latestTickData.desc as string) || '(no description)'}
              </span>
            </div>
          )}
          <div style={{ flex: 1 }} />
          <button
            style={styles.refreshButton}
            onClick={triggerRendererRefresh}
            title="Refresh custom renderers (reload from disk)"
          >
            ↻
          </button>
          <button
            style={{
              ...styles.captureButton,
              ...(isModifierHeld ? styles.captureButtonHard : {}),
            }}
            onClick={handleCaptureTick}
            disabled={isCapturing}
            title={isModifierHeld
              ? "Hard recapture - reset all state (Alt/Option held)"
              : "Capture a manual tick snapshot (hold Alt/Option for hard recapture)"
            }
          >
            {isCapturing
              ? (recapturing ? 'Recapturing...' : 'Capturing...')
              : (isModifierHeld ? 'Hard Recapture' : 'Capture Tick')
            }
          </button>
        </div>
      </ThingUIEventsProvider>
    </HoverCloseContext.Provider>
  );
}
