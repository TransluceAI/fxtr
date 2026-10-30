/**
 * Fallback renderer for fxtr entities.
 *
 * This renderer handles entities without custom renderers by:
 * - Rendering $!fxtr.link.* markers as clickable node references
 * - Showing the entity data in a readable JSON-like format
 * - Displaying type info in the ref view
 */

import type {
  ThingWatcherRenderConfig,
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  GraphNodeRendererProps,
} from 'thingwatcher-bindings';
import {
  InlineNodeRef,
  usePayload,
  useLinks,
  useThingMetadata,
  useDraggableIdMarker,
  Port,
  PortPosition,
  LinkEdgeConfig,
} from 'thingwatcher-bindings';

interface FxtrLinkMarker {
  "$!fxtr.link.weak"?: [string, string];
  "$!fxtr.link.strong"?: [string, string];
  "$!fxtr.link.marker"?: [string, string];
}

function isFxtrLinkMarker(value: unknown): value is FxtrLinkMarker {
  if (typeof value !== "object" || value === null) return false;
  const obj = value as Record<string, unknown>;
  return (
    "$!fxtr.link.weak" in obj ||
    "$!fxtr.link.strong" in obj ||
    "$!fxtr.link.marker" in obj
  );
}

function FxtrLinkRenderer({ marker }: { marker: FxtrLinkMarker }) {
  const key = marker["$!fxtr.link.weak"] ||
              marker["$!fxtr.link.strong"] ||
              marker["$!fxtr.link.marker"];
  if (!key) return <span>???</span>;

  const linkType = "$!fxtr.link.weak" in marker ? "weak" :
                   "$!fxtr.link.strong" in marker ? "strong" : "marker";
  const label = linkType === "marker" ? "ctx" : linkType;

  return (
    <span style={{ opacity: 0.7 }}>
      [{label}] <InlineNodeRef keyJSON={key} />
    </span>
  );
}

const INDENT_SIZE = 16; // pixels per indent level

function renderValue(value: unknown, depth: number = 0): JSX.Element {
  if (depth > 10) return <span>...</span>;

  if (isFxtrLinkMarker(value)) {
    return <FxtrLinkRenderer marker={value} />;
  }

  if (value === null) return <span style={{ color: "#888" }}>null</span>;
  if (typeof value === "boolean") return <span style={{ color: "#569cd6" }}>{String(value)}</span>;
  if (typeof value === "number") return <span style={{ color: "#b5cea8" }}>{value}</span>;
  if (typeof value === "string") return <span style={{ color: "#ce9178" }}>"{value}"</span>;

  if (Array.isArray(value)) {
    if (value.length === 0) return <span>[]</span>;

    // For small arrays of primitives, render inline
    if (value.length <= 3 && value.every(v => typeof v !== "object" || v === null)) {
      return (
        <span>
          [{value.map((item, i) => (
            <span key={i}>
              {i > 0 && ", "}
              {renderValue(item, depth + 1)}
            </span>
          ))}]
        </span>
      );
    }

    return (
      <details open>
        <summary style={{ cursor: "pointer", marginLeft: "-12px" }}>
          {"["}<span style={{ color: "#888", fontSize: "11px" }}>{value.length} items</span>
        </summary>
        <div style={{ paddingLeft: `${INDENT_SIZE}px` }}>
          {value.map((item, i) => (
            <div key={i} style={{ display: "flex", alignItems: "flex-start", gap: "4px" }}>
              <span style={{ color: "#888", minWidth: "20px" }}>{i}:</span>
              {renderValue(item, depth + 1)}
            </div>
          ))}
        </div>
        {"]"}
      </details>
    );
  }

  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return <span>{"{}"}</span>;

    // Check for special markers
    if ("$!bytes" in value) {
      const bytesValue = (value as { "$!bytes": string })["$!bytes"];
      return <span style={{ color: "#888" }}>[bytes: {bytesValue.slice(0, 20)}...]</span>;
    }

    // Check for unknown type marker
    if ("$!unknown" in value) {
      const unknownType = (value as { "$!unknown": string })["$!unknown"];
      return <span style={{ color: "#888" }}>[{unknownType}]</span>;
    }

    return (
      <details open>
        <summary style={{ cursor: "pointer", marginLeft: "-12px" }}>
          {"{"}<span style={{ color: "#888", fontSize: "11px" }}>{entries.length} fields</span>
        </summary>
        <div style={{ paddingLeft: `${INDENT_SIZE}px` }}>
          {entries.map(([k, v]) => (
            <div key={k} style={{ display: "flex", alignItems: "flex-start", gap: "4px" }}>
              <span style={{ color: "#9cdcfe" }}>{k}:</span>
              {renderValue(v, depth + 1)}
            </div>
          ))}
        </div>
        {"}"}
      </details>
    );
  }

  return <span>{String(value)}</span>;
}

function FxtrEntityFull({ handle }: FullRendererProps) {
  const data = usePayload(handle);
  return (
    <div style={{ fontFamily: "monospace", fontSize: "12px", whiteSpace: "pre-wrap" }}>
      {renderValue(data)}
    </div>
  );
}

/** Extract type string from entity data, or null if not available */
function getEntityTypeString(data: unknown): string | null {
  if (typeof data !== "object" || data === null) return null;
  if (!("$fxtr.type" in data)) return null;

  const typeMarker = (data as Record<string, unknown>)["$fxtr.type"];
  if (!isFxtrLinkMarker(typeMarker)) return null;

  const key = typeMarker["$!fxtr.link.weak"] ||
              typeMarker["$!fxtr.link.strong"] ||
              typeMarker["$!fxtr.link.marker"];
  return key?.[1] ?? null;
}

/** Extract the fxtr ID string from thingwatcher key */
function getIdStringFromKey(keyJSON: unknown): string | null {
  if (!Array.isArray(keyJSON) || keyJSON.length !== 2) return null;
  if (keyJSON[0] !== "fxtr_entity") return null;
  return typeof keyJSON[1] === "string" ? keyJSON[1] : null;
}

/** Check if an ID string is a hash (not a builtin starting with !) */
function isHashId(idStr: string): boolean {
  return !idStr.startsWith("!");
}

/** Truncate hash IDs to specified length, leave builtins unchanged */
function truncateHashId(idStr: string, maxLen: number): string {
  if (!isHashId(idStr)) return idStr; // Never truncate builtins
  if (idStr.length <= maxLen) return idStr;
  return idStr.slice(0, maxLen);
}

function FxtrEntityRef({ handle }: RefRendererProps) {
  const data = usePayload(handle);
  const { keyJSON } = useThingMetadata(handle);
  const typeStr = getEntityTypeString(data);
  const idStr = getIdStringFromKey(keyJSON);

  // Check for special node types (contentless nodes from immutable_store)
  if (typeof data === "object" && data !== null) {
    const nodeType = (data as Record<string, unknown>)["$fxtr.node_type"];
    if (nodeType === "ground") {
      return <span style={{ fontFamily: "monospace", color: "#888" }}>[ground]</span>;
    }
    if (nodeType === "builtin") {
      const name = (data as Record<string, unknown>)["$fxtr.builtin_name"];
      return <span style={{ fontFamily: "monospace", color: "#9cdcfe" }}>!{String(name)}</span>;
    }
  }

  // Main display is the entity's own ID
  const displayId = idStr || "?";

  return (
    <span style={{
      fontFamily: "monospace",
      display: "inline-flex",
      alignItems: "center",
      gap: "6px",
      maxWidth: "100%",
      overflow: "hidden",
    }}>
      <span style={{
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        color: "#4ecdc4",
        minWidth: 0,
        flex: "1 1 auto",
      }}>
        {displayId}
      </span>
      {typeStr && (
        <span style={{ color: "#888", fontSize: "11px", flexShrink: 0, whiteSpace: "nowrap" }}>
          [{typeStr}]
        </span>
      )}
    </span>
  );
}

function FxtrEntityInline({ handle }: RefRendererProps) {
  const data = usePayload(handle);
  const { keyJSON } = useThingMetadata(handle);
  const idStr = getIdStringFromKey(keyJSON);

  // Check for special node types
  if (typeof data === "object" && data !== null) {
    const nodeType = (data as Record<string, unknown>)["$fxtr.node_type"];
    if (nodeType === "ground") {
      return <span style={{ fontFamily: "monospace", color: "#888" }}>.</span>;
    }
    if (nodeType === "builtin") {
      const name = (data as Record<string, unknown>)["$fxtr.builtin_name"];
      return <span style={{ fontFamily: "monospace", color: "#9cdcfe" }}>!{String(name)}</span>;
    }
  }

  // Show entity's own ID, truncate hashes to 7 chars
  const displayId = idStr ? truncateHashId(idStr, 7) : "?";

  return (
    <span style={{ fontFamily: "monospace", color: "#4ecdc4" }}>
      {displayId}
    </span>
  );
}

function FxtrEntityPreview({ handle }: PreviewRendererProps) {
  const data = usePayload(handle);
  const { keyJSON } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const idStr = getIdStringFromKey(keyJSON);
  const typeStr = getEntityTypeString(data);

  // Check for special node types
  let displayId = idStr || "?";
  if (typeof data === "object" && data !== null) {
    const nodeType = (data as Record<string, unknown>)["$fxtr.node_type"];
    if (nodeType === "ground") {
      displayId = "ground (.)";
    } else if (nodeType === "builtin") {
      const name = (data as Record<string, unknown>)["$fxtr.builtin_name"];
      displayId = `!${String(name)}`;
    }
  }

  return (
    <div style={{ fontFamily: "monospace", fontSize: "12px", overflow: "hidden" }}>
      <DraggableIDMarker>
        <div style={{
          cursor: "grab",
          marginBottom: "6px",
          display: "flex",
          alignItems: "center",
          gap: "8px",
          overflow: "hidden",
        }}>
          <span style={{
            color: "#4ecdc4",
            fontWeight: "bold",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            minWidth: 0,
            flex: "1 1 auto",
          }}>
            {displayId}
          </span>
          {typeStr && (
            <span style={{ color: "#888", fontSize: "11px", flexShrink: 0, whiteSpace: "nowrap" }}>
              [{typeStr}]
            </span>
          )}
        </div>
      </DraggableIDMarker>

      <div style={{ marginTop: "8px", overflow: "auto" }}>
        {renderValue(data)}
      </div>
    </div>
  );
}

function FxtrEntityGraph({ handle }: GraphNodeRendererProps) {
  const data = usePayload(handle);
  const links = useLinks(handle);
  const { keyJSON } = useThingMetadata(handle);

  const idStr = getIdStringFromKey(keyJSON);
  const typeStr = getEntityTypeString(data);

  // Check for special node types
  let displayId = idStr || "?";
  if (typeof data === "object" && data !== null) {
    const nodeType = (data as Record<string, unknown>)["$fxtr.node_type"];
    if (nodeType === "ground") {
      displayId = ".";
    } else if (nodeType === "builtin") {
      const name = (data as Record<string, unknown>)["$fxtr.builtin_name"];
      displayId = `!${String(name)}`;
    } else if (isHashId(displayId)) {
      // Truncate hash IDs for graph display
      displayId = truncateHashId(displayId, 7);
    }
  } else if (idStr && isHashId(idStr)) {
    displayId = truncateHashId(idStr, 7);
  }

  // Truncate type ID if it's a hash
  const displayType = typeStr ? truncateHashId(typeStr, 7) : null;

  // Get outgoing links (excluding context markers)
  const outgoingLinks = links.filter(l => l.linkType !== "marker");

  return (
    <div style={{
      padding: "8px 12px",
      background: "#1a2a2a",
      borderRadius: "6px",
      border: "1px solid #4ecdc4",
      fontFamily: "monospace",
      fontSize: "11px",
      textAlign: "center",
      minWidth: "60px",
    }}>
      <Port type="target" position={PortPosition.Top} id="in" />

      <div style={{ color: "#4ecdc4", fontWeight: "bold", marginBottom: displayType ? "2px" : 0 }}>
        {displayId}
      </div>
      {displayType && (
        <div style={{ color: "#888", fontStyle: "italic", fontSize: "10px" }}>
          {displayType}
        </div>
      )}

      <Port type="source" position={PortPosition.Bottom} id="out" />

      {/* Configure outgoing edges */}
      {outgoingLinks.map(link => (
        <LinkEdgeConfig
          key={link.linkKey}
          linkKey={link.linkKey}
          sourcePort="out"
          targetPort="in"
          label={JSON.stringify(link.linkKeyJSON)}
          style={{ stroke: link.linkType === "strong" ? "#4ecdc4" : "#888" }}
        />
      ))}
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "fxtr.transluce.org/fxtr_entity_fallback": {
      full: FxtrEntityFull,
      ref: FxtrEntityRef,
      inline: FxtrEntityInline,
      preview: FxtrEntityPreview,
      graph: FxtrEntityGraph,
    },
  },
} as const satisfies ThingWatcherRenderConfig;
