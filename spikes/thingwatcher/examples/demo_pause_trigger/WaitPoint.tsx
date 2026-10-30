import { useState } from 'react';
import {
  usePayload,
  useLinks,
  useTriggerable,
  useTrigger,
  useDraggableIdMarker,
  useThingMetadata,
  NodeRef,
  InlineNodeRef,
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps, TriggerResult } from 'thingwatcher-bindings';

interface WaitPointPayload {
  description?: string;
  worker?: string;
  worker_job?: string;
  worker_progress?: number;
}

interface TriggerPayload {
  action?: string;
  message?: string;
}

function WaitPointFull({ handle }: FullRendererProps) {
  const data = usePayload<WaitPointPayload>(handle);
  const links = useLinks(handle);
  const [triggerable, triggerRecord] = useTriggerable(handle);
  const trigger = useTrigger(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const [message, setMessage] = useState('');
  const [triggeringState, setTriggeringState] = useState(false);
  const [result, setResult] = useState<TriggerResult | null>(null);

  const workerLink = links.find(l => l.linkType === 'worker');

  const handleTrigger = async (action: string) => {
    setTriggeringState(true);
    setResult(null);
    const triggerPayload = action === 'skip'
      ? { action: 'skip' }
      : message ? { message } : null;
    const res = await trigger(triggerPayload);
    setTriggeringState(false);
    setResult(res);
  };

  // Show replay view if we have a trigger record but thing is not currently triggerable
  const showReplay = !triggerable && triggerRecord !== null;
  const replayPayload = triggerRecord?.payload as TriggerPayload | null;
  const replayWasSkip = replayPayload?.action === 'skip';
  const replayMessage = replayPayload?.message || '';

  // Show actions box if triggerable OR if we have a replay to show
  const showActionsBox = triggerable || showReplay;

  return (
    <div style={{ padding: '12px' }}>
      <DraggableIDMarker>
        <div style={{ fontSize: '14px', color: '#e5a445', marginBottom: '8px', cursor: 'grab' }}>
          Wait Point: {data?.description}
        </div>
      </DraggableIDMarker>

      {workerLink && (
        <div style={{ marginBottom: '12px' }}>
          <span style={{ color: '#888', marginRight: '8px' }}>Worker:</span>
          <NodeRef thingKey={workerLink.toThingKey} />
        </div>
      )}

      <div style={{ fontSize: '12px', color: '#888', marginBottom: '12px' }}>
        Job: {data?.worker_job} | Progress: {data?.worker_progress}%
      </div>

      {showActionsBox && (
        <div style={{
          background: showReplay ? '#1a1a1a' : '#1a1a2e',
          padding: '12px',
          borderRadius: '6px',
          opacity: showReplay ? 0.7 : 1,
        }}>
          <div style={{ marginBottom: '8px', fontWeight: '600', display: 'flex', alignItems: 'center', gap: '8px' }}>
            Actions
            {showReplay && (
              <span style={{ fontSize: '11px', color: '#888', fontWeight: 'normal' }}>
                (triggered at tick {triggerRecord.triggerTick})
              </span>
            )}
          </div>
          <input
            type="text"
            placeholder="Optional message..."
            value={showReplay ? replayMessage : message}
            onChange={showReplay ? undefined : (e) => setMessage((e.target as HTMLInputElement).value)}
            readOnly={showReplay}
            disabled={showReplay}
            style={{
              width: '100%',
              padding: '8px',
              background: '#0f0f23',
              border: '1px solid #333',
              borderRadius: '4px',
              color: showReplay ? '#888' : '#eee',
              marginBottom: '8px',
              cursor: showReplay ? 'not-allowed' : 'text',
            }}
          />
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              type="button"
              onClick={showReplay ? undefined : () => handleTrigger('continue')}
              disabled={showReplay || triggeringState}
              style={{
                padding: '8px 16px',
                background: showReplay ? 'transparent' : '#22c55e',
                border: showReplay && !replayWasSkip ? '2px solid #22c55e' : 'none',
                borderRadius: '4px',
                color: showReplay ? (replayWasSkip ? '#666' : '#22c55e') : '#fff',
                cursor: showReplay || triggeringState ? 'not-allowed' : 'pointer',
                opacity: showReplay ? (replayWasSkip ? 0.4 : 1) : (triggeringState ? 0.6 : 1),
              }}
            >
              {triggeringState ? '...' : 'Continue'}
            </button>
            <button
              type="button"
              onClick={showReplay ? undefined : () => handleTrigger('skip')}
              disabled={showReplay || triggeringState}
              style={{
                padding: '8px 16px',
                background: showReplay ? 'transparent' : '#f59e0b',
                border: showReplay && replayWasSkip ? '2px solid #f59e0b' : 'none',
                borderRadius: '4px',
                color: showReplay ? (replayWasSkip ? '#f59e0b' : '#666') : '#fff',
                cursor: showReplay || triggeringState ? 'not-allowed' : 'pointer',
                opacity: showReplay ? (replayWasSkip ? 1 : 0.4) : (triggeringState ? 0.6 : 1),
              }}
            >
              Skip Remaining
            </button>
          </div>
          {result && (
            <div style={{
              marginTop: '8px',
              padding: '8px',
              background: result.ok ? 'rgba(34, 197, 94, 0.2)' : 'rgba(239, 68, 68, 0.2)',
              border: `1px solid ${result.ok ? '#22c55e' : '#ef4444'}`,
              borderRadius: '4px',
              fontSize: '12px',
              color: result.ok ? '#22c55e' : '#ef4444',
            }}>
              {result.ok ? 'Triggered!' : result.error || 'Error'}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function WaitPointRef({ handle }: RefRendererProps) {
  const data = usePayload<WaitPointPayload>(handle);

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 6,
      padding: '2px 8px',
      background: '#e5a445',
      borderRadius: 4,
      color: '#000',
      fontSize: 12,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{ flexShrink: 0 }}>WaitPoint: {data?.description}</span>
    </div>
  );
}

function WaitPointPreview({ handle }: PreviewRendererProps) {
  const data = usePayload<WaitPointPayload>(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#e5a445', borderRadius: 2 }} />
          <span style={{ color: '#e5a445', fontWeight: 'bold' }}>WaitPoint: {data?.description}</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: '#888', fontSize: 11 }}>
        Worker: {data?.worker} ({data?.worker_progress}%)
      </div>
      {links.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
          {links.map((link, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
              <span style={{ color: '#888' }}>{link.linkType}:</span>
              <InlineNodeRef thingKey={link.toThingKey} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "WaitPoint": {
      full: WaitPointFull,
      ref: WaitPointRef,
      preview: WaitPointPreview,
    },
  }
} as ThingWatcherRenderConfig;
