import { useEffect, useMemo, useState } from 'react';
import { useAppContext } from '../../App';
import { useThingWatcher, useTriggerTick, useHardRecapture, useRendererRefreshTrigger } from '../../hooks';
import type { Tick } from '../../types';
import styles from './Timeline.module.css';

interface TimelineProps {
  ticks: Tick[];
  filteredTicks: Tick[];  // Pre-filtered by App (always includes current + latest tick)
  latestTick: Tick | null;
  loading: boolean;
  triggeredTicks: Set<number>;
}

export function Timeline({ ticks, filteredTicks, latestTick, loading, triggeredTicks }: TimelineProps) {
  const { state, setLevelFilter } = useAppContext();
  const { currentTick, isLive, isSubscriptionActive, setCurrentTick } = useThingWatcher();

  // Track slider value locally during drag to prevent losing control
  const [isDragging, setIsDragging] = useState(false);
  const [dragValue, setDragValue] = useState<number | null>(null);

  // Track Alt/Option key state for hard recapture mode
  const [isModifierHeld, setIsModifierHeld] = useState(false);

  // Hook for triggering a manual tick capture
  const [triggering, triggerTick] = useTriggerTick();
  // Hook for hard recapture
  const [recapturing, hardRecapture] = useHardRecapture();
  // Hook for refreshing renderers
  const triggerRendererRefresh = useRendererRefreshTrigger();

  // Track Alt/Option key globally
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.altKey) {
        setIsModifierHeld(true);
      }
    };
    const handleKeyUp = (e: KeyboardEvent) => {
      if (!e.altKey) {
        setIsModifierHeld(false);
      }
    };
    const handleDrag = (e: DragEvent) => setIsModifierHeld(e.altKey);
    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('keyup', handleKeyUp);
    window.addEventListener('drag', handleDrag);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('keyup', handleKeyUp);
      window.removeEventListener('drag', handleDrag);
    };
  }, []);

  const handleCaptureTick = () => {
    if (isModifierHeld) {
      // Alt/Option held - do hard recapture
      hardRecapture();
    } else {
      // Normal click - capture tick
      triggerTick('debug', 'manual capture');
    }
  };

  const isCapturing = triggering || recapturing;

  const currentTickIndex = currentTick === 'live'
    ? latestTick?.index ?? 0
    : currentTick;

  // Get unique levels from ticks (use raw ticks for level checkbox options)
  const levels = useMemo(() => {
    const levelSet = new Set(ticks.map(t => t.level));
    return Array.from(levelSet).sort();
  }, [ticks]);

  // Slider maps to positions in filteredTicks array (1-indexed), plus one extra for "live"
  const sliderMax = filteredTicks.length + 1;

  // Find current tick's position in filtered list (1-indexed), or sliderMax for live
  const currentSliderPos = useMemo(() => {
    if (currentTick === 'live') return sliderMax;
    const idx = filteredTicks.findIndex(t => t.index === currentTickIndex);
    // If current tick is not in filtered list, find nearest
    if (idx === -1) {
      // Find the closest filtered tick
      for (let i = 0; i < filteredTicks.length; i++) {
        if (filteredTicks[i].index >= currentTickIndex) return i + 1;
      }
      return sliderMax; // Past all filtered ticks, go to live
    }
    return idx + 1;
  }, [currentTick, filteredTicks, currentTickIndex, sliderMax]);

  // Slider value: use drag value if dragging, otherwise computed position
  const sliderValue = isDragging && dragValue !== null ? dragValue : currentSliderPos;

  // Convert slider position to actual tick index
  const sliderPosToTickIndex = (pos: number): number | 'live' => {
    if (pos >= sliderMax || pos > filteredTicks.length) return 'live';
    if (pos < 1) return filteredTicks[0]?.index ?? 1;
    return filteredTicks[pos - 1]?.index ?? 'live';
  };

  // Find current tick data
  const currentTickData = useMemo(() => {
    if (currentTick === 'live') return latestTick;
    return ticks.find(t => t.index === currentTick) ?? null;
  }, [ticks, latestTick, currentTick]);

  // Get filtered tick indices for navigation
  const filteredTickIndices = useMemo(() => {
    return filteredTicks.map(t => t.index).sort((a, b) => a - b);
  }, [filteredTicks]);

  const handlePrev = () => {
    // Find the previous tick in filtered list
    const currentIdx = filteredTickIndices.findIndex(i => i >= currentTickIndex);
    if (currentIdx > 0) {
      setCurrentTick(filteredTickIndices[currentIdx - 1]);
    } else if (currentIdx === -1 && filteredTickIndices.length > 0) {
      // Current is past all filtered ticks, go to last filtered
      setCurrentTick(filteredTickIndices[filteredTickIndices.length - 1]);
    }
  };

  const handleNext = () => {
    // Find the next tick in filtered list
    const currentIdx = filteredTickIndices.findIndex(i => i > currentTickIndex);
    if (currentIdx !== -1) {
      setCurrentTick(filteredTickIndices[currentIdx]);
    } else if (currentTick !== 'live') {
      // Go to live if we're past all filtered ticks
      setCurrentTick('live');
    }
  };

  const handleFirst = () => {
    if (filteredTickIndices.length > 0) {
      setCurrentTick(filteredTickIndices[0]);
    }
  };

  const handleLast = () => {
    setCurrentTick('live');
  };

  const handleTickClick = (tickIndex: number) => {
    setCurrentTick(tickIndex);
  };

  const handleLiveClick = () => {
    setCurrentTick('live');
  };

  const handleLevelToggle = (level: string) => {
    if (state.levelFilter.length === 0) {
      // Currently showing all - switch to showing all except this one
      setLevelFilter(levels.filter(l => l !== level));
    } else if (state.levelFilter.includes(level)) {
      // Remove from filter
      const newFilter = state.levelFilter.filter(l => l !== level);
      // If empty, show all
      setLevelFilter(newFilter);
    } else {
      // Add to filter
      setLevelFilter([...state.levelFilter, level]);
    }
  };

  const formatTimestamp = (ts: number) => {
    const date = new Date(ts * 1000);
    return date.toLocaleTimeString();
  };

  return (
    <div className={styles.timeline}>
      <div className={styles.controls}>
        <button
          className={styles.button}
          onClick={handleFirst}
          disabled={loading || ticks.length === 0}
          title="First tick (Home)"
        >
          ⏮
        </button>
        <button
          className={styles.button}
          onClick={handlePrev}
          disabled={loading || currentTickIndex <= 1}
          title="Previous tick (←)"
        >
          ◀
        </button>

        <div className={styles.trackContainer}>
          <div className={styles.trackWrapper}>
            {/* Tick marks overlay */}
            <div className={styles.tickMarksRow}>
              {filteredTicks.map((tick, idx) => {
                // Position by index in filtered array (0-indexed), scaled to slider range
                // idx 0 → 0%, idx N-1 → (N-1)/N %, live at 100%
                const percent = filteredTicks.length > 0
                  ? (idx / (sliderMax - 1)) * 100
                  : 0;
                return (
                  <div
                    key={tick.index}
                    className={`${styles.tickMark} ${tick.index === currentTickIndex && currentTick !== 'live' ? styles.tickMarkActive : ''} ${tick.level === 'stream' ? styles.stream : ''}`}
                    data-level={tick.level}
                    style={{ left: `${percent}%` }}
                    onClick={() => handleTickClick(tick.index)}
                    title={`Tick ${tick.index}: ${tick.level} - ${tick.desc || '(no description)'}`}
                  />
                );
              })}
              {/* Live marker at far right */}
              <div
                className={`${styles.liveMark} ${currentTick === 'live' ? styles.liveMarkActive : ''}`}
                style={{ left: '100%' }}
                onClick={handleLiveClick}
                title="Live mode"
              />
            </div>
            {/* Trigger dots row */}
            <div className={styles.dotsRow}>
              {filteredTicks.map((tick, idx) => {
                if (!triggeredTicks.has(tick.index)) return null;
                const percent = filteredTicks.length > 0
                  ? (idx / (sliderMax - 1)) * 100
                  : 0;
                return (
                  <div
                    key={tick.index}
                    className={styles.triggerDot}
                    style={{ left: `${percent}%` }}
                    title={`Trigger at tick ${tick.index}`}
                  />
                );
              })}
            </div>
            {/* Scrubber slider */}
            <input
              type="range"
              className={styles.slider}
              min={1}
              max={sliderMax}
              value={sliderValue}
              onPointerDown={() => {
                setIsDragging(true);
              }}
              onPointerUp={() => {
                setIsDragging(false);
                setDragValue(null);
              }}
              onPointerCancel={() => {
                setIsDragging(false);
                setDragValue(null);
              }}
              onBlur={() => {
                setIsDragging(false);
                setDragValue(null);
              }}
              onInput={(e) => {
                const value = parseInt((e.target as HTMLInputElement).value, 10);
                setDragValue(value);
              }}
              onChange={(e) => {
                const pos = parseInt(e.target.value, 10);
                const tickIndex = sliderPosToTickIndex(pos);
                setCurrentTick(tickIndex);
              }}
              disabled={!isDragging && (loading || !latestTick)}
            />
          </div>
          <div
            className={`${styles.liveButton} ${currentTick === 'live' && isSubscriptionActive ? styles.liveButtonActive : ''}`}
            onClick={handleLiveClick}
          >
            LIVE
          </div>
          <span className={styles.tickDisplay}>
            {currentTick === 'live' ? 'LIVE' : currentTickIndex} / {latestTick?.index ?? '?'}
          </span>
        </div>

        <button
          className={styles.button}
          onClick={handleNext}
          disabled={loading || !latestTick || currentTickIndex >= latestTick.index}
          title="Next tick (→)"
        >
          ▶
        </button>
        <button
          className={`${styles.button} ${isSubscriptionActive ? (isLive ? styles.liveActive : '') : styles.liveOffline}`}
          onClick={handleLast}
          title="Live mode (L)"
        >
          {isSubscriptionActive ? (isLive ? '● LIVE' : '○ Live') : '○ OFFLINE'}
        </button>
      </div>

      <div className={styles.info}>
        {loading ? (
          <span className={styles.loading}>Loading...</span>
        ) : currentTickData ? (
          <>
            <span className={styles.level} data-level={currentTickData.level}>
              {currentTickData.level}
            </span>
            <span className={styles.time}>
              {formatTimestamp(currentTickData.timestamp)}
            </span>
            <span className={styles.desc}>
              {(currentTickData.desc as string) || '(no description)'}
            </span>
          </>
        ) : (
          <span className={styles.empty}>No ticks yet</span>
        )}
      </div>

      <div className={styles.bottomBar}>
        {levels.length > 1 && (
          <div className={styles.levelFilter}>
            <span className={styles.filterLabel}>Levels:</span>
            {levels.map(level => (
              <label key={level} className={styles.levelCheckbox} data-level={level}>
                <input
                  type="checkbox"
                  checked={state.levelFilter.length === 0 || state.levelFilter.includes(level)}
                  onChange={() => handleLevelToggle(level)}
                />
                {level}
              </label>
            ))}
          </div>
        )}
        <div className={styles.spacer} />
        <button
          className={styles.refreshButton}
          onClick={triggerRendererRefresh}
          title="Refresh custom renderers (reload from disk)"
        >
          ↻
        </button>
        <button
          className={`${styles.captureButton} ${isModifierHeld ? styles.hardRecaptureMode : ''}`}
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
    </div>
  );
}
