// Test renderer that imports from a full esm.sh URL
// This tests that full URLs are passed through unchanged (not double-prefixed)
import ReactJson from "https://esm.sh/react-json-view@1.21.3?external=react";
import { usePayload } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

function ExternalImportFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string; json_data?: unknown } | null;

  return (
    <div style={{ padding: '10px' }}>
      <div style={{ fontWeight: 'bold', marginBottom: '8px' }}>
        {data?.name ?? 'External Import Test'}
      </div>
      <div style={{
        background: '#1e1e1e',
        borderRadius: '4px',
        padding: '8px',
        fontSize: '12px'
      }}>
        <ReactJson
          src={data?.json_data ?? { test: 'data', nested: { value: 42 } }}
          theme="monokai"
          displayDataTypes={false}
          enableClipboard={false}
          collapsed={1}
        />
      </div>
    </div>
  );
}

function ExternalImportRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;

  return (
    <span style={{
      color: '#9cdcfe',
      padding: '2px 6px',
      background: 'rgba(156, 220, 254, 0.1)',
      borderRadius: '3px',
    }}>
      {data?.name ?? 'External Import'}
    </span>
  );
}

function ExternalImportInline({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;

  return (
    <span style={{ color: '#9cdcfe' }}>
      {data?.name ?? 'External Import'}
    </span>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "ExternalImport": {
      full: ExternalImportFull,
      ref: ExternalImportRef,
      inline: ExternalImportInline,
    },
  }
} as ThingWatcherRenderConfig;
