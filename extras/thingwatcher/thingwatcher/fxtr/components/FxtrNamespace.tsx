/**
 * Renderer for fxtr namespace objects.
 *
 * Shows the namespace name, pin count, and pinned entities as clickable refs.
 */

import type {
  ThingWatcherRenderConfig,
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  GraphNodeRendererProps,
} from 'thingwatcher-bindings';
import {
  usePayload,
  useLinks,
  NodeRef,
  InlineNodeRef,
  useDraggableIdMarker,
  Port,
  PortPosition,
  LinkEdgeConfig,
} from 'thingwatcher-bindings';

interface NamespaceData {
  name: string;
  pin_count: number;
  stale: boolean;
}

function NamespaceFull({ handle }: FullRendererProps) {
  const data = usePayload(handle) as NamespaceData | null;
  const links = useLinks(handle);

  if (!data) return <div>Loading...</div>;

  // Filter for pin links and extract pin names from linkKeyJSON
  const pinLinks = links.filter(l => l.linkType === "pin");

  return (
    <div style={{ fontFamily: "monospace", fontSize: "13px" }}>
      <div style={{ marginBottom: "12px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", color: "#4ecdc4" }}>
          {data.name}
        </div>
        <div style={{ color: "#888", fontSize: "12px" }}>
          {data.pin_count} pin{data.pin_count !== 1 ? "s" : ""}
          {data.stale && <span style={{ color: "#f0ad4e", marginLeft: "8px" }}>(loading...)</span>}
        </div>
      </div>

      {pinLinks.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
          {pinLinks.map((link) => {
            // linkKeyJSON is ["pins", pinName]
            const linkKeyArr = link.linkKeyJSON as [string, string] | null;
            const pinName = linkKeyArr ? linkKeyArr[1] : link.linkKey;

            return (
              <div key={link.linkKey} style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <span style={{ color: "#888", minWidth: "120px", overflow: "hidden", textOverflow: "ellipsis" }}>
                  {pinName}:
                </span>
                <div style={{ flex: 1 }}>
                  <NodeRef thingKey={link.toThingKey} />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {pinLinks.length === 0 && (
        <div style={{ color: "#666", fontStyle: "italic" }}>No pins</div>
      )}
    </div>
  );
}

function NamespaceRef({ handle }: RefRendererProps) {
  const data = usePayload(handle) as NamespaceData | null;
  if (!data) return <span style={{ fontFamily: "monospace" }}>[ns:?]</span>;
  return <span style={{ fontFamily: "monospace" }}>[ns:{data.name}]</span>;
}

function NamespaceInline({ handle }: RefRendererProps) {
  const data = usePayload(handle) as NamespaceData | null;
  if (!data) return <span style={{ color: "#4ecdc4" }}>ns:?</span>;
  return <span style={{ color: "#4ecdc4" }}>{data.name}</span>;
}

function NamespacePreview({ handle }: PreviewRendererProps) {
  const data = usePayload(handle) as NamespaceData | null;
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  if (!data) return <div>Loading...</div>;

  const pinLinks = links.filter(l => l.linkType === "pin");

  return (
    <div style={{ fontFamily: "monospace", fontSize: "12px" }}>
      <DraggableIDMarker>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", cursor: "grab", marginBottom: "6px" }}>
          <span style={{
            width: "8px",
            height: "8px",
            background: "#4ecdc4",
            borderRadius: "2px",
          }} />
          <span style={{ color: "#4ecdc4", fontWeight: "bold" }}>{data.name}</span>
        </div>
      </DraggableIDMarker>

      <div style={{ color: "#888", fontSize: "11px", marginBottom: "6px" }}>
        {data.pin_count} pin{data.pin_count !== 1 ? "s" : ""}
        {data.stale && <span style={{ color: "#f0ad4e", marginLeft: "4px" }}>(loading...)</span>}
      </div>

      {pinLinks.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
          {pinLinks.slice(0, 5).map((link) => {
            const linkKeyArr = link.linkKeyJSON as [string, string] | null;
            const pinName = linkKeyArr ? linkKeyArr[1] : link.linkKey;

            return (
              <div key={link.linkKey} style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "11px" }}>
                <span style={{ color: "#888" }}>{pinName}:</span>
                <InlineNodeRef thingKey={link.toThingKey} />
              </div>
            );
          })}
          {pinLinks.length > 5 && (
            <div style={{ color: "#666", fontSize: "10px" }}>
              +{pinLinks.length - 5} more pins
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NamespaceGraph({ handle }: GraphNodeRendererProps) {
  const data = usePayload(handle) as NamespaceData | null;
  const links = useLinks(handle);

  const pinLinks = links.filter(l => l.linkType === "pin");

  return (
    <div style={{
      padding: "8px 12px",
      background: "#1a2a3a",
      borderRadius: "6px",
      border: "1px solid #4ecdc4",
      fontFamily: "monospace",
      fontSize: "11px",
      textAlign: "center",
      minWidth: "80px",
    }}>
      <div style={{ color: "#888", fontSize: "10px" }}>
        Namespace:
      </div>
      <div style={{ color: "#4ecdc4", fontWeight: "bold" }}>
        {data?.name || "?"}
      </div>

      <Port type="source" position={PortPosition.Bottom} id="pin" />

      {/* Configure outgoing edges to pinned entities */}
      {pinLinks.map(link => (
        <LinkEdgeConfig
          key={link.linkKey}
          linkKey={link.linkKey}
          sourcePort="pin"
          targetPort="in"
          label={(link.linkKeyJSON as [string, string])[1]}
          style={{ stroke: "#4ecdc4" }}
        />
      ))}
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "fxtr.transluce.org/fxtr_namespace": {
      full: NamespaceFull,
      ref: NamespaceRef,
      inline: NamespaceInline,
      preview: NamespacePreview,
      graph: NamespaceGraph,
    },
  },
} as const satisfies ThingWatcherRenderConfig;
