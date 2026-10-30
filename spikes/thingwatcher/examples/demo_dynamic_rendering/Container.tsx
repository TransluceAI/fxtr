/**
 * Container renderer module.
 */

// React is auto-imported by JSX transform
import {
  ChildNodeView, NodeRef, InlineNodeRef,
  usePayload, useLinks, useDraggableIdMarker, useThingMetadata,
  Port, PortPosition, LinkEdgeConfig
} from 'thingwatcher-bindings';
import type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  GraphNodeRendererProps
} from 'thingwatcher-bindings';

function ContainerFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const itemLinks = links.filter(l => l.linkType === 'item');
  const data = payload as { name?: string; item_count?: number } | null;

  return (
    <div style={{ padding: '12px' }}>
      <div style={{
        fontSize: '18px',
        fontWeight: 'bold',
        marginBottom: '12px',
        color: '#4ecdc4'
      }}>
        <DraggableIDMarker>
          <span style={{ cursor: 'grab' }}>Container: {data?.name}</span>
        </DraggableIDMarker>
      </div>

      <div style={{
        fontSize: '14px',
        color: '#888',
        marginBottom: '16px'
      }}>
        Contains {data?.item_count} item(s)
      </div>

      {/* Demo: InlineNodeRef in flowing text */}
      {itemLinks.length > 0 && (
        <p style={{ color: '#aaa', margin: '0 0 16px 0', fontSize: '13px' }}>
          First item is <InlineNodeRef thingKey={itemLinks[0].toThingKey} /> (inline ref demo)
        </p>
      )}

      {/* Test NodeRef (block) - right-click should show context menu */}
      <div style={{ marginBottom: '16px' }}>
        <div style={{ fontSize: '12px', color: '#666', marginBottom: '8px' }}>
          Block refs (NodeRef - fill width, right-click for menu):
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
          {itemLinks.map((link, i) => (
            <NodeRef
              key={i}
              thingKey={link.toThingKey}
            />
          ))}
        </div>
      </div>

      {/* Test ChildNodeView - recursive rendering */}
      <div>
        <div style={{
          fontSize: '12px',
          color: '#666',
          marginBottom: '8px',
          borderTop: '1px solid #333',
          paddingTop: '12px'
        }}>
          Inline rendered items (ChildNodeView):
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {itemLinks.map((link, i) => (
            <div key={i} style={{
              background: '#1a1a2e',
              borderRadius: '6px',
              overflow: 'hidden'
            }}>
              <ChildNodeView thingKey={link.toThingKey} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ContainerRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string; item_count?: number } | null;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#1a3a3a',
      borderRadius: 4,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: '#4ecdc4',
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: '#4ecdc4', flexShrink: 0 }}>Container: {data?.name}</span>
      <span style={{
        color: '#888',
        flex: 1,
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        ({data?.item_count} items)
      </span>
    </div>
  );
}

function ContainerInline({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;

  return (
    <span style={{ color: '#4ecdc4' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#4ecdc4',
        borderRadius: 2,
        marginRight: 4,
      }} />
      {data?.name}
    </span>
  );
}

function ContainerPreview({ handle }: PreviewRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const data = payload as { name?: string; item_count?: number } | null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{ width: 8, height: 8, background: '#4ecdc4', borderRadius: 2 }} />
          <span style={{ color: '#4ecdc4', fontWeight: 'bold' }}>Container: {data?.name}</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: '#aaa', fontSize: 12 }}>
        {data?.item_count} item(s)
      </div>
      {links.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 4 }}>
          {links.map((link, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
              <span style={{ color: '#888' }}>{link.linkType}:</span>
              <InlineNodeRef thingKey={link.toThingKey} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Graph node renderer for Container - custom node for React Flow visualization.
 */
function ContainerGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);

  const data = payload as { name?: string; item_count?: number } | null;
  const itemLinks = links.filter(l => l.linkType === 'item');

  return (
    <div style={{
      padding: 8,
      background: '#1a3a3a',
      borderRadius: 6,
      minWidth: 60,
      textAlign: 'center',
    }}>
      <Port type="target" position={PortPosition.Left} id="in" />

      <div style={{ color: '#4ecdc4', fontWeight: 'bold', fontSize: 11 }}>
        #{shortId}
      </div>
      <div style={{ color: '#4ecdc4', fontSize: 10 }}>
        {data?.name}
      </div>
      <div style={{ color: '#888', fontSize: 9 }}>
        {data?.item_count} items
      </div>

      <Port type="source" position={PortPosition.Bottom} id="item" />

      {/* Configure outgoing edges to item links */}
      {itemLinks.map(link => (
        <LinkEdgeConfig
          key={link.linkKey}
          linkKey={link.linkKey}
          sourcePort="item"
          targetPort="in"
          label="item"
          style={{ stroke: '#4ecdc4' }}
        />
      ))}
    </div>
  );
}

export {
  ContainerFull,
  ContainerRef,
  ContainerInline,
  ContainerPreview,
  ContainerGraphNode,
};
