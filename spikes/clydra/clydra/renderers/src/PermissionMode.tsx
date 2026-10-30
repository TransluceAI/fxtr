// PermissionMode.tsx - Permission mode selector renderer for Clydra
// Typecheck with: deno check PermissionMode.tsx
import { useEffect } from "react";
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
  current_mode?: string;
}

const MODES = [
  { value: "default", label: "Ask" },
  { value: "acceptEdits", label: "Accept Edits" },
  { value: "plan", label: "Plan" },
] as const;

const styles = {
  container: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  label: {
    color: "#666",
    fontSize: "11px",
  },
  select: {
    background: "#1a1a1a",
    color: "#e0e0e0",
    border: "1px solid #333",
    padding: "2px 6px",
    fontFamily: "monospace",
    fontSize: "11px",
  },
  hint: {
    color: "#444",
    fontSize: "10px",
  },
};

export function FullRenderer({ handle }: FullRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const currentMode = lastChunk?.current_mode ?? "default";

  const handleChange = async (newMode: string) => {
    if (triggerable) {
      await trigger({ mode: newMode });
    }
  };

  // Shift+Tab cycles through modes
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.shiftKey && e.key === "Tab") {
        e.preventDefault();
        const currentIndex = MODES.findIndex((m) => m.value === currentMode);
        const nextIndex = (currentIndex + 1) % MODES.length;
        handleChange(MODES[nextIndex].value);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [currentMode, triggerable]);

  return (
    <div style={styles.container}>
      <span style={styles.label}>Mode:</span>
      <select
        value={currentMode}
        onChange={(e) => handleChange(e.target.value)}
        disabled={!triggerable}
        style={{
          ...styles.select,
          cursor: triggerable ? "pointer" : "default",
        }}
      >
        {MODES.map(({ value, label }) => (
          <option key={value} value={value}>
            {label}
          </option>
        ))}
      </select>
      <span style={styles.hint}>(Shift+Tab)</span>
    </div>
  );
}

// =============================================================================
// Ref renderers
// =============================================================================

export function RefRenderer({ handle }: RefRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const currentMode = lastChunk?.current_mode ?? "default";
  const modeLabel = MODES.find((m) => m.value === currentMode)?.label ?? currentMode;

  return (
    <span
      style={{
        color: "#888",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Mode: {modeLabel}
    </span>
  );
}

export function InlineRefRenderer({ handle }: RefRendererProps) {
  return RefRenderer({ handle });
}

export function PreviewRenderer({ handle }: PreviewRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const currentMode = lastChunk?.current_mode ?? "default";
  const modeLabel = MODES.find((m) => m.value === currentMode)?.label ?? currentMode;

  return (
    <span
      style={{
        color: "#888",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Mode: {modeLabel}
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
