// Demo renderer using Twind (Tailwind-in-JS) for dynamic Tailwind CSS support
// This demonstrates:
// 1. usePersistentGlobalState for module-scoped shared state
// 2. Twind integration with shadow DOM via adoptedStyleSheets
// 3. Using a callback ref to attach observer when element mounts

import { useCallback } from 'react';
import { twind, cssom, defineConfig, mo as twindMutationObserver, TwindMutationObserver  } from 'https://esm.sh/@twind/core@1.1.3';
import presetTailwind from 'https://esm.sh/@twind/preset-tailwind@1.1.3';
import { usePayload, useDraggableIdMarker, usePersistentGlobalState, AdoptStyleSheet } from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps } from 'thingwatcher-bindings';

// Types for our global state (shared across module, not per-shadow-root)
interface TwindState {
  tw: ReturnType<typeof twind>;  // twind instance
  sheet: CSSStyleSheet;          // the stylesheet to attach to shadow roots
  obs: TwindMutationObserver;         // the observer to observe the shadow root
}

function TwindDemoFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  // Get or initialize twind state (shared across all TwindDemo instances in this module)
  const [twindState, setTwindState] = usePersistentGlobalState<TwindState>(handle, 'twind');

  // Initialize twind on first use (synchronous since imports are top-level)
  if (!twindState) {
    setTwindState((prev: TwindState | undefined) => {
      if (prev) return prev; // Race condition guard

      const sheet = new CSSStyleSheet();
      const tw = twind(defineConfig({
        presets: [presetTailwind()],
      }), cssom(sheet));
      const obs = twindMutationObserver(tw);
      const newTwindState = { tw, sheet, obs };
      console.log('Initializing twindState', newTwindState);
      return newTwindState;
    });
  }

  // Callback ref: attach twind observer when element mounts
  // (stylesheet is handled declaratively via AdoptStyleSheet)
  const containerRef = useCallback((node: HTMLDivElement | null) => {
    if (!node || !twindState) return;

    // Watch for changes to the ref - twind will process tailwind classes
    twindState.obs.observe(node);
  }, [twindState]);

  const data = payload as { title?: string; items?: string[] } | null;

  return (
    <>
      {/* Declaratively adopt twind's stylesheet into the shadow root */}
      {twindState && <AdoptStyleSheet sheet={twindState.sheet} />}
      <div ref={containerRef} className="p-4 bg-slate-800 rounded-lg">
      <DraggableIDMarker>
        <h3 className="text-xl font-bold text-blue-400">{data?.title ?? 'Twind Demo'}</h3>
      </DraggableIDMarker>
      <p className="text-slate-400 text-sm mt-1">
        This renderer uses Tailwind classes via twind!
      </p>
      {data?.items && data.items.length > 0 && (
        <ul className="mt-3 space-y-1">
          {data.items.map((item, i) => (
            <li
              key={i}
              className="px-3 py-2 bg-slate-700 rounded shadow-sm hover:bg-slate-600 text-slate-200 transition-colors"
            >
              {item}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-4 flex gap-2">
        <span className="px-2 py-1 bg-green-600 text-white text-xs rounded">Success</span>
        <span className="px-2 py-1 bg-yellow-600 text-white text-xs rounded">Warning</span>
        <span className="px-2 py-1 bg-red-600 text-white text-xs rounded">Error</span>
      </div>
    </div>
    </>
  );
}

function TwindDemoRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { title?: string } | null;

  // RefRenderer doesn't use twind - just simple inline styles
  return (
    <span style={{
      color: '#60a5fa',
      padding: '2px 6px',
      background: 'rgba(96, 165, 250, 0.1)',
      borderRadius: '3px',
      fontWeight: 'bold',
    }}>
      {data?.title ?? 'Twind Demo'}
    </span>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "TwindDemo": {
      full: TwindDemoFull,
      ref: TwindDemoRef,
    },
  }
} as ThingWatcherRenderConfig;
