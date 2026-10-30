/**
 * DebugEmbedApp - Debug UI for testing the embed view
 *
 * Embeds /embed in an iframe and provides controls to send
 * postMessage with thing key and args.
 */

import React, { useState, useRef, useCallback } from 'react';

// =============================================================================
// Styles
// =============================================================================

const styles = {
  container: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100%',
    background: '#1a1a2e',
  },
  header: {
    padding: '12px 16px',
    borderBottom: '1px solid #333',
    background: '#252545',
  },
  headerTitle: {
    margin: 0,
    fontSize: '16px',
    color: '#fff',
  },
  content: {
    display: 'flex',
    flex: 1,
    overflow: 'hidden',
  },
  controls: {
    width: '350px',
    padding: '16px',
    borderRight: '1px solid #333',
    overflow: 'auto',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '16px',
  },
  embedContainer: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column' as const,
    overflow: 'hidden',
  },
  embedHeader: {
    padding: '8px 16px',
    background: '#222',
    borderBottom: '1px solid #333',
    fontSize: '12px',
    color: '#888',
  },
  iframe: {
    flex: 1,
    border: 'none',
    background: '#1a1a2e',
  },
  fieldGroup: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '6px',
  },
  label: {
    fontSize: '12px',
    color: '#888',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
  },
  input: {
    width: '100%',
    padding: '10px 12px',
    background: '#222',
    border: '1px solid #444',
    borderRadius: '4px',
    color: '#eee',
    fontFamily: 'monospace',
    fontSize: '13px',
  },
  textarea: {
    width: '100%',
    padding: '10px 12px',
    background: '#222',
    border: '1px solid #444',
    borderRadius: '4px',
    color: '#eee',
    fontFamily: 'monospace',
    fontSize: '13px',
    resize: 'vertical' as const,
    minHeight: '80px',
  },
  select: {
    padding: '8px 12px',
    background: '#222',
    border: '1px solid #444',
    borderRadius: '4px',
    color: '#eee',
    fontSize: '13px',
  },
  button: {
    padding: '12px 20px',
    background: '#4a7',
    border: 'none',
    borderRadius: '4px',
    color: '#fff',
    fontSize: '14px',
    fontWeight: 'bold' as const,
    cursor: 'pointer',
    transition: 'background 0.2s',
  },
  buttonHover: {
    background: '#5b8',
  },
  status: {
    padding: '12px',
    background: '#222',
    borderRadius: '4px',
    fontSize: '12px',
    color: '#888',
    fontFamily: 'monospace',
  },
  statusSuccess: {
    color: '#4a7',
  },
  statusError: {
    color: '#f66',
  },
  divider: {
    height: '1px',
    background: '#333',
    margin: '8px 0',
  },
  presets: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '8px',
  },
  presetButton: {
    padding: '8px 12px',
    background: '#333',
    border: '1px solid #444',
    borderRadius: '4px',
    color: '#aaa',
    fontSize: '12px',
    cursor: 'pointer',
    textAlign: 'left' as const,
  },
};

// =============================================================================
// Component
// =============================================================================

type KeyType = 'keyJSON' | 'key';

interface Preset {
  name: string;
  keyType: KeyType;
  key: string;
  args: string;
}

const presets: Preset[] = [
  {
    name: 'Widget Alpha (highlight title)',
    keyType: 'keyJSON',
    key: '{"widget": "alpha"}',
    args: '{"highlight": "title", "message": "Hello from debug!"}',
  },
  {
    name: 'Widget Beta (highlight count)',
    keyType: 'keyJSON',
    key: '{"widget": "beta"}',
    args: '{"highlight": "count"}',
  },
];

export function DebugEmbedApp() {
  const iframeRef = useRef<HTMLIFrameElement>(null);

  // Form state
  const [keyType, setKeyType] = useState<KeyType>('keyJSON');
  const [keyValue, setKeyValue] = useState('');
  const [argsValue, setArgsValue] = useState('{}');

  // Status
  const [status, setStatus] = useState<{ message: string; type: 'success' | 'error' | 'info' }>({
    message: 'Ready. Enter a thing key and click Send.',
    type: 'info',
  });

  // Send postMessage to iframe
  const handleSend = useCallback(() => {
    const iframe = iframeRef.current;
    if (!iframe?.contentWindow) {
      setStatus({ message: 'Iframe not ready', type: 'error' });
      return;
    }

    // Validate key
    if (!keyValue.trim()) {
      setStatus({ message: 'Please enter a key', type: 'error' });
      return;
    }

    // Build message
    let message: Record<string, unknown> = { type: 'showThing' };

    try {
      if (keyType === 'keyJSON') {
        // Parse as JSON
        message.keyJSON = JSON.parse(keyValue);
      } else {
        // Use as base64 string
        message.key = keyValue;
      }
    } catch (e) {
      setStatus({ message: `Invalid key: ${e}`, type: 'error' });
      return;
    }

    // Parse args
    try {
      const args = JSON.parse(argsValue);
      if (typeof args === 'object' && args !== null) {
        message.args = args;
      }
    } catch (e) {
      setStatus({ message: `Invalid args JSON: ${e}`, type: 'error' });
      return;
    }

    // Send message
    iframe.contentWindow.postMessage(message, '*');

    setStatus({
      message: `Sent: ${JSON.stringify(message, null, 2)}`,
      type: 'success',
    });
  }, [keyType, keyValue, argsValue]);

  // Apply preset
  const handlePreset = useCallback((preset: Preset) => {
    setKeyType(preset.keyType);
    setKeyValue(preset.key);
    setArgsValue(preset.args);
    setStatus({ message: `Preset loaded: ${preset.name}`, type: 'info' });
  }, []);

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <h1 style={styles.headerTitle}>Thingwatcher Embed Debug</h1>
      </div>

      {/* Content */}
      <div style={styles.content}>
        {/* Controls */}
        <div style={styles.controls}>
          {/* Key Type */}
          <div style={styles.fieldGroup}>
            <label style={styles.label}>Key Type</label>
            <select
              style={styles.select}
              value={keyType}
              onChange={(e) => setKeyType(e.target.value as KeyType)}
            >
              <option value="keyJSON">keyJSON (JSON value)</option>
              <option value="key">key (base64)</option>
            </select>
          </div>

          {/* Key Value */}
          <div style={styles.fieldGroup}>
            <label style={styles.label}>
              {keyType === 'keyJSON' ? 'Key (JSON)' : 'Key (Base64)'}
            </label>
            <input
              type="text"
              style={styles.input}
              value={keyValue}
              onChange={(e) => setKeyValue(e.target.value)}
              placeholder={keyType === 'keyJSON' ? '{"id": 123}' : 'base64...'}
            />
          </div>

          {/* Args */}
          <div style={styles.fieldGroup}>
            <label style={styles.label}>Args (JSON)</label>
            <textarea
              style={styles.textarea}
              value={argsValue}
              onChange={(e) => setArgsValue(e.target.value)}
              placeholder='{"highlight": "title"}'
            />
          </div>

          {/* Send Button */}
          <button
            style={styles.button}
            onClick={handleSend}
            onMouseEnter={(e) => e.currentTarget.style.background = '#5b8'}
            onMouseLeave={(e) => e.currentTarget.style.background = '#4a7'}
          >
            Send to Embed
          </button>

          {/* Status */}
          <pre
            style={{
              ...styles.status,
              ...(status.type === 'success' ? styles.statusSuccess : {}),
              ...(status.type === 'error' ? styles.statusError : {}),
            }}
          >
            {status.message}
          </pre>

          {/* Divider */}
          <div style={styles.divider} />

          {/* Presets */}
          <div style={styles.fieldGroup}>
            <label style={styles.label}>Presets (for demo server)</label>
            <div style={styles.presets}>
              {presets.map((preset, i) => (
                <button
                  key={i}
                  style={styles.presetButton}
                  onClick={() => handlePreset(preset)}
                >
                  {preset.name}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Embed Container */}
        <div style={styles.embedContainer}>
          <div style={styles.embedHeader}>
            Embed View (/embed)
          </div>
          <iframe
            ref={iframeRef}
            src="/embed"
            style={styles.iframe}
            title="Embed View"
          />
        </div>
      </div>
    </div>
  );
}
