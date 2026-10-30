// InterruptButton.tsx - Interrupt button renderer for Clydra
// Typecheck with: deno check InterruptButton.tsx
import type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  RendererSet,
} from "thingwatcher-bindings";
import {
  useDataChunks,
  useTriggerable,
  useTrigger,
} from "thingwatcher-bindings";

export const thingwatcherDialect = "0.1";

interface ChunkData {
  enabled?: boolean;
}

const styles = {
  button: {
    width: "100%",
    color: "#e0e0e0",
    border: "1px solid #444",
    padding: "6px 12px",
    fontFamily: "monospace",
    fontSize: "12px",
  },
  enabled: {
    background: "#a33",
    cursor: "pointer",
    opacity: 1,
  },
  disabled: {
    background: "#222",
    cursor: "default",
    opacity: 0.4,
  },
};

export function FullRenderer({ handle }: FullRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const enabled = lastChunk?.enabled ?? false;

  const handleClick = async () => {
    if (triggerable && enabled) {
      await trigger({ interrupt: true });
    }
  };

  const isActive = triggerable && enabled;

  return (
    <button
      onClick={handleClick}
      disabled={!isActive}
      style={{
        ...styles.button,
        ...(isActive ? styles.enabled : styles.disabled),
      }}
    >
      {"\u23F9"} Interrupt
    </button>
  );
}

// =============================================================================
// Ref renderers
// =============================================================================

export function RefRenderer({ handle }: RefRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const enabled = lastChunk?.enabled ?? false;

  return (
    <span
      style={{
        color: enabled ? "#a33" : "#666",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Interrupt: {enabled ? "ready" : "disabled"}
    </span>
  );
}

export function InlineRefRenderer({ handle }: RefRendererProps) {
  return RefRenderer({ handle });
}

export function PreviewRenderer({ handle }: PreviewRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const enabled = lastChunk?.enabled ?? false;

  return (
    <span
      style={{
        color: enabled ? "#a33" : "#666",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Interrupt: {enabled ? "ready" : "disabled"}
    </span>
  );
}

// Renderer set for use with ThingWatcherRenderConfig
export const renderers = {
  full: FullRenderer,
  ref: RefRenderer,
  inline: InlineRefRenderer,
  preview: PreviewRenderer,
} as RendererSet;
