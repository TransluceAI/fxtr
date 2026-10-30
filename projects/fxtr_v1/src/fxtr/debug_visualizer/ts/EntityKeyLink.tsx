import React from 'react';
import { CidString } from './EscapedJsonTypes';

interface EntityKeyLinkProps {
  entityKey: any;
  currentModuleCid: string;
  prefix?: string;
}

export function EntityKeyLink({ entityKey, currentModuleCid, prefix = "" }: EntityKeyLinkProps) {
  if (!entityKey) {
    return <span style={{ color: '#cc0000' }}>{prefix}No entity key provided</span>;
  }

  // Handle LocalEntityKey (same collection)
  if (entityKey.tag === 'local' && entityKey.definition_offset !== undefined) {
    return (
      <a
        href={`/view/collection/${currentModuleCid}/entdefn/${entityKey.definition_offset}`}
        style={{
          color: '#0066cc',
          textDecoration: 'none',
          padding: '5px 10px',
          border: '1px solid #ddd',
          borderRadius: '3px',
          display: 'inline-block'
        }}
      >
        {prefix}Local Entity #{entityKey.definition_offset}
      </a>
    );
  }

  // Handle ImportedEntityKey (different collection)
  if (entityKey.tag === 'import' && entityKey.definition_offset !== undefined && entityKey.module) {
    const moduleCid = (entityKey.module as CidString).base32_cid_str;
    return (
      <a
        href={`/view/collection/${moduleCid}/entdefn/${entityKey.definition_offset}`}
        style={{
          color: '#0066cc',
          textDecoration: 'none',
          padding: '5px 10px',
          border: '1px solid #ddd',
          borderRadius: '3px',
          display: 'inline-block'
        }}
      >
        {prefix}Imported Entity #{entityKey.definition_offset} (from other collection)
      </a>
    );
  }

  // Fallback for unknown key structure
  return <span style={{ color: '#666' }}>{prefix}Unknown entity key type: {JSON.stringify(entityKey)}</span>;
}
