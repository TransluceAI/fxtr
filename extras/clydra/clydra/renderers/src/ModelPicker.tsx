// ModelPicker.tsx - Model selector renderer for Clydra
// Typecheck with: deno check ModelPicker.tsx
import { useState, useEffect } from "react";
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
  current_model?: string;
  error?: boolean;
}

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
  input: {
    background: "#1a1a1a",
    border: "1px solid #333",
    padding: "2px 6px",
    fontFamily: "monospace",
    fontSize: "11px",
    width: "180px",
  },
};

export function FullRenderer({ handle }: FullRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const currentModel = lastChunk?.current_model ?? "";
  const hasError = lastChunk?.error ?? false;

  const [inputValue, setInputValue] = useState("");
  const [isFocused, setIsFocused] = useState(false);

  // Clear input when currentModel changes (successful update)
  useEffect(() => {
    setInputValue("");
  }, [currentModel]);

  const handleSubmit = async () => {
    const trimmed = inputValue.trim();
    if (!trimmed || !triggerable) {
      return;
    }
    await trigger({ model: trimmed });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleSubmit();
    }
  };

  // Determine text color based on state
  const hasUnsavedContent = inputValue.trim().length > 0;
  const textColor = hasUnsavedContent ? "#f5a623" : "#000"; // orange for unsaved, black for placeholder

  return (
    <div style={styles.container}>
      <span style={styles.label}>Model:</span>
      <input
        type="text"
        value={inputValue}
        onChange={(e) => setInputValue(e.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => setIsFocused(true)}
        onBlur={() => setIsFocused(false)}
        placeholder={currentModel || "default"}
        disabled={!triggerable}
        style={{
          ...styles.input,
          color: textColor,
          borderColor: hasError ? "#d9534f" : "#333",
          cursor: triggerable ? "text" : "default",
        }}
      />
    </div>
  );
}

// =============================================================================
// Ref renderers
// =============================================================================

export function RefRenderer({ handle }: RefRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const currentModel = lastChunk?.current_model ?? "";

  return (
    <span
      style={{
        color: "#888",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Model: {currentModel || "default"}
    </span>
  );
}

export function InlineRefRenderer({ handle }: RefRendererProps) {
  return RefRenderer({ handle });
}

export function PreviewRenderer({ handle }: PreviewRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const currentModel = lastChunk?.current_model ?? "";

  return (
    <span
      style={{
        color: "#888",
        fontFamily: "monospace",
        fontSize: "11px",
      }}
    >
      Model: {currentModel || "default"}
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
