// React is auto-imported by JSX transform
import {
  usePayload,
  useDraggableIdMarker,
  useThingMetadata,
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';

interface AsyncSnapshottedThingPayload {
  name?: string;
  status?: 'pending' | 'completed' | 'unknown';
  request_id?: string;
  fetched_value?: number;
  fetched_at?: number;
}

function StatusBadge({ status }: { status?: string }) {
  const colors: Record<string, { bg: string; text: string }> = {
    pending: { bg: '#3a3a1a', text: '#ffd93d' },
    completed: { bg: '#1a3a1a', text: '#6bcb77' },
    unknown: { bg: '#2a2a2a', text: '#888' },
  };
  const { bg, text } = colors[status ?? 'unknown'] ?? colors.unknown;

  return (
    <span style={{
      padding: '2px 8px',
      borderRadius: 4,
      background: bg,
      color: text,
      fontSize: 12,
      fontWeight: 'bold',
      textTransform: 'uppercase',
    }}>
      {status ?? 'unknown'}
    </span>
  );
}

function AsyncSnapshottedThingFull({ handle }: FullRendererProps) {
  const data = usePayload<AsyncSnapshottedThingPayload>(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ padding: '16px' }}>
      <DraggableIDMarker>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 12,
          cursor: 'grab',
        }}>
          <span style={{
            width: 10,
            height: 10,
            background: '#9b59b6',
            borderRadius: 2,
          }} />
          <span style={{ fontSize: 14, color: '#888' }}>
            AsyncSnapshottedThing: {data?.name}
          </span>
        </div>
      </DraggableIDMarker>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ color: '#888' }}>Status:</span>
          <StatusBadge status={data?.status} />
        </div>

        {data?.request_id && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: '#888' }}>Request ID:</span>
            <code style={{
              padding: '2px 6px',
              background: '#2a2a2a',
              borderRadius: 3,
              color: '#9b59b6',
              fontSize: 13,
            }}>
              {data.request_id}
            </code>
          </div>
        )}

        {data?.status === 'completed' && data?.fetched_value !== undefined && (
          <div style={{
            marginTop: 8,
            padding: 12,
            background: '#1a3a1a',
            borderRadius: 6,
          }}>
            <div style={{ color: '#6bcb77', marginBottom: 4 }}>
              Async Result:
            </div>
            <div style={{ fontSize: 32, fontWeight: 'bold', color: '#6bcb77' }}>
              {data.fetched_value}
            </div>
            {data.fetched_at && (
              <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
                Fetched at: {new Date(data.fetched_at * 1000).toLocaleTimeString()}
              </div>
            )}
          </div>
        )}

        {data?.status === 'pending' && (
          <div style={{
            marginTop: 8,
            padding: 12,
            background: '#3a3a1a',
            borderRadius: 6,
            color: '#ffd93d',
          }}>
            Waiting for async result...
          </div>
        )}
      </div>
    </div>
  );
}

function AsyncSnapshottedThingRef({ handle }: RefRendererProps) {
  const data = usePayload<AsyncSnapshottedThingPayload>(handle);

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#2a1a3a',
      borderRadius: 4,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: '#9b59b6',
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: '#9b59b6', flexShrink: 0 }}>
        Async: {data?.name}
      </span>
      <StatusBadge status={data?.status} />
      {data?.status === 'completed' && data?.fetched_value !== undefined && (
        <span style={{ color: '#6bcb77' }}>
          = {data.fetched_value}
        </span>
      )}
    </div>
  );
}

function AsyncSnapshottedThingInline({ handle }: RefRendererProps) {
  const data = usePayload<AsyncSnapshottedThingPayload>(handle);

  return (
    <span style={{ color: '#9b59b6' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#9b59b6',
        borderRadius: 2,
        marginRight: 4,
      }} />
      {data?.name} [{data?.status}]
      {data?.status === 'completed' && data?.fetched_value !== undefined && (
        <span style={{ color: '#6bcb77' }}> = {data.fetched_value}</span>
      )}
    </span>
  );
}

function AsyncSnapshottedThingPreview({ handle }: PreviewRendererProps) {
  const data = usePayload<AsyncSnapshottedThingPayload>(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#9b59b6', borderRadius: 2 }} />
          <span style={{ color: '#9b59b6', fontWeight: 'bold' }}>
            AsyncSnapshottedThing: {data?.name}
          </span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <StatusBadge status={data?.status} />
        {data?.request_id && (
          <code style={{ color: '#888', fontSize: 11 }}>{data.request_id}</code>
        )}
      </div>

      {data?.status === 'completed' && data?.fetched_value !== undefined && (
        <div style={{ color: '#6bcb77', fontSize: 18, fontWeight: 'bold' }}>
          Result: {data.fetched_value}
        </div>
      )}
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "AsyncSnapshottedThing": {
      full: AsyncSnapshottedThingFull,
      ref: AsyncSnapshottedThingRef,
      inline: AsyncSnapshottedThingInline,
      preview: AsyncSnapshottedThingPreview,
    },
  }
} as ThingWatcherRenderConfig;
