/**
 * EphemeralThing renderer - a temporary thing that can be pinned.
 */

import {
  usePayload,
  useDraggableIdMarker,
} from 'thingwatcher-bindings';
import type {
  FullRendererProps,
  RefRendererProps,
  ThingWatcherRenderConfig,
} from 'thingwatcher-bindings';

interface EphemeralThingData {
  name: string;
  message: string;
}

function EphemeralThingFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle) as EphemeralThingData | null;
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ padding: '12px' }}>
      <DraggableIDMarker>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
          marginBottom: '12px',
          cursor: 'grab',
        }}>
          <div style={{
            width: '48px',
            height: '48px',
            background: 'linear-gradient(135deg, #06b6d4, #0891b2)',
            borderRadius: '8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '20px',
            animation: 'pulse 2s ease-in-out infinite',
          }}>
            <span style={{ fontSize: '24px' }}>&#x23F3;</span>
          </div>
          <div>
            <div style={{ fontWeight: 'bold', fontSize: '16px' }}>
              {payload?.name}
            </div>
            <div style={{ fontSize: '12px', color: '#06b6d4' }}>
              Ephemeral Thing
            </div>
          </div>
        </div>
      </DraggableIDMarker>

      <div style={{
        background: 'rgba(6, 182, 212, 0.15)',
        borderRadius: '8px',
        padding: '12px',
        border: '1px dashed #06b6d4',
      }}>
        <div style={{
          fontSize: '14px',
          color: '#eee',
          lineHeight: 1.5,
        }}>
          {payload?.message}
        </div>
      </div>

      <div style={{
        marginTop: '12px',
        padding: '10px',
        background: 'rgba(251, 191, 36, 0.15)',
        borderRadius: '6px',
        border: '1px solid rgba(251, 191, 36, 0.3)',
      }}>
        <div style={{
          fontSize: '12px',
          color: '#fbbf24',
          fontWeight: 'bold',
          marginBottom: '4px',
        }}>
          &#x1F4CC; Try pinning me!
        </div>
        <div style={{
          fontSize: '11px',
          color: '#888',
          lineHeight: 1.4,
        }}>
          Click the pin button above, then navigate to tick 6+.
          I'll stay visible in the pinned section even though I don't exist there!
        </div>
      </div>

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.7; }
        }
      `}</style>
    </div>
  );
}

function EphemeralThingRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle) as EphemeralThingData | null;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: 'rgba(6, 182, 212, 0.3)',
      borderRadius: 4,
      whiteSpace: 'nowrap',
    }}>
      <span style={{ fontSize: 12 }}>&#x23F3;</span>
      <span style={{ color: '#67e8f9' }}>{payload?.name}</span>
      <span style={{ color: '#888', fontSize: 11 }}>ephemeral</span>
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "EphemeralThing": {
      full: EphemeralThingFull,
      ref: EphemeralThingRef,
    },
  },
} as ThingWatcherRenderConfig;
