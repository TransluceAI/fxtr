/**
 * OverridableThing renderer - shows whether data comes from store or snapshot.
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

interface OverridableThingData {
  source: 'immutable_store' | 'snapshot';
  message: string;
  color: 'blue' | 'red';
}

function OverridableThingFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle) as OverridableThingData | null;
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const isFromStore = payload?.source === 'immutable_store';
  const bgColor = isFromStore ? 'rgba(59, 130, 246, 0.2)' : 'rgba(239, 68, 68, 0.2)';
  const accentColor = isFromStore ? '#3b82f6' : '#ef4444';

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
            background: accentColor,
            borderRadius: '8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '20px',
          }}>
            {isFromStore ? '&#x1F4E6;' : '&#x1F4F8;'}
          </div>
          <div>
            <div style={{ fontWeight: 'bold', fontSize: '16px' }}>
              Overridable Thing
            </div>
            <div style={{
              fontSize: '12px',
              color: accentColor,
              fontWeight: 'bold',
            }}>
              Source: {isFromStore ? 'Immutable Store' : 'Snapshot Override'}
            </div>
          </div>
        </div>
      </DraggableIDMarker>

      <div style={{
        background: bgColor,
        borderRadius: '8px',
        padding: '12px',
        border: `1px solid ${accentColor}`,
      }}>
        <div style={{
          fontSize: '14px',
          color: '#eee',
          lineHeight: 1.5,
        }}>
          "{payload?.message}"
        </div>
      </div>

      <div style={{
        marginTop: '12px',
        fontSize: '11px',
        color: '#888',
        lineHeight: 1.4,
      }}>
        {isFromStore ? (
          <>
            This thing comes from the <strong>immutable store</strong>.
            Navigate to ticks 2-3 to see the snapshot override!
          </>
        ) : (
          <>
            This thing is <strong>overriding</strong> the immutable store version.
            Navigate to tick 1 or 4+ to see the original store version.
          </>
        )}
      </div>
    </div>
  );
}

function OverridableThingRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle) as OverridableThingData | null;

  const isFromStore = payload?.source === 'immutable_store';
  const accentColor = isFromStore ? '#3b82f6' : '#ef4444';
  const bgColor = isFromStore ? 'rgba(59, 130, 246, 0.3)' : 'rgba(239, 68, 68, 0.3)';

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: bgColor,
      borderRadius: 4,
      whiteSpace: 'nowrap',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: accentColor,
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: accentColor }}>
        {isFromStore ? 'Store' : 'Override'}
      </span>
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "OverridableThing": {
      full: OverridableThingFull,
      ref: OverridableThingRef,
    },
  },
} as ThingWatcherRenderConfig;
