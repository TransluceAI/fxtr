// Test renderer that imports by bare module name (not full URL)
// This tests that bare imports correctly get the esm.sh prefix added
import ReactJson from "react-json-view@1.21.3";
import { usePayload } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

function BareImportFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string; json_data?: unknown } | null;

  return (
    <div style={{ padding: '10px' }}>
      <div style={{ fontWeight: 'bold', marginBottom: '8px' }}>
        {data?.name ?? 'Bare Import Test'}
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

function BareImportRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;

  return (
    <span style={{
      color: '#dcdcaa',
      padding: '2px 6px',
      background: 'rgba(220, 220, 170, 0.1)',
      borderRadius: '3px',
    }}>
      {data?.name ?? 'Bare Import'}
    </span>
  );
}

function BareImportInline({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;

  return (
    <span style={{ color: '#dcdcaa' }}>
      {data?.name ?? 'Bare Import'}
    </span>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "BareImport": {
      full: BareImportFull,
      ref: BareImportRef,
      inline: BareImportInline,
    },
  }
} as ThingWatcherRenderConfig;
