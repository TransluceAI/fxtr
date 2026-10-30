// Test renderer with an intentionally bad import to test error propagation
// This should show a meaningful error about failed external import
import Foo from "nonexistent-package-12345-abcdef";
import { usePayload } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

function BrokenImportFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { description?: string } | null;

  return (
    <div style={{ padding: '10px' }}>
      <div>{data?.description ?? 'Broken import test'}</div>
      <div>Using Foo: {String(Foo)}</div>
    </div>
  );
}

function BrokenImportRef({ handle }: RefRendererProps) {
  return <span>Broken Import</span>;
}

function BrokenImportInline({ handle }: RefRendererProps) {
  return <span>Broken Import</span>;
}

export default {
  dialect: "0.1",
  renderers: {
    "BrokenImport": {
      full: BrokenImportFull,
      ref: BrokenImportRef,
      inline: BrokenImportInline,
    },
  }
} as ThingWatcherRenderConfig;
