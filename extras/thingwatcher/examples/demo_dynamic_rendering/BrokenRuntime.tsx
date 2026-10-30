// This file compiles successfully but throws during render.
// Used to test runtime error handling in the ErrorBoundary.
import { usePayload } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps } from 'thingwatcher-bindings';

interface BrokenRuntimePayload {
  description?: string;
  crash_mode?: string;
}

function BrokenRuntimeFull({ handle }: FullRendererProps) {
  const payload = usePayload<BrokenRuntimePayload>(handle);

  // Intentionally throw an error during render
  if (payload?.crash_mode === 'always') {
    throw new Error('Intentional runtime error: This renderer always crashes!');
  }

  // Access undefined property to cause runtime error
  const value = (payload as any).nonexistent.deeply.nested.property;

  return (
    <div style={{ padding: '12px' }}>
      <div>This should never render</div>
      <div>Value: {value}</div>
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "BrokenRuntime": { full: BrokenRuntimeFull },
  }
} as ThingWatcherRenderConfig;
