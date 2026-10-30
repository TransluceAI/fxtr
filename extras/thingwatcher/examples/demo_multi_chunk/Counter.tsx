// React is auto-imported by JSX transform
import {
  usePayload,
  useLinks,
  useDraggableIdMarker,
  useThingMetadata,
  InlineNodeRef,
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';

interface CounterPayload {
  name?: string;
  value?: number;
}

// This renderer uses payload (not dataChunks) because Counter
// uses replace mode - there's always just one chunk.
function CounterFull({ handle }: FullRendererProps) {
  const data = usePayload<CounterPayload>(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ padding: '16px', textAlign: 'center' }}>
      <DraggableIDMarker>
        <div style={{ fontSize: '14px', color: '#888', marginBottom: '8px', cursor: 'grab' }}>
          Counter: {data?.name}
        </div>
      </DraggableIDMarker>
      <div style={{
        fontSize: '48px',
        fontWeight: 'bold',
        color: '#ff6b6b'
      }}>
        {data?.value ?? 0}
      </div>
    </div>
  );
}

function CounterRef({ handle }: RefRendererProps) {
  // Block ref - fills width
  const data = usePayload<CounterPayload>(handle);

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#3a1a1a',
      borderRadius: 4,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: '#ff6b6b',
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: '#ff6b6b', flexShrink: 0 }}>
        Counter: {data?.name}
      </span>
      <span style={{
        color: '#888',
        flex: 1,
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        value = {data?.value ?? 0}
      </span>
    </div>
  );
}

function CounterInline({ handle }: RefRendererProps) {
  // Inline ref - minimal, flows with text
  const data = usePayload<CounterPayload>(handle);

  return (
    <span style={{ color: '#ff6b6b' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#ff6b6b',
        borderRadius: 2,
        marginRight: 4,
      }} />
      {data?.name}: {data?.value ?? 0}
    </span>
  );
}

function CounterPreview({ handle }: PreviewRendererProps) {
  // Preview for hover cards with draggable ID and links
  const data = usePayload<CounterPayload>(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#ff6b6b', borderRadius: 2 }} />
          <span style={{ color: '#ff6b6b', fontWeight: 'bold' }}>Counter: {data?.name}</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: '#ff6b6b', fontSize: 24, fontWeight: 'bold' }}>
        {data?.value ?? 0}
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
    "Counter": {
      full: CounterFull,
      ref: CounterRef,
      inline: CounterInline,
      preview: CounterPreview,
    },
  }
} as ThingWatcherRenderConfig;
