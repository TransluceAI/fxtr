/**
 * Item renderer module.
 */

// React is auto-imported by JSX transform
import {
  InlineNodeRef,
  usePayload, useLinks, useDraggableIdMarker, useThingMetadata,
  Port, PortPosition, LinkEdgeConfig
} from 'thingwatcher-bindings';
import type {
  FullRendererProps,
  RefRendererProps,
  PreviewRendererProps,
  GraphNodeRendererProps
} from 'thingwatcher-bindings';

function ItemFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const containerLink = links.find(l => l.linkType === 'container');
  const data = payload as { name?: string; value?: number } | null;
  const hue = (data?.value || 0) * 30;

  return (
    <div style={{ padding: '10px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <DraggableIDMarker>
          <div style={{
            width: '40px',
            height: '40px',
            background: `hsl(${hue}, 70%, 50%)`,
            borderRadius: '8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '16px',
            fontWeight: 'bold',
            cursor: 'grab'
          }}>
            {data?.value}
          </div>
        </DraggableIDMarker>
        <div>
          <div style={{ fontWeight: 'bold' }}>{data?.name}</div>
          {containerLink && (
            <div style={{ fontSize: '12px', color: '#888' }}>
              In: <InlineNodeRef thingKey={containerLink.toThingKey} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ItemRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string; value?: number } | null;
  const hue = (data?.value || 0) * 30;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: `hsla(${hue}, 70%, 30%, 0.5)`,
      borderRadius: 4,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: `hsl(${hue}, 70%, 50%)`,
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: `hsl(${hue}, 70%, 70%)`, flexShrink: 0 }}>{data?.name}</span>
      <span style={{
        color: '#888',
        flex: 1,
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>value = {data?.value}</span>
    </div>
  );
}

function ItemInline({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string; value?: number } | null;
  const hue = (data?.value || 0) * 30;

  return (
    <span style={{ color: `hsl(${hue}, 70%, 70%)` }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: `hsl(${hue}, 70%, 50%)`,
        borderRadius: 2,
        marginRight: 4,
      }} />
      {data?.name}
    </span>
  );
}

function ItemPreview({ handle }: PreviewRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const data = payload as { name?: string; value?: number } | null;
  const hue = (data?.value || 0) * 30;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{
            width: 24,
            height: 24,
            background: `hsl(${hue}, 70%, 50%)`,
            borderRadius: 4,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 12,
            fontWeight: 'bold',
          }}>
            {data?.value}
          </span>
          <span style={{ color: `hsl(${hue}, 70%, 70%)`, fontWeight: 'bold' }}>{data?.name}</span>
          <span style={{ color: '#888', fontSize: 11 }}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
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
 * Graph node renderer for Item - custom node for React Flow visualization.
 */
function ItemGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);

  const data = payload as { name?: string; value?: number } | null;
  const hue = (data?.value || 0) * 30;
  const containerLink = links.find(l => l.linkType === 'container');

  return (
    <div style={{
      padding: 8,
      background: `hsl(${hue}, 70%, 25%)`,
      borderRadius: 6,
      minWidth: 50,
      textAlign: 'center',
    }}>
      <Port type="target" position={PortPosition.Top} id="in" />

      <div style={{
        width: 24,
        height: 24,
        background: `hsl(${hue}, 70%, 50%)`,
        borderRadius: 4,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        margin: '0 auto 4px auto',
        fontSize: 12,
        fontWeight: 'bold',
        color: '#fff',
      }}>
        {data?.value}
      </div>
      <div style={{ color: `hsl(${hue}, 70%, 70%)`, fontSize: 10 }}>
        #{shortId}
      </div>

      <Port type="source" position={PortPosition.Right} id="container" />

      {/* Configure edge to container (if any) */}
      {containerLink && (
        <LinkEdgeConfig
          linkKey={containerLink.linkKey}
          sourcePort="container"
          targetPort="in"
        />
      )}
    </div>
  );
}

export {
  ItemFull,
  ItemRef,
  ItemInline,
  ItemPreview,
  ItemGraphNode,
};
