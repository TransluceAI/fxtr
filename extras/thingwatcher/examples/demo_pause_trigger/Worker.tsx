// React is auto-imported by JSX transform
import {
  usePayload,
  useLinks,
  useDraggableIdMarker,
  useThingMetadata,
  InlineNodeRef,
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';

interface WorkerPayload {
  name?: string;
  job?: string;
  progress?: number;
}

function WorkerFull({ handle }: FullRendererProps) {
  const data = usePayload<WorkerPayload>(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);
  const { name, job, progress } = data || {};

  return (
    <div style={{ padding: '12px' }}>
      <DraggableIDMarker>
        <div style={{ fontSize: '16px', fontWeight: 'bold', marginBottom: '8px', cursor: 'grab' }}>
          Worker: {name}
        </div>
      </DraggableIDMarker>
      <div style={{ marginBottom: '8px' }}>
        <span style={{ color: '#888' }}>Job:</span> {job}
      </div>
      <div style={{ marginBottom: '8px' }}>
        <span style={{ color: '#888' }}>Progress:</span>
        <div style={{
          background: '#333',
          borderRadius: '4px',
          height: '8px',
          marginTop: '4px',
          overflow: 'hidden',
        }}>
          <div style={{
            background: '#4a90d9',
            height: '100%',
            width: `${progress}%`,
            transition: 'width 0.3s',
          }} />
        </div>
        <div style={{ fontSize: '12px', color: '#888', marginTop: '2px' }}>
          {progress}%
        </div>
      </div>
    </div>
  );
}

function WorkerRef({ handle }: RefRendererProps) {
  const data = usePayload<WorkerPayload>(handle);
  const { name, progress } = data || {};

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 6,
      padding: '2px 8px',
      background: '#4a90d9',
      borderRadius: 4,
      color: '#fff',
      fontSize: 12,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{ flexShrink: 0 }}>{name} ({progress}%)</span>
    </div>
  );
}

function WorkerPreview({ handle }: PreviewRendererProps) {
  const data = usePayload<WorkerPayload>(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);
  const { name, job, progress } = data || {};

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#4a90d9', borderRadius: 2 }} />
          <span style={{ color: '#4a90d9', fontWeight: 'bold' }}>Worker: {name}</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: '#aaa', fontSize: 12 }}>
        Job: {job}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{
          background: '#333',
          borderRadius: '4px',
          height: '6px',
          width: '100px',
          overflow: 'hidden',
        }}>
          <div style={{
            background: '#4a90d9',
            height: '100%',
            width: `${progress}%`,
          }} />
        </div>
        <span style={{ color: '#888', fontSize: 11 }}>{progress}%</span>
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
    "Worker": {
      full: WorkerFull,
      ref: WorkerRef,
      preview: WorkerPreview,
    },
  }
} as ThingWatcherRenderConfig;
