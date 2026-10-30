// React is auto-imported by JSX transform
import { useState } from 'react';
import {
  usePayload,
  useLinks,
  useTriggerable,
  useTrigger,
  useDraggableIdMarker,
  useThingMetadata,
  usePersistentThingState,
  NodeRef,
  InlineNodeRef,
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';

interface WidgetPayload {
  title?: string;
  messages?: string[];
  messageCount?: number;
}

interface WidgetArgs {
  highlight?: string;
  message?: string;
}

function WidgetFull({ handle, args }: FullRendererProps) {
  const data = usePayload<WidgetPayload>(handle);
  const [triggerable] = useTriggerable(handle);
  const trigger = useTrigger(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const [inputRaw, setInput] = usePersistentThingState<string>(handle, 'input');
  const input = inputRaw || '';
  const [sending, setSending] = useState(false);
  const typedArgs = args as WidgetArgs;
  const highlight = typedArgs?.highlight;
  const argsMessage = typedArgs?.message;

  // Determine the "other" widget for demo purposes
  const otherWidgetId = data?.title === 'Alpha Widget' ? 'beta' : 'alpha';
  const otherWidgetKey = { widget: otherWidgetId };

  const handleTrigger = async () => {
    if (input.trim() && !sending) {
      setSending(true);
      try {
        await trigger({ message: input });
        setInput(undefined);
      } finally {
        setSending(false);
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleTrigger();
    }
  };

  return (
    <div style={{ padding: 16, background: '#222', borderRadius: 8 }}>
      {/* Title with optional highlighting */}
      <DraggableIDMarker>
        <h2 style={{
          margin: '0 0 16px 0',
          color: highlight === 'title' ? '#ff0' : '#fff',
          transition: 'color 0.3s',
          cursor: 'grab'
        }}>
          {data?.title}
        </h2>
      </DraggableIDMarker>

      {/* Demo: InlineNodeRef in flowing text */}
      <p style={{ color: '#aaa', margin: '0 0 12px 0', fontSize: 14 }}>
        See also: <InlineNodeRef keyJSON={otherWidgetKey} /> (inline ref demo)
      </p>

      {/* Demo: NodeRef as block element */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ color: '#888', fontSize: 11, marginBottom: 4 }}>Related widget (block ref):</div>
        <NodeRef keyJSON={otherWidgetKey} />
      </div>

      {/* Args message from postMessage */}
      {argsMessage && (
        <div style={{
          padding: '8px 12px',
          background: '#2a4a2a',
          borderRadius: 4,
          marginBottom: 16,
          color: '#8f8',
          fontSize: 14
        }}>
          Args message: {argsMessage}
        </div>
      )}

      {/* Message list */}
      <div style={{ marginBottom: 16 }}>
        <h4 style={{
          color: highlight === 'messages' ? '#ff0' : '#888',
          fontSize: 12,
          textTransform: 'uppercase',
          margin: '0 0 8px 0',
          transition: 'color 0.3s'
        }}>
          Messages ({data?.messageCount})
        </h4>
        {(data?.messages?.length ?? 0) === 0 ? (
          <p style={{ color: '#666', fontStyle: 'italic', margin: 0 }}>
            No messages yet. Send one below!
          </p>
        ) : (
          <ul style={{
            margin: 0,
            paddingLeft: 20,
            maxHeight: 200,
            overflowY: 'auto'
          }}>
            {data?.messages?.map((msg, i) => (
              <li key={i} style={{ color: '#aaa', marginBottom: 4 }}>
                {msg}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Trigger form */}
      {triggerable && (
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput((e.target as HTMLInputElement).value)}
            onKeyDown={handleKeyDown}
            placeholder="Type a message..."
            disabled={sending}
            style={{
              flex: 1,
              padding: '10px 14px',
              background: '#333',
              border: '1px solid #555',
              borderRadius: 4,
              color: '#fff',
              fontSize: 14,
              outline: 'none'
            }}
          />
          <button
            type="button"
            onClick={handleTrigger}
            disabled={sending || !input.trim()}
            style={{
              padding: '10px 20px',
              background: sending ? '#555' : '#4a7',
              border: 'none',
              borderRadius: 4,
              color: '#fff',
              fontSize: 14,
              cursor: sending ? 'not-allowed' : 'pointer',
              transition: 'background 0.2s'
            }}
          >
            {sending ? 'Sending...' : 'Send'}
          </button>
        </div>
      )}

      {!triggerable && (
        <p style={{ color: '#666', fontStyle: 'italic', margin: 0 }}>
          Widget is not currently triggerable (not in receive loop)
        </p>
      )}
    </div>
  );
}

function WidgetRef({ handle }: RefRendererProps) {
  // Block ref - fills width, shows preview info
  const data = usePayload<WidgetPayload>(handle);

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '4px 8px',
      background: '#2a4a3a',
      borderRadius: 4,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    }}>
      <span style={{
        width: 8,
        height: 8,
        background: '#4a9',
        borderRadius: 2,
        flexShrink: 0,
      }} />
      <span style={{ color: '#4a9', flexShrink: 0 }}>
        Widget: {data?.title}
      </span>
      <span style={{
        color: '#888',
        flex: 1,
        minWidth: 0,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        ({data?.messageCount} messages)
      </span>
    </div>
  );
}

function WidgetInline({ handle }: RefRendererProps) {
  // Inline ref - minimal, flows with text
  const data = usePayload<WidgetPayload>(handle);

  return (
    <span style={{ color: '#4a9' }}>
      <span style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        background: '#4a9',
        borderRadius: 2,
        marginRight: 4,
      }} />
      {data?.title}
    </span>
  );
}

function WidgetPreview({ handle }: PreviewRendererProps) {
  // Preview for hover cards - compact summary with draggable ID and links
  const data = usePayload<WidgetPayload>(handle);
  const links = useLinks(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <DraggableIDMarker>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab' }}>
          <span style={{
            width: 8,
            height: 8,
            background: '#4a9',
            borderRadius: 2,
          }} />
          <span style={{ color: '#4a9', fontWeight: 'bold' }}>
            {data?.title}
          </span>
          <span style={{ color: '#888', fontSize: 11 }}>
            #{shortId}
          </span>
        </div>
      </DraggableIDMarker>
      <div style={{ color: '#aaa', fontSize: 12 }}>
        {data?.messageCount} messages
      </div>
      {(data?.messages?.length ?? 0) > 0 && (
        <div style={{ color: '#666', fontSize: 11, fontStyle: 'italic' }}>
          Latest: {data?.messages?.[data.messages.length - 1]}
        </div>
      )}
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

export default {
  dialect: "0.1",
  renderers: {
    "Widget": {
      full: WidgetFull,
      ref: WidgetRef,
      inline: WidgetInline,
      preview: WidgetPreview,
    },
  }
} as ThingWatcherRenderConfig;
