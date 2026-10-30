// FileListIndicator.tsx - File list renderer for Clydra
// Typecheck with: deno check FileListIndicator.tsx
import { useEffect, useRef } from "react";
import type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  RendererSet,
} from "thingwatcher-bindings";
import { useDataChunks } from "thingwatcher-bindings";

export const thingwatcherDialect = "0.1";

interface ChunkData {
  cwd?: string;
  files?: string[];
  count?: number;
}

interface FileListArgs {
  onFilesChange?: (files: string[]) => void;
}

const styles = {
  container: {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "2px 6px",
    background: "#1a2a1a",
    borderRadius: 4,
    fontSize: "10px",
    color: "#6a9a6a",
  },
};

export function FullRenderer({ handle, args }: FullRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const typedArgs = args as FileListArgs;

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const files = lastChunk?.files ?? [];
  const count = lastChunk?.count ?? 0;

  // Track previous files to avoid calling onFilesChange on every render
  const prevFilesRef = useRef<string[]>([]);

  useEffect(() => {
    if (!typedArgs.onFilesChange) return;

    // Simple comparison: check if files array changed
    const prevFiles = prevFilesRef.current;
    if (
      files.length !== prevFiles.length ||
      files.some((f, i) => f !== prevFiles[i])
    ) {
      prevFilesRef.current = files;
      typedArgs.onFilesChange(files);
    }
  }, [files, typedArgs.onFilesChange]);

  return (
    <div style={styles.container}>
      <span>{count} files</span>
    </div>
  );
}

// =============================================================================
// Ref renderers
// =============================================================================

export function RefRenderer({ handle }: RefRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const count = lastChunk?.count ?? 0;

  return (
    <span
      style={{
        color: "#6a9a6a",
        fontFamily: "monospace",
        fontSize: "10px",
      }}
    >
      {count} files
    </span>
  );
}

export function InlineRefRenderer({ handle, args }: RefRendererProps) {
  return RefRenderer({ handle, args });
}

export function PreviewRenderer({ handle }: PreviewRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const count = lastChunk?.count ?? 0;
  const cwd = lastChunk?.cwd ?? "";

  return (
    <div style={{ fontSize: "11px", color: "#888" }}>
      <div>Files: {count}</div>
      <div style={{ fontSize: "10px", color: "#666" }}>CWD: {cwd}</div>
    </div>
  );
}

// Renderer set for use with ThingWatcherRenderConfig
export const renderers = {
  full: FullRenderer,
  ref: RefRenderer,
  inline: InlineRefRenderer,
  preview: PreviewRenderer,
} as RendererSet;
