import React, { useState, useEffect } from 'react';
import { EntityKeyLink } from './EntityKeyLink';
import { BasicCollapsibleViewRoot } from './BasicCollapsibleView';
import { convertEscapedJson, CidString } from './EscapedJsonTypes';
import { EntityView, renderNestedEntitiesByCid, createEntityDefinitionPathAwareLinkRenderer, renderPythonPath, renderCanonicalPythonPath } from './HubValueView';
import { LinkRendererProvider } from './LinkRendererContext';
import { createChildPathMapping } from './module_util';

interface EntityDefinitionPage {
  moduleCid: string;
  entityIndex: number;
}

export function EntityDefinitionPage({ moduleCid, entityIndex }: EntityDefinitionPage) {
  const [moduleData, setModuleData] = useState<any>(null);
  const [entityDefnData, setEntityDefnData] = useState<any>(null);
  const [entityData, setEntityData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [entityDefnError, setEntityDefnError] = useState<string | null>(null);
  const [entityError, setEntityError] = useState<string | null>(null);
  const [childPathMapping, setChildPathMapping] = useState<Map<string, { moduleCid: string; offset: number }> | null>(null);
  const [typespecPathMapping, setTypespecPathMapping] = useState<Map<string, { moduleCid: string; offset: number }> | null>(null);

  useEffect(() => {
    const loadEntityDefinition = async () => {
      if (!moduleCid || entityIndex < 0) {
        setError('Invalid module CID or entity index');
        setLoading(false);
        return;
      }

      try {
        // First fetch the module data
        const moduleResponse = await fetch(`/block/${moduleCid}`);
        if (!moduleResponse.ok) {
          throw new Error(`Failed to fetch module: ${moduleResponse.statusText}`);
        }
        const rawModData = await moduleResponse.json();
        const modData = convertEscapedJson(rawModData);
        setModuleData(modData);

        // Find the entity definition link at the specified index
        const entityDefnLinks = modData?.entity_defns || [];
        if (entityIndex >= entityDefnLinks.length) {
          throw new Error(`Entity definition index ${entityIndex} out of range (0-${entityDefnLinks.length - 1})`);
        }

        const entityDefnLink = entityDefnLinks[entityIndex];
        const entityDefnCid = (entityDefnLink as CidString).base32_cid_str;

        if (!entityDefnCid) {
          setEntityDefnError('No entity definition CID found in link');
          setLoading(false);
          return;
        }

        // Fetch the actual entity definition data
        try {
          const entityDefnResponse = await fetch(`/block/${entityDefnCid}`);
          if (!entityDefnResponse.ok) {
            throw new Error(`Failed to fetch entity definition: ${entityDefnResponse.statusText}`);
          }
          const rawDefnData = await entityDefnResponse.json();
          const defnData = convertEscapedJson(rawDefnData);
          setEntityDefnData(defnData);

          // Create child path mapping for Stage 8b link renderer
          const pathMapping = createChildPathMapping(moduleCid, defnData);
          setChildPathMapping(pathMapping);

          // Now try to get the actual entity from the definition
          const entityLink = defnData?.entity;
          const entityCid = (entityLink as CidString).base32_cid_str;
          if (!entityCid) {
            setEntityError('No entity CID found in entity definition');
          } else {
            try {
              const entityResponse = await fetch(`/block/${entityCid}`);
              if (!entityResponse.ok) {
                throw new Error(`Failed to fetch entity: ${entityResponse.statusText}`);
              }
              const rawEntData = await entityResponse.json();
              const entData = convertEscapedJson(rawEntData);
              setEntityData(entData);
            } catch (entityErr) {
              setEntityError(entityErr instanceof Error ? entityErr.message : 'Unknown error fetching entity');
            }
          }
        } catch (defnErr) {
          setEntityDefnError(defnErr instanceof Error ? defnErr.message : 'Unknown error fetching entity definition');
        }

        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    };

    loadEntityDefinition();
  }, [moduleCid, entityIndex]);

  if (loading) {
    return <div>Loading...</div>;
  }

  if (error) {
    console.error(error);
    return <div>Error: {error}</div>;
  }

  const entityDefnLink = moduleData?.entity_defns?.[entityIndex];
  const entityDefnCid = (entityDefnLink as CidString).base32_cid_str;

  // Get collection name - prefer name if it exists and is not null/empty, otherwise use namespace_id
  let collectionName = 'Unknown';
  if (moduleData) {
    console.log('EntityDefinitionPage moduleData:', moduleData);
    console.log('moduleData.name:', moduleData.name);
    console.log('moduleData.namespace_id:', moduleData.namespace_id);
    if (moduleData.name && moduleData.name !== '') {
      collectionName = moduleData.name;
    } else if (moduleData.namespace_id) {
      collectionName = moduleData.namespace_id;
    }
  } else {
    console.log('EntityDefinitionPage: moduleData is null/undefined');
  }

  // Use path-aware link renderer if we have the child path mapping, otherwise fall back to basic renderer
  const linkRenderer = childPathMapping
    ? createEntityDefinitionPathAwareLinkRenderer(
        childPathMapping,
        moduleCid,
        entityDefnData?.canonical_access_path
      )
    : renderNestedEntitiesByCid;

  const entityCid = entityDefnData?.entity.base32_cid_str;

  return (
      <div style={{ padding: '20px', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' }}>
      <nav style={{ marginBottom: '20px' }}>
        <a href={`/view/collection/${moduleCid}`} style={{ color: '#0066cc', textDecoration: 'none' }}>
          ← Back to Collection: {collectionName}
        </a>
      </nav>

      <h1>Entity Definition #{entityIndex}</h1>
      {entityDefnCid && (
        <>
        <p style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
          Entity CID: <code style={{ background: '#f8f9fa', padding: '2px 4px', borderRadius: '3px' }}>{entityCid}</code>
        </p>
        <p style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
          Definition CID: <code style={{ background: '#f8f9fa', padding: '2px 4px', borderRadius: '3px' }}>{entityDefnCid}</code>
        </p>
        <p style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
          Canonical path: {
            entityDefnData.canonical_access_path ?
              <code style={{ background: '#f8f9fa', padding: '2px 4px', borderRadius: '3px' }}>{renderCanonicalPythonPath(collectionName, entityDefnData.canonical_access_path)}</code>
              :
              <span style={{ color: '#666' }}>None</span>
          }
        </p>
        {entityDefnData?.aliases && entityDefnData.aliases.length > 0 && (
          <div style={{ color: '#666', fontSize: '14px', marginBottom: '20px' }}>
            <span>Aliases:</span>
            <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
              {entityDefnData.aliases.map((alias: any, index: number) => (
                <li key={index} style={{ marginBottom: '4px' }}>
                  <code style={{ background: '#f8f9fa', padding: '2px 4px', borderRadius: '3px' }}>
                    {renderCanonicalPythonPath(collectionName, alias)}
                  </code>
                </li>
              ))}
            </ul>
          </div>
        )}
        </>
      )}

      {entityDefnData?.documentation && (
        <div style={{ marginBottom: '20px' }}>
          <h2>Documentation</h2>
          <div style={{
            background: '#f8f9fa',
            padding: '15px',
            borderRadius: '4px',
            border: '1px solid #e9ecef',
            fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
            whiteSpace: 'pre'
          }}>
            {entityDefnData.documentation}
          </div>
        </div>
      )}

      <div style={{ marginBottom: '20px' }}>
        <h2>Entity</h2>
        {entityError ? (
          <div style={{
            background: '#ffe6e6',
            padding: '15px',
            borderRadius: '4px',
            border: '1px solid #ff9999',
            color: '#cc0000',
            fontSize: '14px'
          }}>
            <strong>Error loading entity data:</strong> {entityError}
          </div>
        ) : (
          <div style={{
            background: '#f0f8ff',
            padding: '15px',
            borderRadius: '4px',
            overflow: 'auto',
            whiteSpace: 'pre',
            border: '1px solid #b3d9ff',
            fontSize: '12px'
          }}>
            <LinkRendererProvider linkRenderer={linkRenderer}>
              <EntityView
                entitySpec={entityData.$typespec}
                entityData={entityData}
                collapseBehavior={null}
                startExpanded={true}
              />
            </LinkRendererProvider>
          </div>
        )}
      </div>

      {entityData && (
        <div style={{ marginBottom: '20px' }}>
          <details style={{ marginBottom: '10px' }}>
            <summary style={{ cursor: 'pointer', fontWeight: 'bold' }}>Show/Hide Raw Entity Data</summary>
            <div style={{
              background: '#f8f9fa',
              padding: '15px',
              borderRadius: '4px',
              overflow: 'auto',
              whiteSpace: 'pre',
              border: '1px solid #e9ecef',
              fontSize: '12px',
              marginTop: '10px'
            }}>
              <LinkRendererProvider linkRenderer={linkRenderer}>
                <BasicCollapsibleViewRoot value={entityData} />
              </LinkRendererProvider>
            </div>
          </details>
        </div>
      )}

      <div style={{ marginBottom: '20px' }}>
        {entityDefnError ? (
          <div style={{
            background: '#ffe6e6',
            padding: '15px',
            borderRadius: '4px',
            border: '1px solid #ff9999',
            color: '#cc0000',
            fontSize: '14px'
          }}>
            <strong>Error loading entity definition:</strong> {entityDefnError}
            {entityDefnLink && (
              <div style={{ marginTop: '10px', fontSize: '12px' }}>
                <strong>Raw link data:</strong>
                <pre style={{ marginTop: '5px', background: '#fff', padding: '5px', border: '1px solid #ddd' }}>
                  {JSON.stringify(entityDefnLink, null, 2)}
                </pre>
              </div>
            )}
          </div>
        ) : (
          <details style={{ marginBottom: '10px' }}>
            <summary style={{ cursor: 'pointer', fontWeight: 'bold' }}>Show/Hide Raw Entity Definition</summary>
            <div style={{
              background: '#f8f9fa',
              padding: '15px',
              borderRadius: '4px',
              overflow: 'auto',
              whiteSpace: 'pre',
              border: '1px solid #e9ecef',
              fontSize: '12px',
              marginTop: '10px'
            }}>
              <BasicCollapsibleViewRoot value={entityDefnData} />
            </div>
          </details>
        )}
      </div>

      </div>
  );
}
