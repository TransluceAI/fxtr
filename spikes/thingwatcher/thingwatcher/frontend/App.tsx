import React, { createContext, useContext, useState, useCallback, useEffect, useMemo } from 'react';
import type { Thing, ChangeInfo, TriggerRecord, Root, Tick } from './types';
import { Timeline } from './components/Timeline';
import { DockviewLayout } from './components/DockviewLayout';
import {
  ThingWatcherProvider,
  useThingWatcher,
  useTicks,
  useTickData,
  useTriggerRecords,
  useThing,
  RendererRefreshProvider,
  ActiveMarksProvider,
  useActiveMarksRegistry,
  PinnedThingsProvider,
  useAdditionalActiveThings,
} from './hooks';
import { ThingDropRegionProvider } from './lib/ThingDropRegionContext';
import type { ActiveMark } from './hooks';
import { Registration } from './lib/Registry';
import styles from './App.module.css';

// App context for UI state (selection + level filter)
// Tick state is managed by ThingWatcherProvider
interface AppState {
  selectedThingKey: string | null;  // b64 key - canonical identifier
  levelFilter: string[];
}

interface AppContextValue {
  state: AppState;
  setSelectedThing: (key: string | null) => void;
  setLevelFilter: (levels: string[]) => void;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useAppContext() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be used within AppProvider');
  return ctx;
}

function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>({
    selectedThingKey: null,
    levelFilter: [],
  });

  const setSelectedThing = useCallback((key: string | null) => {
    setState(s => ({ ...s, selectedThingKey: key }));
  }, []);

  const setLevelFilter = useCallback((levels: string[]) => {
    setState(s => ({ ...s, levelFilter: levels }));
  }, []);

  return (
    <AppContext.Provider value={{ state, setSelectedThing, setLevelFilter }}>
      {children}
    </AppContext.Provider>
  );
}

function MainContent() {
  const { state, setSelectedThing } = useAppContext();
  const { resolvedTick, latestTick, isLive, isSubscriptionActive, setCurrentTick } = useThingWatcher();
  const activeMarksRegistry = useActiveMarksRegistry();

  // Sidebar visibility state
  const [sidebarVisible, setSidebarVisible] = useState(true);

  // Get all ticks for timeline using hook
  const { ticks, loading: ticksLoading } = useTicks();

  // Get trigger records using hook
  const { records: allTriggerRecords } = useTriggerRecords();

  const currentTickIndex = resolvedTick;

  // Apply level filter, but always include currentTick and latestTick
  const filteredTicks = useMemo(() => {
    const latestTickIndex = ticks.length > 0 ? ticks[ticks.length - 1].index : null;

    return ticks.filter(t =>
      // Include if passes level filter (or no filter active)
      (state.levelFilter.length === 0 || state.levelFilter.includes(t.level)) ||
      // Always include current tick
      t.index === currentTickIndex ||
      // Always include latest tick
      t.index === latestTickIndex
    );
  }, [ticks, state.levelFilter, currentTickIndex]);

  // Find prev/next tick indices from filtered ticks
  const prevTickIndex = useMemo(() => {
    for (let i = filteredTicks.length - 1; i >= 0; i--) {
      if (filteredTicks[i].index < currentTickIndex) {
        return filteredTicks[i].index;
      }
    }
    return undefined;
  }, [filteredTicks, currentTickIndex]);

  const nextTickIndex = useMemo(() => {
    if (isLive) return undefined;  // No next tick when live
    for (let i = 0; i < filteredTicks.length; i++) {
      if (filteredTicks[i].index > currentTickIndex) {
        return filteredTicks[i].index;
      }
    }
    return undefined;
  }, [filteredTicks, currentTickIndex, isLive]);

  // Get tick data (things + changeInfo + roots) using hook
  const { things, changeInfo, roots, loading: tickDataLoading } = useTickData({
    tick: currentTickIndex,
    prevTick: prevTickIndex,
    nextTick: nextTickIndex,
  });

  // Snapshot keys for filtering additional active things
  const snapshotKeys = useMemo(() => new Set(things.map(t => t.key)), [things]);

  // Memoize snapshot marks - one mark per thing in the snapshot
  // These are registered declaratively via <Registration> components below
  const snapshotMarks = useMemo((): ActiveMark[] => {
    return things.map(t => ({ key: t.key, source: 'snapshot' as const }));
  }, [things]);

  // Fetch additional active things (from stores, not in snapshot)
  const { things: additionalThings } = useAdditionalActiveThings({ snapshotKeys });

  // Merge snapshot things + additional active things for Graph and ThingsList
  const allActiveThings = useMemo(() => {
    return [...things, ...additionalThings];
  }, [things, additionalThings]);

  // Debug logging for duplicate/missing nodes issue
  // console.log('[App] Tick data:', {
  //   tick: currentTickIndex,
  //   prevTick: prevTickIndex,
  //   nextTick: nextTickIndex,
  //   thingsCount: things.length,
  //   thingIds: things.map(t => `${t.shortId}:${t.thingType}`),
  //   addedCount: changeInfo.addedKeys.size,
  //   modifiedCount: changeInfo.modifiedKeys.size,
  //   disappearingCount: changeInfo.disappearingKeys.size,
  // });

  // Get selected thing details using hook
  const keyInput = state.selectedThingKey ? { key: state.selectedThingKey } : null;
  const { thing: selectedThing, loading: thingLoading } = useThing({
    keyInput: keyInput ?? { key: '' },
    pause: keyInput === null,
    includeData: true,
  });

  // Filter trigger records for current tick
  const currentTickTriggerRecords = useMemo(() => {
    return allTriggerRecords.filter(tr => tr.triggerTick === currentTickIndex);
  }, [allTriggerRecords, currentTickIndex]);

  // Set of shortIds that were triggered at current tick (for badge in ThingsList)
  const triggeredShortIds = useMemo(() => {
    return new Set(currentTickTriggerRecords.map(tr => tr.thing.shortId));
  }, [currentTickTriggerRecords]);

  // Set of tick indices that have triggers (for Timeline markers)
  const triggeredTicks = useMemo(() => {
    return new Set(allTriggerRecords.map(tr => tr.triggerTick));
  }, [allTriggerRecords]);

  // Trigger records for the selected thing at current tick
  const selectedThingTriggerRecords = useMemo(() => {
    if (state.selectedThingKey === null) return [];
    return currentTickTriggerRecords.filter(tr => tr.thing.key === state.selectedThingKey);
  }, [currentTickTriggerRecords, state.selectedThingKey]);

  // Callback after trigger mutation - trigger records auto-refetch via cache invalidation
  const handleTrigger = useCallback(() => {
    // No manual refetch needed - graphcache handles it
  }, []);

  // Keyboard shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if typing in an input
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) {
        return;
      }

      switch (e.key) {
        case 'ArrowLeft':
          if (currentTickIndex > 1) {
            setCurrentTick(currentTickIndex - 1);
          }
          break;
        case 'ArrowRight':
          if (latestTick !== null && currentTickIndex < latestTick) {
            setCurrentTick(currentTickIndex + 1);
          }
          break;
        case 'Home':
          if (ticks.length > 0) {
            setCurrentTick(ticks[0].index);
          }
          break;
        case 'End':
        case 'l':
        case 'L':
          setCurrentTick('live');
          break;
        case 'Escape':
          setSelectedThing(null);
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [currentTickIndex, latestTick, ticks, setCurrentTick, setSelectedThing]);

  const loading = ticksLoading || tickDataLoading;

  // Build latestTick object for Timeline (it expects Tick, not just number)
  const latestTickData = useMemo(() => {
    if (latestTick === null) return null;
    const tick = ticks.find(t => t.index === latestTick);
    return tick ?? { index: latestTick, timestamp: 0, level: 'unknown', type: 'unknown', desc: '' };
  }, [latestTick, ticks]);

  return (
    <>
      {/* Register snapshot things as active - renders nothing */}
      {snapshotMarks.map(mark => (
        <Registration
          key={mark.key}
          registry={activeMarksRegistry}
          registryKey="activeMarks"
          value={mark}
        />
      ))}

      <div className={styles.container}>
        <header className={styles.header}>
        <div className={styles.headerLeft}>
          <button
            className={styles.sidebarToggle}
            onClick={() => setSidebarVisible(v => !v)}
            title={sidebarVisible ? 'Hide sidebar' : 'Show sidebar'}
          >
            {sidebarVisible ? '◀' : '▶'}
          </button>
          <h1 className={styles.title}>Thingwatcher</h1>
        </div>
        <div className={styles.tickInfo}>
          Tick: {currentTickIndex}
          {isLive && (
            <span className={isSubscriptionActive ? styles.liveBadge : styles.offlineBadge}>
              {isSubscriptionActive ? 'LIVE' : 'OFFLINE'}
            </span>
          )}
        </div>
      </header>

      <main className={styles.main}>
        <DockviewLayout
          sidebarVisible={sidebarVisible}
          things={allActiveThings}
          changeInfo={changeInfo}
          selectedThingKey={state.selectedThingKey}
          onSelectThing={setSelectedThing}
          selectedThing={selectedThing}
          detailsLoading={thingLoading && !selectedThing}
          isLive={isLive}
          triggerRecords={selectedThingTriggerRecords}
          onTrigger={handleTrigger}
          triggeredShortIds={triggeredShortIds}
          roots={roots}
          currentTickIndex={currentTickIndex}
        />
      </main>

      <footer className={styles.timelineArea}>
        <Timeline
          ticks={ticks}
          filteredTicks={filteredTicks}
          latestTick={latestTickData}
          loading={loading}
          triggeredTicks={triggeredTicks}
        />
      </footer>
    </div>
    </>
  );
}

interface AppProps {
  isWsConnected?: boolean;
}

export function App({ isWsConnected }: AppProps) {
  return (
    <RendererRefreshProvider>
      <ThingWatcherProvider isWsConnected={isWsConnected}>
        <ActiveMarksProvider>
          <PinnedThingsProvider>
            <ThingDropRegionProvider>
              <AppProvider>
                <MainContent />
              </AppProvider>
            </ThingDropRegionProvider>
          </PinnedThingsProvider>
        </ActiveMarksProvider>
      </ThingWatcherProvider>
    </RendererRefreshProvider>
  );
}
