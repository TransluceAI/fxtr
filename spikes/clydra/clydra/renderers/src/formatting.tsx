// formatting.tsx - Shared syntax highlighting and formatting utilities
// Used by ClydraRoot.tsx and PermissionDialog.tsx
import { useState } from "react";
// npm packages (bundled by esbuild)
import { diffLines } from "diff";
// react-shiki stays as esm.sh (external)
import { ShikiHighlighter } from "https://esm.sh/gh/AVGVSTVS96/react-shiki@react-shiki@0.9.1/package?external=react,react-dom";
import type { ShikiTransformer } from "https://esm.sh/shiki@1.24.0";
import diffStyles from "./diff.module.css";

// =============================================================================
// Utility functions
// =============================================================================

/**
 * Map file extension to language identifier for syntax highlighting.
 */
export function getLanguageFromPath(filePath: string): string {
  const ext = filePath.split(".").pop()?.toLowerCase() || "";
  const langMap: Record<string, string> = {
    // JavaScript/TypeScript
    js: "javascript",
    jsx: "jsx",
    ts: "typescript",
    tsx: "tsx",
    mjs: "javascript",
    cjs: "javascript",
    // Python
    py: "python",
    pyi: "python",
    pyw: "python",
    // Rust
    rs: "rust",
    // Go
    go: "go",
    // C/C++
    c: "c",
    h: "c",
    cpp: "cpp",
    cc: "cpp",
    cxx: "cpp",
    hpp: "cpp",
    hxx: "cpp",
    // Java/Kotlin
    java: "java",
    kt: "kotlin",
    kts: "kotlin",
    // Web
    html: "html",
    htm: "html",
    css: "css",
    scss: "scss",
    sass: "sass",
    less: "less",
    // Data formats
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    toml: "toml",
    xml: "xml",
    // Shell
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    fish: "fish",
    // Config
    md: "markdown",
    mdx: "mdx",
    sql: "sql",
    graphql: "graphql",
    gql: "graphql",
    // Other
    rb: "ruby",
    php: "php",
    swift: "swift",
    r: "r",
    lua: "lua",
    vim: "vim",
    dockerfile: "dockerfile",
    makefile: "makefile",
    cmake: "cmake",
  };
  return langMap[ext] || "text";
}

// =============================================================================
// Diff utilities
// =============================================================================

interface DiffLine {
  type: "add" | "remove" | "unchanged";
  content: string;
}

/**
 * Compute line-level diff between old and new strings.
 */
export function computeDiff(oldStr: string, newStr: string): DiffLine[] {
  const changes = diffLines(oldStr, newStr);
  const lines: DiffLine[] = [];

  for (const change of changes) {
    // Split the value into lines, handling trailing newlines
    const changeLines = change.value.replace(/\n$/, "").split("\n");
    const type: DiffLine["type"] = change.added
      ? "add"
      : change.removed
      ? "remove"
      : "unchanged";

    for (const line of changeLines) {
      lines.push({ type, content: line });
    }
  }

  return lines;
}

/**
 * Format diff lines for unified view (all lines with +/-/space prefix).
 */
export function formatUnifiedDiff(lines: DiffLine[]): string {
  return lines
    .map((line) => {
      const prefix = line.type === "add" ? "+" : line.type === "remove" ? "-" : " ";
      return prefix + line.content;
    })
    .join("\n");
}

/**
 * Format diff lines for split left view (remove + unchanged only).
 */
export function formatSplitLeft(lines: DiffLine[]): string {
  return lines
    .filter((line) => line.type !== "add")
    .map((line) => {
      const prefix = line.type === "remove" ? "-" : " ";
      return prefix + line.content;
    })
    .join("\n");
}

/**
 * Format diff lines for split right view (add + unchanged only).
 */
export function formatSplitRight(lines: DiffLine[]): string {
  return lines
    .filter((line) => line.type !== "remove")
    .map((line) => {
      const prefix = line.type === "add" ? "+" : " ";
      return prefix + line.content;
    })
    .join("\n");
}

/**
 * Shiki transformer for diff notation.
 * Strips +/-/space prefix from lines and adds CSS classes for styling.
 * Based on https://usagi.io/articles/2024-04-24-adding-diff-highlighting-to-markdown-using-shiki/
 */
export function transformerDiffNotation(): ShikiTransformer {
  return {
    name: "diff-notation",
    code(node) {
      // Find all line elements
      const lines = node.children.filter(
        (child): child is typeof child & { type: "element" } => child.type === "element"
      );

      for (const line of lines) {
        // Find the first text node in the line
        for (const child of line.children) {
          if (child.type !== "element") continue;
          const text = child.children[0];
          if (!text || text.type !== "text") continue;

          const value = text.value;
          if (value.startsWith("+")) {
            text.value = value.slice(1);
            this.addClassToHast(line, diffStyles.add);
            break;
          } else if (value.startsWith("-")) {
            text.value = value.slice(1);
            this.addClassToHast(line, diffStyles.remove);
            break;
          } else if (value.startsWith(" ")) {
            text.value = value.slice(1);
            break;
          }
        }
      }
    },
  };
}

// =============================================================================
// Shared styles
// =============================================================================

export const formattingStyles = {
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
};

// =============================================================================
// Components
// =============================================================================

/**
 * Toggle buttons to switch between rich/raw view modes.
 * Shows both buttons with active one highlighted.
 */
export function ViewModePicker({
  mode,
  onChange,
}: {
  mode: "rich" | "raw";
  onChange: (mode: "rich" | "raw") => void;
}) {
  const buttonStyle = (isActive: boolean): React.CSSProperties => ({
    background: isActive ? "#333" : "transparent",
    border: "1px solid #444",
    borderRadius: "3px",
    padding: "2px 6px",
    fontSize: "10px",
    color: isActive ? "#8be9fd" : "#666",
    cursor: "pointer",
  });

  return (
    <div style={{ display: "flex", gap: "4px", marginLeft: "auto" }}>
      <button
        type="button"
        onClick={() => onChange("rich")}
        style={buttonStyle(mode === "rich")}
        title="Show rich view"
      >
        rich
      </button>
      <button
        type="button"
        onClick={() => onChange("raw")}
        style={buttonStyle(mode === "raw")}
        title="Show raw view"
      >
        raw
      </button>
    </div>
  );
}

/**
 * Syntax highlighted code block using react-shiki.
 */
export function SyntaxHighlightedCode({
  code,
  language,
  showLineNumbers = true,
  startingLineNumber = 1,
}: {
  code: string;
  language: string;
  showLineNumbers?: boolean;
  startingLineNumber?: number;
}) {
  // Cast to any to handle React version mismatch
  const Highlighter = ShikiHighlighter as React.ComponentType<{
    children: string;
    language: string;
    theme: string;
    showLineNumbers?: boolean;
    startingLineNumber?: number;
    style?: React.CSSProperties;
  }>;
  return (
    <div style={{ ...formattingStyles.codeBlock, padding: 0, overflow: "auto" }}>
      <Highlighter
        language={language}
        theme="github-dark"
        showLineNumbers={showLineNumbers}
        startingLineNumber={startingLineNumber}
        style={{ margin: 0, padding: "8px", fontSize: "12px" }}
      >
        {code}
      </Highlighter>
    </div>
  );
}

/**
 * Syntax highlighted code block with diff notation support.
 * Strips +/-/space prefixes and colorizes added/removed lines.
 */
export function DiffHighlightedCode({
  code,
  language,
}: {
  code: string;
  language: string;
}) {
  // Cast to any to handle React version mismatch
  const Highlighter = ShikiHighlighter as React.ComponentType<{
    children: string;
    language: string;
    theme: string;
    showLineNumbers?: boolean;
    transformers?: ShikiTransformer[];
    style?: React.CSSProperties;
  }>;
  return (
    <div style={{ ...formattingStyles.codeBlock, padding: 0, overflow: "auto" }}>
      <Highlighter
        language={language}
        theme="github-dark"
        showLineNumbers={false}
        transformers={[transformerDiffNotation()]}
        style={{ margin: 0, padding: "8px", fontSize: "12px" }}
      >
        {code}
      </Highlighter>
    </div>
  );
}

/**
 * View for Write tool calls - shows syntax highlighted file content.
 */
export function WriteContentView({
  input,
  rawView,
}: {
  input: Record<string, unknown>;
  rawView: React.ReactNode;
}) {
  const [viewMode, setViewMode] = useState<"rich" | "raw">("rich");
  const filePath = (input.file_path as string) || "";
  const content = (input.content as string) || "";
  const language = getLanguageFromPath(filePath);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: "4px" }}>
        <span style={{ color: "#666", fontSize: "11px", flex: 1 }}>{filePath}</span>
        <ViewModePicker mode={viewMode} onChange={setViewMode} />
      </div>
      {viewMode === "rich" ? (
        <SyntaxHighlightedCode code={content} language={language} showLineNumbers={true} />
      ) : (
        rawView
      )}
    </div>
  );
}

type DiffViewMode = "unified" | "split" | "raw";

/**
 * Toggle buttons for diff view modes (unified/split/raw).
 */
function DiffViewModePicker({
  mode,
  onChange,
}: {
  mode: DiffViewMode;
  onChange: (mode: DiffViewMode) => void;
}) {
  const buttonStyle = (isActive: boolean): React.CSSProperties => ({
    background: isActive ? "#333" : "transparent",
    border: "1px solid #444",
    borderRadius: "3px",
    padding: "2px 6px",
    fontSize: "10px",
    color: isActive ? "#8be9fd" : "#666",
    cursor: "pointer",
  });

  return (
    <div style={{ display: "flex", gap: "4px", marginLeft: "auto" }}>
      <button
        type="button"
        onClick={() => onChange("unified")}
        style={buttonStyle(mode === "unified")}
        title="Show unified diff"
      >
        unified
      </button>
      <button
        type="button"
        onClick={() => onChange("split")}
        style={buttonStyle(mode === "split")}
        title="Show split diff"
      >
        split
      </button>
      <button
        type="button"
        onClick={() => onChange("raw")}
        style={buttonStyle(mode === "raw")}
        title="Show raw JSON"
      >
        raw
      </button>
    </div>
  );
}

/**
 * View for Edit tool calls - shows diff with unified/split/raw modes.
 */
export function EditDiffView({
  input,
  rawView,
}: {
  input: Record<string, unknown>;
  rawView: React.ReactNode;
}) {
  const [viewMode, setViewMode] = useState<DiffViewMode>("unified");
  const filePath = (input.file_path as string) || "";
  const oldString = (input.old_string as string) || "";
  const newString = (input.new_string as string) || "";
  const language = getLanguageFromPath(filePath);

  // Compute diff once
  const diffLines = computeDiff(oldString, newString);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", marginBottom: "4px" }}>
        <span style={{ color: "#666", fontSize: "11px", flex: 1 }}>{filePath}</span>
        <DiffViewModePicker mode={viewMode} onChange={setViewMode} />
      </div>
      {viewMode === "unified" ? (
        <DiffHighlightedCode code={formatUnifiedDiff(diffLines)} language={language} />
      ) : viewMode === "split" ? (
        <div style={{ display: "flex", gap: "8px" }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: "#ff6b6b", fontSize: "11px", marginBottom: "4px" }}>Old</div>
            <DiffHighlightedCode code={formatSplitLeft(diffLines)} language={language} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ color: "#69db7c", fontSize: "11px", marginBottom: "4px" }}>New</div>
            <DiffHighlightedCode code={formatSplitRight(diffLines)} language={language} />
          </div>
        </div>
      ) : (
        rawView
      )}
    </div>
  );
}
