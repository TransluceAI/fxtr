// Demo renderer using global CSS (non-module)
// This demonstrates:
// 1. Local global CSS with bare classnames like className="global-box"
// 2. Remote CSS fetched from a CDN URL

import './GlobalCss.css';  // Local global CSS import - no module binding
// Remote CSS import - fetched and bundled at compile time
import 'https://cdn.jsdelivr.net/gh/AVGVSTVS96/react-shiki@react-shiki@0.9.1/package/src/lib/styles.css';
import { usePayload, useDraggableIdMarker } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

function GlobalCssFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);
  const data = payload as { name?: string; description?: string } | null;

  return (
    <div className="global-box">
      <DraggableIDMarker>
        <h3>{data?.name}</h3>
      </DraggableIDMarker>
      <p>{data?.description}</p>
      {/* Using .languageLabel from remote CSS - should be monospace font */}
      <div style={{position: 'relative'}}>
        <div className="languageLabel">
          This text uses .languageLabel from remote CDN CSS (should be monospace)
        </div>
      </div>
    </div>
  );
}

function GlobalCssRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;

  return (
    <span style={{ color: '#4a9e3f', fontWeight: 'bold' }}>
      {data?.name}
    </span>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "GlobalCss": {
      full: GlobalCssFull,
      ref: GlobalCssRef,
    },
  }
} as ThingWatcherRenderConfig;
