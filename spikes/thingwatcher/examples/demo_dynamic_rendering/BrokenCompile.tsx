// This file intentionally has broken TSX to test compile error display.
// It will fail Deno typechecking AND esbuild compilation.
import { usePayload } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps } from 'thingwatcher-bindings';

function BrokenCompileFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { description?: string } | null;

  return (
    <div style={{ padding: '12px' }}>
      <div>Description: {data?.description}</div>
      {/* Syntax error: missing closing brace */}
      <span style={{ color: 'red' }
      This will not compile
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "BrokenCompile": { full: BrokenCompileFull },
  }
} as ThingWatcherRenderConfig;
