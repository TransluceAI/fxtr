/**
 * TreePointer renderer - displays pointers that reference tree nodes.
 */

import {
  ChildNodeView,
  usePayload,
  useLinks,
  useDraggableIdMarker,
  Port,
  PortPosition,
  LinkEdgeConfig,
} from 'thingwatcher-bindings';
import type {
  FullRendererProps,
  RefRendererProps,
  GraphNodeRendererProps,
  ThingWatcherRenderConfig,
} from 'thingwatcher-bindings';

interface TreePointerData {
  name: string;
  target_path: string;
  description: string;
}

function TreePointerFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle) as TreePointerData | null;
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const targetLink = links.find(l => l.linkType === 'target');

  return (
    <div style={{ padding: '12px' }}>
      <DraggableIDMarker>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: '12px',
          marginBottom: '12px',
          cursor: 'grab',
        }}>
          <div style={{
            width: '48px',
            height: '48px',
            background: '#22c55e',
            borderRadius: '8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '20px',
          }}>
            <span style={{ transform: 'rotate(-45deg)' }}>&#x279C;</span>
          </div>
          <div>
            <div style={{ fontWeight: 'bold', fontSize: '16px' }}>
              {payload?.name}
            </div>
            <div style={{ fontSize: '12px', color: '#888' }}>
              Points to tree node
            </div>
          </div>
        </div>
      </DraggableIDMarker>

      <div style={{
        background: 'rgba(34, 197, 94, 0.15)',
        borderRadius: '8px',
        padding: '12px',
        marginTop: '8px',
      }}>
        <div style={{ fontSize: '12px', color: '#888', marginBottom: '6px' }}>
          Target:
        </div>
        {targetLink ? (
          <ChildNodeView thingKey={targetLink.toThingKey} />
        ) : (
          <span style={{ color: '#888' }}>No target</span>
        )}
      </div>
    </div>
  );
}

function TreePointerRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle) as TreePointerData | null;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: 'rgba(34, 197, 94, 0.3)',
      borderRadius: 4,
      whiteSpace: 'nowrap',
    }}>
      <span style={{ color: '#22c55e' }}>&#x279C;</span>
      <span style={{ color: '#86efac' }}>{payload?.name}</span>
      <span style={{ color: '#888', fontSize: 11 }}>
        &rarr; {payload?.target_path}
      </span>
    </div>
  );
}

function TreePointerGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle) as TreePointerData | null;
  const links = useLinks(handle);

  const targetLink = links.find(l => l.linkType === 'target');

  return (
    <div style={{
      padding: 8,
      background: '#22c55e',
      borderRadius: 6,
      minWidth: 60,
      textAlign: 'center',
    }}>
      <div style={{
        color: '#fff',
        fontWeight: 'bold',
        fontSize: 11,
        marginBottom: 2,
      }}>
        {payload?.name}
      </div>
      <div style={{ fontSize: 16 }}>&#x279C;</div>

      {targetLink && (
        <>
          <Port type="source" position={PortPosition.Bottom} id="target" />
          <LinkEdgeConfig linkKey={targetLink.linkKey} sourcePort="target" targetPort="in" />
        </>
      )}
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "TreePointer": {
      full: TreePointerFull,
      ref: TreePointerRef,
      graph: TreePointerGraphNode,
    },
  },
} as ThingWatcherRenderConfig;
