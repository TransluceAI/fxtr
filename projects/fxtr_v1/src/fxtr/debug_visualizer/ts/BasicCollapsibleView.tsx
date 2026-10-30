import React, { useState } from 'react';
import { CollapsibleProvider, CollapsibleContainer, useCollapsibleContext } from './CollapsibleContext';
import { CidString, BytesObject } from './EscapedJsonTypes';
import { useLinkRenderer } from './LinkRendererContext';

interface BasicCollapsibleViewRootProps {
  value: any;
  ipldPath?: any[];
}

export function BasicCollapsibleViewRoot({ value, ipldPath = [] }: BasicCollapsibleViewRootProps) {
  return (
    <CollapsibleProvider indentLevel={0} forceCollapse={false}>
      <BasicCollapsibleView value={value} ipldPath={ipldPath} />
    </CollapsibleProvider>
  );
}

interface BasicCollapsibleViewProps {
  value: any;
  ipldPath: any[];
}

export function BasicCollapsibleView({ value, ipldPath }: BasicCollapsibleViewProps) {
  const context = useCollapsibleContext();

  // Route to appropriate renderer based on value type
  if (value === null) {
    return <PrimitiveValue value={value} />;
  }

  // Handle special escaped JSON types
  if (value instanceof CidString) {
    return <CidValue value={value} ipldPath={ipldPath} />;
  }

  if (value instanceof BytesObject) {
    return <BytesValue value={value} />;
  }

  if (typeof value === 'string') {
    // Check if string is multiline or long and needs special handling
    if (value.includes('\n') || value.length > 80) {
      return <MultilineStringValue value={value} />;
    }
    return <PrimitiveValue value={value} />;
  }

  if (typeof value === 'number' || typeof value === 'boolean') {
    return <PrimitiveValue value={value} />;
  }

  if (Array.isArray(value)) {
    return <ArrayValue value={value} ipldPath={ipldPath} />;
  }

  if (typeof value === 'object') {
    return <ObjectValue value={value} ipldPath={ipldPath} />;
  }

  // Fallback for unknown types
  return <PrimitiveValue value={value} />;
}

interface PrimitiveValueProps {
  value: any;
}

function PrimitiveValue({ value }: PrimitiveValueProps) {
  const renderValue = () => {
    if (value === null) return 'None';
    return JSON.stringify(value);
  };

  const getValueStyle = () => {
    if (value === null) return { color: '#666' };
    if (typeof value === 'string') return { color: '#d73a49' };
    if (typeof value === 'number') return { color: '#005cc5' };
    if (typeof value === 'boolean') return { color: '#e36209' };
    return {};
  };

  return (
    <span style={{
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      ...getValueStyle()
    }}>
      {renderValue()}
    </span>
  );
}

interface MultilineStringValueProps {
  value: string;
}

function MultilineStringValue({ value }: MultilineStringValueProps) {
  const [wrapLines, setWrapLines] = useState(true);

  const handleWrapToggle = (e: React.ChangeEvent<HTMLInputElement>) => {
    setWrapLines(e.target.checked);
  };

  return (
    <CollapsibleContainer
      startCollapsed={true}
      incrementIndent={2}
      renderCollapsed={() => (
        <span
          style={{
            fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
            color: '#d73a49'
          }}
        >
          {JSON.stringify(value)}
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
        }}>
          <CollapseTarget><span style={{ color: '#d73a49' }}>⎡</span></CollapseTarget>
          {'\n'}{indentString}{'  '}
          <div style={{
            background: 'rgba(255, 165, 0, 0.1)', // Light orange background
            border: '1px solid rgba(255, 165, 0, 0.3)',
            borderRadius: '4px',
            padding: '8px',
            margin: '4px 0',
            whiteSpace: wrapLines ? 'pre-wrap' : 'pre',
            display: 'inline-block',
            minWidth: '200px',
            ...(wrapLines && { maxWidth: '100vw' })
          }}>
            <div style={{
              marginBottom: '4px',
              fontSize: '12px',
              color: '#666',
              background: 'rgba(179, 179, 179, 0.1)',
              border: '1px solid rgba(255, 165, 0, 0.3)',
            }}>
              <label style={{ display: 'flex', alignItems: 'center', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={wrapLines}
                  onChange={handleWrapToggle}
                  style={{ marginRight: '6px' }}
                  onClick={(e) => e.stopPropagation()}
                />
                Wrap lines
              </label>
            </div>
            {value}
          </div>
          {'\n'}{indentString}<CollapseTarget><span style={{ color: '#d73a49' }}>⎦</span></CollapseTarget>
        </span>
      )}
    />
  );
}

interface ArrayValueProps {
  value: any[];
  ipldPath: any[];
}

function ArrayValue({ value, ipldPath }: ArrayValueProps) {
  if (value.length === 0) {
    return <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>[]</span>;
  }

  return (
    <CollapsibleContainer
      incrementIndent={2}
      renderCollapsed={() => (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
        }}>
          [
          {value.map((item, index) => (
            <React.Fragment key={index}>
              <BasicCollapsibleView value={item} ipldPath={[...ipldPath, {tag: 'item_int', key: index}]} />
              {index < value.length - 1 && ', '}
            </React.Fragment>
          ))}
          ]
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
          <CollapseTarget>[</CollapseTarget>
          {value.map((item, index) => (
            <React.Fragment key={index}>
              {'\n'}{indentString}{'  '}
              <BasicCollapsibleView value={item} ipldPath={[...ipldPath, {tag: 'item_int', key: index}]} />
              {index < value.length - 1 && ','}
            </React.Fragment>
          ))}
          {'\n'}{indentString}
          <CollapseTarget>]</CollapseTarget>
        </span>
      )}
    />
  );
}

interface ObjectValueProps {
  value: object;
  ipldPath: any[];
}

function ObjectValue({ value, ipldPath }: ObjectValueProps) {
  const entries = Object.entries(value);

  if (entries.length === 0) {
    return <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>{"{}"}</span>;
  }

  return (
    <CollapsibleContainer
      incrementIndent={2}
      renderCollapsed={() => (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
        }}>
          {"{"}
          {entries.map(([key, val], index) => (
            <React.Fragment key={key}>
              <span style={{ color: '#032f62' }}>"{key}"</span>{": "}
              <BasicCollapsibleView value={val} ipldPath={[...ipldPath, {tag: 'item_str', key: key}]} />
              {index < entries.length - 1 && ', '}
            </React.Fragment>
          ))}
          {"}"}
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
          <CollapseTarget>{"{"}</CollapseTarget>
          {entries.map(([key, val], index) => (
            <React.Fragment key={key}>
              {'\n'}{indentString}{'  '}
              <span style={{ color: '#032f62' }}>"{key}"</span>{": "}
              <BasicCollapsibleView value={val} ipldPath={[...ipldPath, {tag: 'item_str', key: key}]} />
              {index < entries.length - 1 && ','}
            </React.Fragment>
          ))}
          {'\n'}{indentString}
          <CollapseTarget>{"}"}</CollapseTarget>
        </span>
      )}
    />
  );
}

interface CidValueProps {
  value: CidString;
  ipldPath: any[];
}

function CidValue({ value, ipldPath }: CidValueProps) {
  const linkRenderer = useLinkRenderer();
  return <>{linkRenderer(value, null, ipldPath)}</>;
}

interface BytesValueProps {
  value: BytesObject;
}

function BytesValue({ value }: BytesValueProps) {
  return (
    <span style={{
      fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
      color: '#8a2be2', // Blue-violet color for bytes
      border: '1px solid #8a2be2',
      borderRadius: '3px',
      padding: '1px 4px',
      fontSize: '0.9em'
    }}>
      Bytes({value.base64_byte_str})
    </span>
  );
}
