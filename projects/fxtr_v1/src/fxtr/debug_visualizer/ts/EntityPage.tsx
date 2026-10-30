import React, { useState, useEffect } from 'react';
import { BasicCollapsibleViewRoot } from './BasicCollapsibleView';
import { EntityView, renderNestedEntitiesByCid } from './HubValueView';
import { convertEscapedJson } from './EscapedJsonTypes';
import { LinkRendererProvider } from './LinkRendererContext';

interface EntityPageProps {
  entityCid: string;
}

export function EntityPage({ entityCid }: EntityPageProps) {
  const [entityData, setEntityData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadEntityData = async () => {
      if (!entityCid) {
        setError('No entity CID provided');
        setLoading(false);
        return;
      }

      try {
        const response = await fetch(`/block/${entityCid}`);
        if (!response.ok) {
          throw new Error(`Failed to fetch: ${response.statusText}`);
        }
        const rawData = await response.json();
        const convertedData = convertEscapedJson(rawData);
        setEntityData(convertedData);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    };

    loadEntityData();
  }, [entityCid]);

  if (loading) {
    return <div>Loading...</div>;
  }

  if (error) {
    console.error(error);
    return <div>Error: {error}</div>;
  }

  return (
      <div style={{ padding: '20px', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' }}>
        <h1>Entity Data</h1>
        <p style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
          CID: <code style={{ background: '#f8f9fa', padding: '2px 4px', borderRadius: '3px' }}>{entityCid}</code>
        </p>

        <div style={{
          background: '#f8f9fa',
          padding: '15px',
          borderRadius: '4px',
          overflow: 'auto',
          whiteSpace: 'pre',
          border: '1px solid #e9ecef',
          fontSize: '12px'
        }}>
          <LinkRendererProvider linkRenderer={renderNestedEntitiesByCid}>
            <EntityView
              entitySpec={entityData.$typespec}
              entityData={entityData}
              collapseBehavior={null}
              startExpanded={true}
            />
          </LinkRendererProvider>
        </div>

        <details style={{ marginBottom: '20px' }}>
        <summary style={{ cursor: 'pointer', fontWeight: 'bold' }}>Show/Hide Raw Entity Data</summary>
        <div style={{
          background: '#f8f9fa',
          padding: '15px',
          borderRadius: '4px',
          overflow: 'auto',
          whiteSpace: 'pre',
          border: '1px solid #e9ecef',
          fontSize: '12px'
        }}>
          <BasicCollapsibleViewRoot value={entityData} />
        </div>
      </details>
      </div>
  );
}
