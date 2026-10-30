// Test renderer that is missing the dialect property.
// This should fail with a clear error message about the missing dialect.
import { usePayload, useDraggableIdMarker } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps } from 'thingwatcher-bindings';

function BrokenDialectFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ padding: 12 }}>
      <DraggableIDMarker>
        <span style={{ color: '#f88' }}>BrokenDialect: Missing dialect property</span>
      </DraggableIDMarker>
      <pre>{JSON.stringify(payload, null, 2)}</pre>
    </div>
  );
}

// Intentionally missing dialect property - this should trigger a validation error
export default {
  renderers: {
    "BrokenDialect": { full: BrokenDialectFull },
  }
} as ThingWatcherRenderConfig;
