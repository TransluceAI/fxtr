/**
 * TreeNode renderer - displays binary tree nodes from immutable store.
 */

import {
  InlineNodeRef,
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

interface TreeNodeData {
  path: string;
  level: number;
  is_leaf: boolean;
  description: string;
}

function TreeNodeFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle) as TreeNodeData | null;
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const leftLink = links.find(l => l.linkType === 'left');
  const rightLink = links.find(l => l.linkType === 'right');
  const parentLink = links.find(l => l.linkType === 'parent');

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
            background: '#8b5cf6',
            borderRadius: '50%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '14px',
            fontWeight: 'bold',
            color: '#fff',
          }}>
            {payload?.path === 'root' ? 'R' : payload?.path}
          </div>
          <div>
            <div style={{ fontWeight: 'bold', fontSize: '16px' }}>
              {payload?.path === 'root' ? 'Root Node' : `Node "${payload?.path}"`}
            </div>
            <div style={{ fontSize: '12px', color: '#888' }}>
              Level {payload?.level} {payload?.is_leaf ? '(leaf)' : ''}
            </div>
          </div>
        </div>
      </DraggableIDMarker>

      <div style={{
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        fontSize: '13px',
      }}>
        {parentLink && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ color: '#888', width: '50px' }}>Parent:</span>
            <InlineNodeRef thingKey={parentLink.toThingKey} />
          </div>
        )}
        {leftLink && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ color: '#888', width: '50px' }}>Left:</span>
            <InlineNodeRef thingKey={leftLink.toThingKey} />
          </div>
        )}
        {rightLink && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ color: '#888', width: '50px' }}>Right:</span>
            <InlineNodeRef thingKey={rightLink.toThingKey} />
          </div>
        )}
        {payload?.is_leaf && (
          <div style={{ color: '#666', fontStyle: 'italic', marginTop: '4px' }}>
            This is a leaf node (no children)
          </div>
        )}
      </div>
    </div>
  );
}

function TreeNodeRef({ handle }: RefRendererProps) {
  const payload = usePayload(handle) as TreeNodeData | null;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: 'rgba(139, 92, 246, 0.3)',
      borderRadius: 4,
      whiteSpace: 'nowrap',
    }}>
      <span style={{
        width: 20,
        height: 20,
        background: '#8b5cf6',
        borderRadius: '50%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 10,
        fontWeight: 'bold',
        color: '#fff',
        flexShrink: 0,
      }}>
        {payload?.path === 'root' ? 'R' : payload?.path?.slice(-1)}
      </span>
      <span style={{ color: '#c4b5fd' }}>
        {payload?.path === 'root' ? 'root' : payload?.path}
      </span>
      <span style={{ color: '#888', fontSize: 11 }}>
        L{payload?.level}
      </span>
    </div>
  );
}

function TreeNodeGraphNode({ handle }: GraphNodeRendererProps) {
  const payload = usePayload(handle) as TreeNodeData | null;
  const links = useLinks(handle);

  const leftLink = links.find(l => l.linkType === 'left');
  const rightLink = links.find(l => l.linkType === 'right');

  return (
    <div style={{
      padding: 8,
      background: '#8b5cf6',
      borderRadius: '50%',
      width: 40,
      height: 40,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
    }}>
      <Port type="target" position={PortPosition.Top} id="in" />

      <span style={{
        color: '#fff',
        fontWeight: 'bold',
        fontSize: payload?.path === 'root' ? 12 : 10,
      }}>
        {payload?.path === 'root' ? 'R' : payload?.path}
      </span>

      {leftLink && (
        <>
          <Port type="source" position={PortPosition.Left} id="left" />
          <LinkEdgeConfig linkKey={leftLink.linkKey} sourcePort="left" targetPort="in" />
        </>
      )}
      {rightLink && (
        <>
          <Port type="source" position={PortPosition.Right} id="right" />
          <LinkEdgeConfig linkKey={rightLink.linkKey} sourcePort="right" targetPort="in" />
        </>
      )}
    </div>
  );
}

export default {
  dialect: "0.1",
  renderers: {
    "TreeNode": {
      full: TreeNodeFull,
      ref: TreeNodeRef,
      graph: TreeNodeGraphNode,
    },
  },
} as ThingWatcherRenderConfig;
