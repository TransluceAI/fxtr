// React is auto-imported by JSX transform
import {
  useDataChunks,
  useLinks,
  useDraggableIdMarker,
  useThingMetadata,
  InlineNodeRef,
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';

interface LogEntry {
  timestamp: number;
  level: string;
  message: string;
}

interface LogChunk {
  name?: string;
  entries?: LogEntry[];
}

// This renderer uses dataChunks (not payload) because LogAccumulator
// accumulates entries over time via ThingDelta append mode.
function LogAccumulatorFull({ handle }: FullRendererProps) {
  const dataChunks = useDataChunks<LogChunk>(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  // Flatten all entries from all chunks
  // Each chunk has shape: { name: string, entries: [...] } or { entries: [...] }
  const allEntries = dataChunks.flatMap(chunk => chunk?.entries || []);

  const levelColors: Record<string, string> = {
    info: '#4ecdc4',
    warn: '#f9ca24',
    error: '#eb4d4b',
    debug: '#686de0',
  };

  const formatTime = (ts: number) => {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  };

  return (
    <div style={{ padding: '12px' }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: '12px',
        paddingBottom: '8px',
        borderBottom: '1px solid #333'
      }}>
        <DraggableIDMarker>
          <span style={{ fontWeight: 'bold', color: '#4ecdc4', cursor: 'grab' }}>Log Accumulator</span>
        </DraggableIDMarker>
        <span style={{ fontSize: '12px', color: '#888' }}>
          {allEntries.length} entries across {dataChunks.length} chunk(s)
        </span>
      </div>

      <div style={{
        maxHeight: '300px',
        overflowY: 'auto',
        fontFamily: 'monospace',
        fontSize: '12px'
      }}>
        {allEntries.length === 0 ? (
          <div style={{ color: '#666', fontStyle: 'italic' }}>No log entries yet</div>
        ) : (
          allEntries.map((entry, i) => (
            <div key={i} style={{
              display: 'flex',
              gap: '8px',
              padding: '4px 0',
              borderBottom: '1px solid #222'
            }}>
              <span style={{ color: '#666', minWidth: '80px' }}>
                {formatTime(entry.timestamp)}
              </span>
              <span style={{
                color: levelColors[entry.level] || '#888',
                minWidth: '50px',
                textTransform: 'uppercase'
              }}>
                {entry.level}
              </span>
              <span style={{ color: '#ddd' }}>{entry.message}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function LogAccumulatorRef({ handle }: RefRendererProps) {
  // Block ref - fills width, shows summary
  const dataChunks = useDataChunks<LogChunk>(handle);
  const allEntries = dataChunks.flatMap(chunk => chunk?.entries || []);

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#1a3a3a',
      borderRadius: 4,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: '#4ecdc4',
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: '#4ecdc4', flexShrink: 0 }}>Log</span>
      <span style={{
        color: '#888',
        flex: 1,
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        {allEntries.length} entries across {dataChunks.length} chunk(s)
      </span>
    </div>
  );
}

function LogAccumulatorInline({ handle }: RefRendererProps) {
  // Inline ref - minimal, flows with text
  const dataChunks = useDataChunks<LogChunk>(handle);
  const allEntries = dataChunks.flatMap(chunk => chunk?.entries || []);

  return (
    <span style={{ color: '#4ecdc4' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#4ecdc4',
        borderRadius: 2,
        marginRight: 4,
      }} />
      Log ({allEntries.length})
    </span>
  );
}

function LogAccumulatorPreview({ handle }: PreviewRendererProps) {
  // Preview for hover cards with draggable ID and links
  const dataChunks = useDataChunks<LogChunk>(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const allEntries = dataChunks.flatMap(chunk => chunk?.entries || []);
  const lastEntry = allEntries[allEntries.length - 1];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#4ecdc4', borderRadius: 2 }} />
          <span style={{ color: '#4ecdc4', fontWeight: 'bold' }}>Log Accumulator</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: '#aaa', fontSize: 12 }}>
        {allEntries.length} entries across {dataChunks.length} chunk(s)
      </div>
      {lastEntry && (
        <div style={{ color: '#666', fontSize: 11, fontFamily: 'monospace' }}>
          [{lastEntry.level}] {lastEntry.message?.substring(0, 40)}...
        </div>
      )}
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
    "LogAccumulator": {
      full: LogAccumulatorFull,
      ref: LogAccumulatorRef,
      inline: LogAccumulatorInline,
      preview: LogAccumulatorPreview,
    },
  }
} as ThingWatcherRenderConfig;
