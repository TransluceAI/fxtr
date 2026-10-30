/**
 * Fruit renderer - Simple draggable fruit display
 *
 * Used for Apple, Orange, and Banana thing types.
 * Demonstrates that things can be dragged from anywhere.
 */

import {
  usePayload,
  useThingMetadata,
  useDraggableIdMarker,
} from 'thingwatcher-bindings';
import type {
  ThingWatcherRenderConfig,
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
} from 'thingwatcher-bindings';

interface FruitPayload {
  name: string;
}

// Color mapping by fruit type
const FRUIT_COLORS: Record<string, string> = {
  Apple: '#e74c3c',
  Orange: '#f39c12',
  Banana: '#f1c40f',
};

const FRUIT_EMOJI: Record<string, string> = {
  Apple: '\u{1F34E}',   // Red apple
  Orange: '\u{1F34A}',  // Tangerine
  Banana: '\u{1F34C}',  // Banana
};

function FruitFull({ handle }: FullRendererProps) {
  const data = usePayload<FruitPayload>(handle);
  const { thingType, shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const color = FRUIT_COLORS[thingType] || '#888';
  const emoji = FRUIT_EMOJI[thingType] || '\u{1F34F}';

  return (
    <div style={{ padding: '16px' }}>
      <DraggableIDMarker>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '12px',
            padding: '16px',
            background: `${color}22`,
            border: `2px solid ${color}`,
            borderRadius: '8px',
            cursor: 'grab',
          }}
        >
          <span style={{ fontSize: '32px' }}>{emoji}</span>
          <div>
            <div style={{ fontSize: '18px', fontWeight: 'bold', color }}>
              {thingType}
            </div>
            <div style={{ fontSize: '14px', color: '#888' }}>
              {data?.name} #{shortId}
            </div>
          </div>
        </div>
      </DraggableIDMarker>
      <div style={{ marginTop: '12px', fontSize: '12px', color: '#666' }}>
        Drag this fruit onto a drop region!
      </div>
    </div>
  );
}

function FruitRef({ handle }: RefRendererProps) {
  const data = usePayload<FruitPayload>(handle);
  const { thingType } = useThingMetadata(handle);

  const color = FRUIT_COLORS[thingType] || '#888';
  const emoji = FRUIT_EMOJI[thingType] || '\u{1F34F}';

  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 10px',
        background: color,
        borderRadius: '4px',
        color: '#000',
        fontSize: '13px',
        fontWeight: 600,
      }}
    >
      <span>{emoji}</span>
      <span>{data?.name}</span>
    </div>
  );
}

function FruitPreview({ handle }: PreviewRendererProps) {
  const data = usePayload<FruitPayload>(handle);
  const { thingType, shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const color = FRUIT_COLORS[thingType] || '#888';
  const emoji = FRUIT_EMOJI[thingType] || '\u{1F34F}';

  return (
    <DraggableIDMarker>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          cursor: 'grab',
        }}
      >
        <span style={{ fontSize: '20px' }}>{emoji}</span>
        <span style={{ color, fontWeight: 'bold' }}>{thingType}</span>
        <span style={{ color: '#888' }}>{data?.name}</span>
        <span style={{ color: '#666', fontSize: '11px' }}>#{shortId}</span>
      </div>
    </DraggableIDMarker>
  );
}

// Export for all fruit types - they share the same renderer
export default {
  dialect: '0.1',
  renderers: {
    Apple: {
      full: FruitFull,
      ref: FruitRef,
      preview: FruitPreview,
    },
    Orange: {
      full: FruitFull,
      ref: FruitRef,
      preview: FruitPreview,
    },
    Banana: {
      full: FruitFull,
      ref: FruitRef,
      preview: FruitPreview,
    },
  },
} as ThingWatcherRenderConfig;
