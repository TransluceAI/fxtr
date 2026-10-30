// ClydraRoot.tsx - Main conversation renderer for Clydra
import { useState, useRef, useEffect, createContext, useContext, useCallback, useMemo } from "react";
import { createPortal } from "react-dom";
// npm packages (bundled by esbuild)
import JsonView from "react18-json-view";
import "react18-json-view/src/style.css";
import { Streamdown, defaultRehypePlugins } from "streamdown";
import { defaultSchema } from "hast-util-sanitize";
import { twind, cssom, defineConfig, mo as twindMutationObserver, type TwindMutationObserver } from "@twind/core";
import presetTailwind from "@twind/preset-tailwind";
// CodeMirror imports (bundled by esbuild)
import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import { EditorView, ViewPlugin, Decoration, WidgetType, keymap } from "@codemirror/view";
import type { DecorationSet, ViewUpdate } from "@codemirror/view";
import { Prec } from "@codemirror/state";
import type { Range } from "@codemirror/state";
import { autocompletion, acceptCompletion, completionStatus } from "@codemirror/autocomplete";
import type { CompletionContext, CompletionResult } from "@codemirror/autocomplete";
// react-shiki stays as esm.sh (external)
import "https://cdn.jsdelivr.net/gh/AVGVSTVS96/react-shiki@react-shiki@0.9.1/package/src/lib/styles.css";
import type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  RendererSet,
} from "thingwatcher-bindings";
import {
  ChildNodeView,
  InlineNodeRef,
  useDataChunks,
  useTriggerable,
  useTrigger,
  useDraggableIdMarker,
  useThingMetadata,
  usePersistentGlobalState,
  useThingDropRegion,
  AdoptStyleSheet,
} from "thingwatcher-bindings";
import {
  getLanguageFromPath,
  ViewModePicker,
  SyntaxHighlightedCode,
  formattingStyles,
  WriteContentView,
  EditDiffView,
} from "./formatting.tsx";

// =============================================================================
// Streamdown configuration for tw-k thing references
// =============================================================================

// Augment JSX to recognize tw-k as a valid element
declare global {
  namespace JSX {
    interface IntrinsicElements {
      "tw-k": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
    }
  }
}

/**
 * Extended sanitize schema that allows <tw-k> elements.
 * This lets us render InlineNodeRef for thing references in markdown.
 */
const twkSanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames || []), "tw-k"],
};

// Extract the sanitize plugin function from the tuple
const sanitizePlugin = (defaultRehypePlugins.sanitize as [unknown, unknown])[0];

/**
 * Custom rehype plugins array for Streamdown with tw-k support.
 * Uses extended sanitize schema to allow tw-k elements through.
 * Type assertion needed because unified's Pluggable type is complex.
 */
const streamdownRehypePlugins = [
  defaultRehypePlugins.raw,
  [sanitizePlugin, twkSanitizeSchema],
  defaultRehypePlugins.katex,
  defaultRehypePlugins.harden,
] as Parameters<typeof Streamdown>[0]["rehypePlugins"];

/**
 * Custom tw-k component that renders InlineNodeRef for thing references.
 * The children contain the JSON key for the thing.
 *
 * Uses a hidden-but-selectable span trick so that copying text includes
 * the original <tw-k>JSON</tw-k> syntax rather than the visual representation.
 */
function TwKComponent({ children }: JSX.IntrinsicElements["tw-k"]) {
  const keyJSON = String(children || "");
  const originalText = `<tw-k>${keyJSON}</tw-k>`;

  try {
    const parsed = JSON.parse(keyJSON);
    return (
      <span style={{ position: "relative", display: "inline" }}>
        {/* Hidden but selectable original text - copied when selected */}
        <span
          style={{
            position: "absolute",
            opacity: 0,
            pointerEvents: "none",
            userSelect: "text",
            whiteSpace: "pre",
          }}
        >
          {originalText}
        </span>
        {/* Visual representation - not included in selection */}
        <span style={{ userSelect: "none" }}>
          <InlineNodeRef keyJSON={parsed} />
        </span>
      </span>
    );
  } catch {
    return <span style={{ color: "#ff6b6b" }}>[Invalid ref]</span>;
  }
}

/**
 * Components map for Streamdown to render tw-k elements as InlineNodeRef.
 */
const streamdownComponents = {
  "tw-k": TwKComponent,
};

// =============================================================================
// CodeMirror configuration
// =============================================================================

// Dark theme for CodeMirror matching our UI
const codemirrorTheme = EditorView.theme({
  "&": {
    backgroundColor: "#1a1a1a",
    color: "#e0e0e0",
    fontSize: "13px",
    fontFamily: "monospace",
  },
  ".cm-content": {
    caretColor: "#e0e0e0",
    padding: "8px",
  },
  ".cm-cursor": {
    borderLeftColor: "#e0e0e0",
  },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
    backgroundColor: "#3a4a5a",
  },
  ".cm-gutters": {
    display: "none",
  },
  ".cm-activeLine": {
    backgroundColor: "transparent",
  },
  ".cm-activeLineGutter": {
    backgroundColor: "transparent",
  },
  // Style for mentions autocomplete
  ".cm-tooltip-autocomplete": {
    backgroundColor: "#1a1a1a !important",
    border: "1px solid #333 !important",
  },
  ".cm-tooltip-autocomplete ul li": {
    color: "#e0e0e0",
  },
  ".cm-tooltip-autocomplete ul li[aria-selected]": {
    backgroundColor: "#3a4a5a",
  },
}, { dark: true });

// =============================================================================
// Widget registry for tw-k decorations
// =============================================================================

/**
 * Registry for tracking tw-k widget containers.
 * ClydraRoot subscribes to this and renders portals into the containers.
 */
const widgetRegistry = {
  containers: new Map<string, { element: HTMLElement; keyJSON: unknown }>(),
  listeners: new Set<() => void>(),

  register(id: string, element: HTMLElement, keyJSON: unknown) {
    this.containers.set(id, { element, keyJSON });
    this.listeners.forEach((fn) => fn());
  },

  unregister(id: string) {
    this.containers.delete(id);
    this.listeners.forEach((fn) => fn());
  },

  subscribe(fn: () => void) {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  },

  getAll() {
    return Array.from(this.containers.entries());
  },
};

let widgetIdCounter = 0;

/**
 * Widget for rendering InlineNodeRef inside CodeMirror.
 * Creates a placeholder DOM element; actual React content is rendered
 * via portal from ClydraRoot to preserve context.
 */
class ThingRefWidget extends WidgetType {
  private id: string;

  constructor(private keyJSON: unknown) {
    super();
    this.id = `tw-k-${++widgetIdCounter}`;
  }

  toDOM() {
    const container = document.createElement("span");
    container.className = "tw-inline-ref";
    container.style.cssText = "display: inline-block; vertical-align: middle;";
    container.id = this.id;

    // Register for portal rendering by ClydraRoot
    widgetRegistry.register(this.id, container, this.keyJSON);

    return container;
  }

  override destroy(_dom: HTMLElement) {
    widgetRegistry.unregister(this.id);
  }

  override eq(other: WidgetType) {
    return JSON.stringify(this.keyJSON) === JSON.stringify((other as ThingRefWidget).keyJSON);
  }

  override ignoreEvent() {
    return false;
  }
}

/**
 * ViewPlugin that finds <tw-k>JSON</tw-k> patterns and replaces them with InlineNodeRef widgets
 */
function createTwkDecorationPlugin() {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = this.buildDecorations(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged) {
        this.decorations = this.buildDecorations(update.view);
      }
    }

    buildDecorations(view: EditorView) {
      const widgets: Range<Decoration>[] = [];
      const doc = view.state.doc.toString();
      const regex = /<tw-k>([\s\S]*?)<\/tw-k>/g;
      let match;
      while ((match = regex.exec(doc)) !== null) {
        try {
          const keyJSON = JSON.parse(match[1]);
          widgets.push(
            Decoration.replace({
              widget: new ThingRefWidget(keyJSON),
            }).range(match.index, match.index + match[0].length)
          );
        } catch {
          // Invalid JSON, skip this match
        }
      }
      return Decoration.set(widgets, true);
    }
  }, { decorations: (v: { decorations: DecorationSet }) => v.decorations });
}

// Twind state shared across all instances
interface TwindState {
  tw: ReturnType<typeof twind>;
  sheet: CSSStyleSheet;
  obs: TwindMutationObserver;
}

export const thingwatcherDialect = "0.1";

// =============================================================================
// Types
// =============================================================================

interface ContentBlock {
  type: string;
  text?: string;
  thinking?: string;
  signature?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: string | unknown[];
  is_error?: boolean;
}

interface StreamEventData {
  type: string;
  index?: number;
  content_block?: {
    type: string;
    text?: string;
    thinking?: string;
    // tool_use fields
    id?: string;
    name?: string;
  };
  delta?: {
    type: string;
    text?: string;
    thinking?: string;
    // input_json_delta field
    partial_json?: string;
  };
}

interface SDKEvent {
  // Explicit event type (added by us)
  _event_type?: string;
  // Live SDK events (from dataclasses.asdict)
  content?: string | ContentBlock[];
  model?: string;
  session_id?: string;
  duration_ms?: number;
  total_cost_usd?: number;
  num_turns?: number;
  is_error?: boolean;
  // History events (from transcript parsing)
  uuid?: string;
  timestamp?: string;
  raw?: unknown;
  // StreamEvent fields
  event?: StreamEventData;
  parent_tool_use_id?: string | null;
}

interface ChunkData {
  events?: SDKEvent[];
  state?: string;
  session_id?: string;
  transcript_path?: string;
}

// =============================================================================
// Styles
// =============================================================================

const styles = {
  container: {
    background: "#0a0a0a",
    color: "#e0e0e0",
    fontFamily: "monospace",
    fontSize: "13px",
    lineHeight: "1.4",
    height: "100%",
    display: "flex",
    flexDirection: "column" as const,
  },
  conversationArea: {
    flex: 1,
    overflow: "auto",
    padding: "8px",
  },
  inputArea: {
    borderTop: "1px solid #333",
    padding: "8px",
    display: "flex",
    gap: "8px",
  },
  input: {
    flex: 1,
    background: "#1a1a1a",
    color: "#e0e0e0",
    border: "1px solid #333",
    padding: "8px",
    fontFamily: "inherit",
    fontSize: "inherit",
    resize: "none" as const,
    minHeight: "40px",
    maxHeight: "200px",
    overflow: "auto",
    whiteSpace: "pre-wrap" as const,
    wordWrap: "break-word" as const,
  },
  sendButton: {
    background: "#333",
    color: "#e0e0e0",
    border: "1px solid #444",
    padding: "8px 16px",
    fontFamily: "inherit",
    cursor: "pointer",
  },
  userMessage: {
    background: "#1a2a3a",
    border: "1px solid #2a4a6a",
    padding: "8px",
    marginBottom: "8px",
  },
  assistantMessage: {
    background: "#1a1a1a",
    border: "1px solid #333",
    padding: "8px",
    marginBottom: "8px",
  },
  systemMessage: {
    color: "#666",
    fontStyle: "italic" as const,
    padding: "4px 8px",
    marginBottom: "8px",
  },
  resultMessage: {
    color: "#666",
    fontSize: "11px",
    padding: "4px 8px",
    borderTop: "1px solid #222",
    marginTop: "4px",
  },
  details: {
    marginTop: "4px",
  },
  summary: {
    cursor: "pointer",
    color: "#888",
    fontSize: "12px",
  },
  codeBlock: {
    background: "#111",
    border: "1px solid #333",
    padding: "8px",
    overflow: "auto",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
    fontSize: "12px",
    marginTop: "4px",
  },
  label: {
    color: "#888",
    marginRight: "8px",
  },
  toolName: {
    color: "#8be9fd",
  },
  thinking: {
    color: "#6272a4",
    fontStyle: "italic" as const,
  },
  error: {
    color: "#ff5555",
  },
  streamingBox: {
    background: "#1a1a1a",
    border: "1px solid #4a90d9",
    borderLeft: "3px solid #4a90d9",
    padding: "8px",
    marginBottom: "8px",
  },
  streamingText: {
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
  },
  streamingThinking: {
    color: "#6272a4",
    fontStyle: "italic" as const,
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
  },
  streamingIndicator: {
    display: "inline-block",
    width: 8,
    height: 8,
    background: "#4ecdc4",
    borderRadius: "50%",
    marginRight: 8,
    animation: "pulse 1s infinite",
  },
  messageSummary: {
    cursor: "pointer",
    display: "flex",
    alignItems: "baseline",
    gap: "8px",
    listStyle: "none",  // Hide default triangle
  },
  triangle: {
    display: "inline-block",
    width: 0,
    height: 0,
    borderLeft: "5px solid transparent",
    borderRight: "5px solid transparent",
    borderTop: "6px solid #888",
    marginRight: "4px",
    transition: "transform 0.1s",
  },
  triangleOpen: {
    transform: "rotate(0deg)",
  },
  triangleClosed: {
    transform: "rotate(-90deg)",
  },
  messagePreview: {
    color: "#888",
    fontSize: "12px",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap" as const,
    flex: 1,
  },
  messageContent: {
    marginTop: "8px",
  },
  clickableSessionId: {
    cursor: "pointer",
    textDecoration: "underline",
    textDecorationStyle: "dotted" as const,
  },
};

// =============================================================================
// Utility functions for syntax highlighting
// =============================================================================

/**
 * Parse Read/Edit tool result which has format "     N→content" per line.
 * Separates content into: text above code region, code content, text below code region.
 * Read results typically have system reminders below; Edit results have status text above.
 */
function parseReadResult(text: string): {
  contentAbove: string;
  codeContent: string;
  startingLineNumber: number;
  contentBelow: string;
  originalText: string;
} {
  const lines = text.split("\n");
  const aboveLines: string[] = [];
  const codeLines: string[] = [];
  const belowLines: string[] = [];
  const lineNumberPattern = /^\s*(\d+)→(.*)$/;
  let startingLineNumber = 1;
  let foundFirstCodeLine = false;
  let foundLastCodeLine = false;

  for (const line of lines) {
    const match = line.match(lineNumberPattern);
    if (match) {
      // Line with arrow → code content
      if (!foundFirstCodeLine) {
        startingLineNumber = parseInt(match[1], 10);
        foundFirstCodeLine = true;
      }
      foundLastCodeLine = false; // Reset - we're still in code region
      codeLines.push(match[2]);
    } else {
      // Line without arrow
      if (!foundFirstCodeLine) {
        // Before code region
        aboveLines.push(line);
      } else {
        // After code region (or between code lines - treat as end of code)
        foundLastCodeLine = true;
        belowLines.push(line);
      }
    }
  }

  // Trim empty lines from edges
  while (aboveLines.length > 0 && !aboveLines[0].trim()) {
    aboveLines.shift();
  }
  while (aboveLines.length > 0 && !aboveLines[aboveLines.length - 1].trim()) {
    aboveLines.pop();
  }
  while (belowLines.length > 0 && !belowLines[0].trim()) {
    belowLines.shift();
  }
  while (belowLines.length > 0 && !belowLines[belowLines.length - 1].trim()) {
    belowLines.pop();
  }

  return {
    contentAbove: aboveLines.join("\n"),
    codeContent: codeLines.join("\n"),
    startingLineNumber,
    contentBelow: belowLines.join("\n"),
    originalText: text,
  };
}

/**
 * Find tool info (name and input) for a given tool_use_id by scanning events.
 */
function findToolInfoByUseId(
  events: SDKEvent[],
  toolUseId: string
): { toolName: string; input: Record<string, unknown> } | null {
  for (const event of events) {
    if (event._event_type === "AssistantMessage" && Array.isArray(event.content)) {
      for (const block of event.content) {
        const typedBlock = block as ContentBlock & { _block_type?: string };
        if (typedBlock._block_type === "ToolUseBlock" && typedBlock.id === toolUseId) {
          return {
            toolName: typedBlock.name || "",
            input: (typedBlock.input as Record<string, unknown>) || {},
          };
        }
      }
    }
  }
  return null;
}

// Context to pass events to nested components for tool lookups
const EventsContext = createContext<SDKEvent[]>([]);

// =============================================================================
// Helper components and functions
// =============================================================================

/**
 * JSON tree view component using react18-json-view
 */
function JsonTreeView({ data, collapsed = 1 }: { data: unknown; collapsed?: number | boolean }) {
  if (typeof data !== "object" || data === null) {
    return <span>{String(data)}</span>;
  }
  return (
    <JsonView
      src={data as object}
      theme="a11y"
      dark={true}
      collapsed={collapsed}
      style={{ background: "transparent", fontSize: "12px" }}
      enableClipboard={false}
    />
  );
}

/**
 * Get a preview string from message content (first ~60 chars of text)
 */
function getMessagePreview(content: string | ContentBlock[] | undefined): string {
  if (!content) return "";

  if (typeof content === "string") {
    const trimmed = content.trim().replace(/\n/g, " ");
    return trimmed.length > 60 ? trimmed.substring(0, 60) + "..." : trimmed;
  }

  if (Array.isArray(content)) {
    // Check if all tool results
    const allToolResults = content.every(
      (block) => (block as unknown as { _block_type?: string })._block_type === "ToolResultBlock"
    );
    if (allToolResults) {
      return `[${content.length} tool result${content.length === 1 ? "" : "s"}]`;
    }

    // Find first text or thinking block for preview
    for (const block of content) {
      if (block.text) {
        const trimmed = block.text.trim().replace(/\n/g, " ");
        return trimmed.length > 60 ? trimmed.substring(0, 60) + "..." : trimmed;
      }
      if (block.thinking) {
        const trimmed = block.thinking.trim().replace(/\n/g, " ");
        return `[Thinking] ${trimmed.length > 50 ? trimmed.substring(0, 50) + "..." : trimmed}`;
      }
      // Tool use block
      const blockType = (block as unknown as { _block_type?: string })._block_type;
      if (blockType === "ToolUseBlock" && block.name) {
        const desc = (block.input as Record<string, unknown> | undefined)?.description;
        if (desc && typeof desc === "string") {
          const trimmedDesc = desc.trim().replace(/\n/g, " ");
          return `[Tool: ${block.name}] ${trimmedDesc.length > 40 ? trimmedDesc.substring(0, 40) + "..." : trimmedDesc}`;
        }
        return `[Tool: ${block.name}]`;
      }
    }

    // Summarize block types
    const blockTypes = content.map(
      (b) => (b as unknown as { _block_type?: string })._block_type || "unknown"
    );
    return `[${blockTypes.join(", ")}]`;
  }

  return "";
}

// =============================================================================
// Content block renderers
// =============================================================================

function TextBlockRenderer({ block }: { block: ContentBlock }) {
  const [viewMode, setViewMode] = useState<"rich" | "raw">("rich");

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: "4px" }}>
        <ViewModePicker mode={viewMode} onChange={setViewMode} />
      </div>
      {viewMode === "rich" ? (
        <div className="streamdown-content">
          <Streamdown
            mode="static"
            rehypePlugins={streamdownRehypePlugins}
            components={streamdownComponents}
          >
            {block.text || ""}
          </Streamdown>
        </div>
      ) : (
        <div style={{ whiteSpace: "pre-wrap" }}>{block.text}</div>
      )}
    </div>
  );
}

function ThinkingBlockRenderer({ block }: { block: ContentBlock }) {
  return (
    <div style={{ marginTop: "8px" }}>
      <div style={{ ...styles.label, ...styles.thinking }}>Thinking:</div>
      <div style={{ ...styles.thinking, whiteSpace: "pre-wrap", marginTop: "4px" }}>
        {block.thinking}
      </div>
    </div>
  );
}

/**
 * View for Read tool results - shows syntax highlighted code.
 */
/**
 * Styled block for non-code content (status messages, system reminders, etc.)
 */
function NonCodeBlock({ content, style }: { content: string; style?: React.CSSProperties }) {
  return (
    <div style={{
      ...styles.codeBlock,
      color: "#888",
      fontSize: "11px",
      ...style,
    }}>
      <span style={{ whiteSpace: "pre-wrap" }}>{content}</span>
    </div>
  );
}

/**
 * View for Read/Edit tool results - shows syntax highlighted code with optional
 * content above (status messages) and below (system reminders).
 */
function ReadResultView({ content, filePath }: { content: string; filePath: string }) {
  const [viewMode, setViewMode] = useState<"rich" | "raw">("rich");
  const parsed = parseReadResult(content);
  const language = getLanguageFromPath(filePath);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: "4px" }}>
        <span style={{ color: "#666", fontSize: "11px", flex: 1 }}>{filePath}</span>
        <ViewModePicker mode={viewMode} onChange={setViewMode} />
      </div>
      {viewMode === "rich" ? (
        <>
          {parsed.contentAbove && (
            <NonCodeBlock content={parsed.contentAbove} style={{ marginBottom: "8px" }} />
          )}
          {parsed.codeContent && (
            <SyntaxHighlightedCode
              code={parsed.codeContent}
              language={language}
              startingLineNumber={parsed.startingLineNumber}
            />
          )}
          {parsed.contentBelow && (
            <NonCodeBlock content={parsed.contentBelow} style={{ marginTop: "8px" }} />
          )}
        </>
      ) : (
        <div style={styles.codeBlock}>
          <span style={{ whiteSpace: "pre-wrap" }}>{parsed.originalText}</span>
        </div>
      )}
    </div>
  );
}

function ToolUseBlockRenderer({ block }: { block: ContentBlock }) {
  const toolName = block.name || "";
  const input = block.input || {};

  // Raw view for Write/Edit tools
  const rawView = (
    <div style={styles.codeBlock}>
      <JsonTreeView data={input} />
    </div>
  );

  // Special handling for Write and Edit tools
  if (toolName === "Write") {
    return (
      <div style={{ marginTop: "8px" }}>
        <div>
          <span style={styles.label}>Tool:</span>
          <span style={styles.toolName}>{toolName}</span>
        </div>
        <WriteContentView input={input} rawView={rawView} />
      </div>
    );
  }

  if (toolName === "Edit") {
    return (
      <div style={{ marginTop: "8px" }}>
        <div>
          <span style={styles.label}>Tool:</span>
          <span style={styles.toolName}>{toolName}</span>
        </div>
        <EditDiffView input={input} rawView={rawView} />
      </div>
    );
  }

  // Default: show JSON tree view
  return (
    <div style={{ marginTop: "8px" }}>
      <div>
        <span style={styles.label}>Tool:</span>
        <span style={styles.toolName}>{toolName}</span>
      </div>
      {rawView}
    </div>
  );
}

function ToolResultBlockRenderer({ block }: { block: ContentBlock }) {
  const events = useContext(EventsContext);
  const isString = typeof block.content === "string";
  const toolUseId = block.tool_use_id || "";

  // Look up the tool info to determine if this is a Read or Edit result
  const toolInfo = toolUseId ? findToolInfoByUseId(events, toolUseId) : null;
  const toolName = toolInfo?.toolName || "";
  const isCodeResult = (toolName === "Read" || toolName === "Edit") && isString;
  const filePath = isCodeResult ? (toolInfo?.input?.file_path as string) || "" : "";

  return (
    <div style={{ marginTop: "8px" }}>
      <div>
        <span style={styles.label}>Result:</span>
        {block.is_error && <span style={styles.error}>[error] </span>}
      </div>
      {isCodeResult ? (
        <ReadResultView content={block.content as string} filePath={filePath} />
      ) : (
        <div style={styles.codeBlock}>
          {isString ? (
            <span style={{ whiteSpace: "pre-wrap" }}>{block.content as string}</span>
          ) : (
            <JsonTreeView data={block.content} />
          )}
        </div>
      )}
    </div>
  );
}

function ContentBlockRenderer({ block }: { block: ContentBlock }) {
  // Use _block_type field added by backend
  const blockType = (block as unknown as { _block_type?: string })._block_type;

  switch (blockType) {
    case "TextBlock":
      return <TextBlockRenderer block={block} />;
    case "ThinkingBlock":
      return <ThinkingBlockRenderer block={block} />;
    case "ToolUseBlock":
      return <ToolUseBlockRenderer block={block} />;
    case "ToolResultBlock":
      return <ToolResultBlockRenderer block={block} />;
    default:
      // Unknown or missing block type
      return (
        <details style={styles.details}>
          <summary style={styles.summary}>
            Unknown block: {blockType || Object.keys(block).join(", ")}
          </summary>
          <div style={styles.codeBlock}>
            <JsonTreeView data={block} />
          </div>
        </details>
      );
  }
}

// =============================================================================
// Event renderers
// =============================================================================

function UserMessageRenderer({ event }: { event: SDKEvent }) {
  const content = event.content;
  const isString = typeof content === "string";

  // Check if all content blocks are tool results
  const isAllToolResults =
    Array.isArray(content) &&
    content.length > 0 &&
    content.every(
      (block) =>
        (block as unknown as { _block_type?: string })._block_type === "ToolResultBlock"
    );

  // Tool result messages start collapsed
  const [isOpen, setIsOpen] = useState(!isAllToolResults);
  const [viewMode, setViewMode] = useState<"rich" | "raw">("rich");

  const label = isAllToolResults ? "Tool Result" : "You";
  const preview = getMessagePreview(content as string | ContentBlock[]);

  return (
    <details
      open={isOpen}
      onToggle={(e) => setIsOpen((e.target as HTMLDetailsElement).open)}
      style={styles.userMessage}
    >
      <summary style={styles.messageSummary}>
        <span style={{ ...styles.triangle, ...(isOpen ? styles.triangleOpen : styles.triangleClosed) }} />
        <span style={styles.label}>{label}:</span>
        {!isOpen && <span style={styles.messagePreview}>{preview}</span>}
      </summary>
      <div style={styles.messageContent}>
        {isString ? (
          <div>
            <div style={{ display: "flex", alignItems: "center", marginBottom: "4px" }}>
              <ViewModePicker mode={viewMode} onChange={setViewMode} />
            </div>
            {viewMode === "rich" ? (
              <div className="streamdown-content">
                <Streamdown
                  mode="static"
                  rehypePlugins={streamdownRehypePlugins}
                  components={streamdownComponents}
                >
                  {content}
                </Streamdown>
              </div>
            ) : (
              <div style={{ whiteSpace: "pre-wrap" }}>{content}</div>
            )}
          </div>
        ) : (
          Array.isArray(content) &&
          content.map((block, i) => (
            <ContentBlockRenderer key={i} block={block as ContentBlock} />
          ))
        )}
      </div>
    </details>
  );
}

function AssistantMessageRenderer({ event }: { event: SDKEvent }) {
  const content = event.content;

  // Check if message contains thinking or tool_use blocks (should start collapsed)
  const hasThinkingOrToolUse =
    Array.isArray(content) &&
    content.some((block) => {
      const blockType = (block as unknown as { _block_type?: string })._block_type;
      return blockType === "ThinkingBlock" || blockType === "ToolUseBlock";
    });

  // Messages with thinking/tool_use start collapsed
  const [isOpen, setIsOpen] = useState(!hasThinkingOrToolUse);
  const preview = getMessagePreview(content as string | ContentBlock[]);

  return (
    <details
      open={isOpen}
      onToggle={(e) => setIsOpen((e.target as HTMLDetailsElement).open)}
      style={styles.assistantMessage}
    >
      <summary style={styles.messageSummary}>
        <span style={{ ...styles.triangle, ...(isOpen ? styles.triangleOpen : styles.triangleClosed) }} />
        <span style={styles.label}>Claude:</span>
        {!isOpen && <span style={styles.messagePreview}>{preview}</span>}
      </summary>
      <div style={styles.messageContent}>
        {Array.isArray(content) ? (
          content.map((block, i) => (
            <ContentBlockRenderer key={i} block={block as ContentBlock} />
          ))
        ) : (
          <div style={{ whiteSpace: "pre-wrap" }}>{String(content)}</div>
        )}
      </div>
    </details>
  );
}

function ResultMessageRenderer({ event }: { event: SDKEvent }) {
  return (
    <details style={styles.details}>
      <summary style={styles.resultMessage}>
        Turn complete
        {event.total_cost_usd !== undefined &&
          ` | Cost: $${event.total_cost_usd.toFixed(4)}`}
        {event.duration_ms !== undefined &&
          ` | Duration: ${(event.duration_ms / 1000).toFixed(1)}s`}
      </summary>
      <div style={styles.codeBlock}>
        <JsonTreeView data={event} />
      </div>
    </details>
  );
}

function EventRenderer({ event }: { event: SDKEvent }) {
  // Use explicit _event_type field for reliable dispatch
  const eventType = event._event_type;

  switch (eventType) {
    case "user_message":
      return <UserMessageRenderer event={event} />;

    case "UserMessage":
      return <UserMessageRenderer event={event} />;

    case "AssistantMessage":
      return <AssistantMessageRenderer event={event} />;

    case "ResultMessage":
      return <ResultMessageRenderer event={event} />;

    case "SystemMessage":
      return (
        <details style={styles.details}>
          <summary style={styles.summary}>
            System: {(event as unknown as { subtype?: string }).subtype || "message"}
          </summary>
          <div style={styles.codeBlock}>
            <JsonTreeView data={event} />
          </div>
        </details>
      );

    case "unknown_history_event":
      return (
        <details style={styles.details}>
          <summary style={styles.summary}>Unknown history event</summary>
          <div style={styles.codeBlock}>
            <JsonTreeView data={event.raw} />
          </div>
        </details>
      );

    case "ClydraInternalError":
      return (
        <div
          style={{
            padding: "8px 12px",
            margin: "8px 0",
            background: "#3d1a1a",
            border: "1px solid #d9534f",
            borderRadius: "4px",
            color: "#ff6b6b",
            fontFamily: "monospace",
            fontSize: "12px",
          }}
        >
          Clydra Error: {(event as unknown as { message?: string }).message || "Unknown error"}
        </div>
      );

    default:
      // Fallback for events without _event_type (shouldn't happen)
      return (
        <details style={styles.details}>
          <summary style={styles.summary}>
            Unknown event: {eventType || "(no type)"}
          </summary>
          <div style={styles.codeBlock}>
            <JsonTreeView data={event} />
          </div>
        </details>
      );
  }
}

// =============================================================================
// Streaming content aggregation
// =============================================================================

interface StreamingBlock {
  type: "text" | "thinking" | "tool_use";
  content: string;
  toolName?: string;
  toolId?: string;
}

interface StreamingState {
  blocks: StreamingBlock[];
  currentBlockIndex: number | null;
  isComplete: boolean;
}

/**
 * Aggregate StreamEvent messages into displayable streaming content.
 * Returns the accumulated text/thinking blocks from consecutive StreamEvents.
 */
function aggregateStreamEvents(events: SDKEvent[]): StreamingState {
  const state: StreamingState = {
    blocks: [],
    currentBlockIndex: null,
    isComplete: false,
  };

  // Find consecutive StreamEvents at the end of the events array
  let streamStartIndex = events.length;
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i]._event_type === "StreamEvent") {
      streamStartIndex = i;
    } else {
      break;
    }
  }

  // Process StreamEvents
  for (let i = streamStartIndex; i < events.length; i++) {
    const event = events[i];
    if (event._event_type !== "StreamEvent" || !event.event) continue;

    const streamEvent = event.event;

    switch (streamEvent.type) {
      case "content_block_start": {
        const block = streamEvent.content_block;
        if (block?.type === "text") {
          state.blocks.push({ type: "text", content: block.text || "" });
          state.currentBlockIndex = state.blocks.length - 1;
        } else if (block?.type === "thinking") {
          state.blocks.push({ type: "thinking", content: block.thinking || "" });
          state.currentBlockIndex = state.blocks.length - 1;
        } else if (block?.type === "tool_use") {
          state.blocks.push({
            type: "tool_use",
            content: "",
            toolName: block.name || "unknown",
            toolId: block.id,
          });
          state.currentBlockIndex = state.blocks.length - 1;
        }
        break;
      }

      case "content_block_delta": {
        const delta = streamEvent.delta;
        if (state.currentBlockIndex !== null && state.currentBlockIndex < state.blocks.length) {
          if (delta?.type === "text_delta" && delta.text) {
            state.blocks[state.currentBlockIndex].content += delta.text;
          } else if (delta?.type === "thinking_delta" && delta.thinking) {
            state.blocks[state.currentBlockIndex].content += delta.thinking;
          } else if (delta?.type === "input_json_delta" && delta.partial_json) {
            state.blocks[state.currentBlockIndex].content += delta.partial_json;
          }
        }
        break;
      }

      case "content_block_stop":
        state.currentBlockIndex = null;
        break;

      case "message_stop":
        state.isComplete = true;
        break;
    }
  }

  return state;
}

function StreamingContentRenderer({ streamingState }: { streamingState: StreamingState }) {
  const [viewMode, setViewMode] = useState<"rich" | "raw">("rich");

  if (streamingState.blocks.length === 0) {
    return null;
  }

  return (
    <div style={styles.streamingBox}>
      <div style={{ ...styles.label, display: "flex", alignItems: "center" }}>
        {!streamingState.isComplete && (
          <span style={styles.streamingIndicator} />
        )}
        <span>Claude:</span>
        <ViewModePicker mode={viewMode} onChange={setViewMode} />
      </div>
      {streamingState.blocks.map((block, i) => (
        <div key={i}>
          {block.type === "thinking" ? (
            <details style={styles.details} open>
              <summary style={{ ...styles.summary, ...styles.thinking }}>
                Thinking...
              </summary>
              <div style={styles.streamingThinking}>{block.content}</div>
            </details>
          ) : block.type === "tool_use" ? (
            <details style={styles.details} open>
              <summary style={styles.summary}>
                <span style={styles.label}>Tool:</span>
                <span style={styles.toolName}>{block.toolName}</span>
              </summary>
              <div style={styles.codeBlock}>
                {block.content || "..."}
              </div>
            </details>
          ) : viewMode === "rich" ? (
            <div className="streamdown-content">
              <Streamdown
                isAnimating={!streamingState.isComplete}
                rehypePlugins={streamdownRehypePlugins}
                components={streamdownComponents}
              >
                {block.content}
              </Streamdown>
            </div>
          ) : (
            <div style={styles.streamingText}>{block.content}</div>
          )}
        </div>
      ))}
    </div>
  );
}

// =============================================================================
// Main renderer
// =============================================================================

export function FullRenderer({ handle }: FullRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);

  // Drop region for accepting dragged things into the input
  // (CodeMirror handles actual insertion - thingwatcher sets plain text to <tw-k>JSON</tw-k>)
  const { ThingDropRegion, mimeTypeForAnyThing } = useThingDropRegion(handle);

  // Initialize twind for Tailwind CSS support (used by Streamdown)
  const [twindState, setTwindState] = usePersistentGlobalState<TwindState>(handle, "twind");

  if (!twindState) {
    setTwindState((prev: TwindState | undefined) => {
      if (prev) return prev; // Race condition guard
      const sheet = new CSSStyleSheet();
      const tw = twind(defineConfig({
        presets: [presetTailwind()],
      }), cssom(sheet));
      const obs = twindMutationObserver(tw);
      return { tw, sheet, obs };
    });
  }

  // Callback ref: attach twind observer when element mounts
  const twindContainerRef = useCallback((node: HTMLDivElement | null) => {
    if (!node || !twindState) return;
    twindState.obs.observe(node);
  }, [twindState]);

  // Flatten events from all chunks
  const allEvents = dataChunks.flatMap(
    (chunk) => chunk?.events || []
  );

  // Separate non-stream events from stream events
  // StreamEvents are aggregated and displayed separately
  const displayEvents = allEvents.filter(
    (event) => event._event_type !== "StreamEvent"
  );

  // Aggregate streaming content from StreamEvents
  const streamingState = aggregateStreamEvents(allEvents);

  // Get current state from last chunk
  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const state = lastChunk?.state || "setup";
  const sessionId = lastChunk?.session_id;
  const transcriptPath = lastChunk?.transcript_path;

  // Detect if we're in a resumed session waiting for history
  const isResumedWithoutHistory = sessionId && allEvents.length === 0;

  const [inputText, setInputText] = useState("");
  const [fileList, setFileList] = useState<string[]>([]);
  const [slashCommands, setSlashCommands] = useState<string[]>([]);
  const [isInputDragOver, setIsInputDragOver] = useState(false);
  const conversationRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<{ view?: EditorView }>(null);
  const handleSendRef = useRef<() => void>(() => {});

  // Extract slash commands from init SystemMessage - only update state if commands actually changed
  useEffect(() => {
    for (const event of allEvents) {
      if (event._event_type === "SystemMessage") {
        console.log("Found SystemMessage event:", event);
        const data = (event as unknown as { data?: { subtype?: string; slash_commands?: string[] } }).data;
        console.log("SystemMessage data:", data, "subtype:", data?.subtype, "slash_commands:", data?.slash_commands);
        if (data?.subtype === "init" && Array.isArray(data.slash_commands)) {
          const newCommands = data.slash_commands;
          // Only update if different (compare stringified to handle array reference changes)
          setSlashCommands(prev => {
            if (JSON.stringify(prev) !== JSON.stringify(newCommands)) {
              console.log("Slash commands changed:", newCommands);
              return newCommands;
            }
            return prev;
          });
          return;
        }
      }
    }
  }, [allEvents]);

  // Create custom autocompletion for @ file mentions and / slash commands
  const customAutocompletion = useMemo(() => {
    // Completion source for @file mentions
    const fileCompletionSource = (context: CompletionContext): CompletionResult | null => {
      // Match @ followed by path characters (word chars, /, ., -)
      const word = context.matchBefore(/@[\w/.+-]*/);
      if (!word) return null;
      if (word.from === word.to && !context.explicit) return null;

      const query = word.text.slice(1).toLowerCase(); // Remove @ prefix
      const options = fileList
        .filter((file) => file.toLowerCase().includes(query))
        .slice(0, 50) // Limit results
        .map((file) => ({ label: `@${file}`, type: "file" }));

      return { from: word.from, options };
    };

    // Completion source for /slash commands
    const slashCompletionSource = (context: CompletionContext): CompletionResult | null => {
      // Match / followed by word characters (including -)
      const word = context.matchBefore(/\/[\w-]*/);
      console.log("slashCompletionSource called, word:", word);
      if (!word) return null;
      if (word.from === word.to && !context.explicit) {
        console.log("slashCompletionSource: word.from === word.to, returning null");
        return null;
      }

      // Only trigger at start of line (position 0) or after whitespace/newline
      if (word.from > 0) {
        const charBefore = context.state.sliceDoc(word.from - 1, word.from);
        console.log("slashCompletionSource: charBefore is", JSON.stringify(charBefore));
        if (!/[\s\n]/.test(charBefore)) {
          console.log("slashCompletionSource: not at start of line, returning null");
          return null;
        }
      }

      const query = word.text.slice(1).toLowerCase(); // Remove / prefix
      const options = slashCommands
        .filter((cmd) => cmd.toLowerCase().includes(query))
        .map((cmd) => ({ label: `/${cmd}`, type: "keyword" })); // Add / prefix to label

      console.log("slashCompletionSource returning", options.length, "options");
      return { from: word.from, options };
    };

    console.log("Creating autocompletion extension, fileList:", fileList.length, "slashCommands:", slashCommands.length);
    const ext = autocompletion({
      override: [fileCompletionSource, slashCompletionSource],
      defaultKeymap: true,
      activateOnTyping: true,
    });
    console.log("autocompletion() returned:", ext, "typeof:", typeof ext);
    return ext;
  }, [fileList, slashCommands]);

  // Debug: log the extensions array
  console.log("codemirrorExtensions will include customAutocompletion:", customAutocompletion);

  // Keymap for Enter to send (only when autocomplete is not open) and Tab to accept completion
  // Uses ref to avoid recreating keymap when handleSend changes
  // Using Prec.high to ensure our keymap runs before basicSetup's keymaps
  const sendKeymap = useMemo(() => {
    return Prec.high(keymap.of([
      {
        key: "Enter",
        run: (view) => {
          const status = completionStatus(view.state);
          console.log("Enter pressed, completionStatus:", status);
          // If autocomplete is open, let it handle Enter
          if (status !== null) {
            return false;
          }
          // Otherwise, trigger send via ref
          console.log("Calling handleSendRef.current()");
          handleSendRef.current();
          return true;
        },
      },
      {
        key: "Tab",
        // @ts-ignore odd mismatch between types
        run: acceptCompletion,
      },
    ]));
  }, []);

  console.log("sendKeymap created:", sendKeymap);

  // Build CodeMirror extensions with dynamic mention data
  const codemirrorExtensions = useMemo(() => {
    const exts = [
      markdown(),
      codemirrorTheme,
      EditorView.lineWrapping,
      customAutocompletion,
      sendKeymap,
      createTwkDecorationPlugin(),
    ];
    console.log("Building codemirrorExtensions, sendKeymap is:", sendKeymap, "full array:", exts);
    return exts;
  }, [customAutocompletion, sendKeymap]);

  // Callback for FileListIndicator to update our file list
  const handleFilesChange = useCallback((files: string[]) => {
    setFileList(files);
  }, []);

  // Drag/drop handlers for accepting things into the input
  const handleInputDragOver = useCallback((e: React.DragEvent) => {
    // Ignore if input is disabled
    if (!triggerable) return;
    // Only accept thing drags
    if (!e.dataTransfer.types.includes(mimeTypeForAnyThing)) return;

    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    setIsInputDragOver(true);
  }, [triggerable, mimeTypeForAnyThing]);

  const handleInputDragLeave = useCallback(() => {
    setIsInputDragOver(false);
  }, []);

  const handleInputDrop = useCallback(() => {
    // Just clear drag state - CodeMirror handles the actual insertion
    // (thingwatcher sets plain text to <tw-k>JSON</tw-k> syntax)
    setIsInputDragOver(false);
  }, []);

  // Track tw-k widget containers for portal rendering
  const [widgetContainers, setWidgetContainers] = useState<
    Array<[string, HTMLElement, unknown]>
  >([]);

  // Subscribe to widget registry changes
  useEffect(() => {
    const updateWidgets = () => {
      setWidgetContainers(
        widgetRegistry
          .getAll()
          .map(([id, { element, keyJSON }]) => [id, element, keyJSON])
      );
    };

    // Initial population
    updateWidgets();

    // Subscribe to changes
    return widgetRegistry.subscribe(updateWidgets);
  }, []);

  // Auto-scroll to bottom when new events arrive or streaming content changes
  useEffect(() => {
    if (conversationRef.current) {
      conversationRef.current.scrollTop = conversationRef.current.scrollHeight;
    }
  }, [allEvents.length, streamingState.blocks.length]);

  const handleSend = useCallback(async () => {
    if (!inputText.trim() || !triggerable) return;
    await trigger({ text: inputText });
    setInputText("");
  }, [inputText, triggerable, trigger]);

  // Keep ref updated for keymap to use
  handleSendRef.current = handleSend;

  return (
    <div ref={twindContainerRef} style={styles.container}>
      {/* Adopt twind stylesheet for Tailwind CSS support */}
      {twindState && <AdoptStyleSheet sheet={twindState.sheet} />}

      {/* CSS for Streamdown content */}
      <style>{`
        .streamdown-content pre {
          background: #111 !important;
          border: 1px solid #333;
          border-radius: 4px;
          padding: 8px !important;
          margin: 8px 0 !important;
          overflow-x: auto;
          white-space: pre !important;
        }
        .streamdown-content code {
          font-family: monospace !important;
          font-size: 12px !important;
          white-space: pre !important;
        }
        .streamdown-content p {
          margin: 0 0 8px 0;
        }
        .streamdown-content p:last-child {
          margin-bottom: 0;
        }
        .streamdown-content ul, .streamdown-content ol {
          margin: 8px 0;
          padding-left: 20px;
        }
        .streamdown-content h1, .streamdown-content h2, .streamdown-content h3 {
          margin: 12px 0 8px 0;
          color: #e0e0e0;
        }
        .streamdown-content a {
          color: #8be9fd;
        }
        .streamdown-content blockquote {
          border-left: 3px solid #444;
          padding-left: 12px;
          margin: 8px 0;
          color: #888;
        }
      `}</style>

      {/* Status bar */}
      <div
        style={{
          borderBottom: "1px solid #333",
          padding: "4px 8px",
          fontSize: "11px",
          color: "#666",
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>
          State: <span style={{ color: "#888" }}>{state}</span>
        </span>
        {sessionId && (
          <span
            style={transcriptPath ? styles.clickableSessionId : undefined}
            onClick={
              transcriptPath
                ? () => {
                    navigator.clipboard.writeText(transcriptPath);
                  }
                : undefined
            }
            title={transcriptPath ? `Click to copy: ${transcriptPath}` : undefined}
          >
            Session: <span style={{ color: "#888" }}>{sessionId}</span>
          </span>
        )}
      </div>

      {/* Conversation area */}
      <div ref={conversationRef} style={styles.conversationArea}>
        <EventsContext.Provider value={allEvents}>
          {isResumedWithoutHistory && (
            <div style={styles.systemMessage}>
              (Resumed session {sessionId} - history will load after first
              message (you can send "!noop" to load it now))
            </div>
          )}
          {displayEvents.map((event, i) => (
            <EventRenderer key={i} event={event} />
          ))}
          {/* Show streaming content when there are active stream events */}
          {streamingState.blocks.length > 0 && !streamingState.isComplete && (
            <StreamingContentRenderer streamingState={streamingState} />
          )}
          {state === "streaming" && streamingState.blocks.length === 0 && (
            <div style={styles.systemMessage}>Claude is responding...</div>
          )}
        </EventsContext.Provider>
      </div>

      {/* Permission dialog (above input, shown when pending) */}
      <ChildNodeView keyJSON={["clydra_permission_dialog"]} />

      {/* Input area */}
      <div style={styles.inputArea}>
        <ThingDropRegion style={{ flex: 1 }}>
          <div
            style={{
              border: isInputDragOver ? "2px solid #22c55e" : "1px solid #333",
              borderRadius: "4px",
              overflow: "hidden",
              opacity: triggerable ? 1 : 0.5,
              background: isInputDragOver ? "rgba(34, 197, 94, 0.1)" : undefined,
              transition: "border-color 0.15s, background-color 0.15s",
            }}
            onDragOver={handleInputDragOver}
            onDragLeave={handleInputDragLeave}
            onDrop={handleInputDrop}
          >
            <CodeMirror
              ref={editorRef}
              value={inputText}
              onChange={setInputText}
              theme="dark"
              extensions={codemirrorExtensions}
              placeholder={
                triggerable
                  ? "Type a message... (@file, /command)"
                  : "Waiting..."
              }
              editable={triggerable}
              basicSetup={{
                lineNumbers: false,
                foldGutter: false,
                highlightActiveLine: false,
                highlightActiveLineGutter: false,
              }}
              style={{
                minHeight: "40px",
                maxHeight: "200px",
                overflow: "auto",
              }}
            />
          </div>
        </ThingDropRegion>
        <button
          type="button"
          onClick={handleSend}
          disabled={!triggerable || !inputText.trim()}
          style={{
            ...styles.sendButton,
            opacity: triggerable && inputText.trim() ? 1 : 0.5,
            cursor: triggerable && inputText.trim() ? "pointer" : "default",
          }}
        >
          Send
        </button>
      </div>

      {/* File list indicator (hidden, just for tracking files) */}
      <ChildNodeView
        keyJSON={["clydra_file_list"]}
        args={{ onFilesChange: handleFilesChange }}
      />

      {/* Controls bar: permission mode + interrupt button side by side */}
      <div
        style={{
          display: "flex",
          gap: "8px",
          padding: "8px",
          borderTop: "1px solid #333",
          alignItems: "center",
        }}
      >
        <ChildNodeView keyJSON={["clydra_permission_mode"]} />
        <ChildNodeView keyJSON={["clydra_model"]} />
        <div style={{ flex: 1 }}>
          <ChildNodeView keyJSON={["clydra_interrupt"]} />
        </div>
      </div>

      {/* Portals for tw-k widgets - renders InlineNodeRef with context preserved */}
      <>
        {widgetContainers.map(([id, element, keyJSON]) =>
          createPortal(<InlineNodeRef keyJSON={keyJSON} />, element, id)
        )}
      </>
    </div>
  );
}

// =============================================================================
// Ref renderers
// =============================================================================

export function RefRenderer({ handle }: RefRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const state = lastChunk?.state || "setup";
  const eventCount = dataChunks.flatMap(
    (chunk) => chunk?.events || []
  ).length;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "4px 8px",
        background: "#1a2030",
        borderRadius: 4,
        fontFamily: "monospace",
        fontSize: "12px",
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          background: state === "streaming" ? "#4ecdc4" : "#4a90d9",
          borderRadius: 2,
        }}
      />
      <span style={{ color: "#4a90d9" }}>Clydra</span>
      <span style={{ color: "#666" }}>
        {state} | {eventCount} events
      </span>
    </div>
  );
}

export function InlineRefRenderer({ handle }: RefRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const state = lastChunk?.state || "setup";

  return (
    <span style={{ color: "#4a90d9", fontFamily: "monospace" }}>
      <span
        style={{
          display: "inline-block",
          width: 6,
          height: 6,
          background: state === "streaming" ? "#4ecdc4" : "#4a90d9",
          borderRadius: 2,
          marginRight: 4,
        }}
      />
      Clydra ({state})
    </span>
  );
}

export function PreviewRenderer({ handle }: PreviewRendererProps) {
  const dataChunks = useDataChunks<ChunkData>(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const lastChunk = dataChunks[dataChunks.length - 1] as ChunkData | undefined;
  const state = lastChunk?.state || "setup";
  const sessionId = lastChunk?.session_id;
  const eventCount = dataChunks.flatMap(
    (chunk) => chunk?.events || []
  ).length;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        fontFamily: "monospace",
        fontSize: "12px",
      }}
    >
      <DraggableIDMarker>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            cursor: "grab",
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              background: "#4a90d9",
              borderRadius: 2,
            }}
          />
          <span style={{ color: "#4a90d9", fontWeight: "bold" }}>
            Clydra Session
          </span>
          <span style={{ color: "#888", fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: "#aaa" }}>State: {state}</div>
      <div style={{ color: "#888" }}>{eventCount} events</div>
      {sessionId && (
        <div style={{ color: "#666", fontSize: 11 }}>ID: {sessionId}</div>
      )}
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
