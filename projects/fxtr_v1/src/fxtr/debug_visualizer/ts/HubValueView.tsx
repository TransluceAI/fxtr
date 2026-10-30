import React, { useState, useEffect } from 'react';
import { BasicCollapsibleView } from './BasicCollapsibleView';
import { CollapsibleProvider, CollapsibleContainer, useCollapsibleContext } from './CollapsibleContext';
import { convertEscapedJson, CidString } from './EscapedJsonTypes';
import { useLinkRenderer, LinkRendererProvider, defaultLinkRenderer } from './LinkRendererContext';
import { loadEntityDefinition, createChildPathMapping, canonicalKeyStrForIPLDPath } from './module_util';

// Helper function to render a canonical Python path
export function renderPythonPath(renderedRoot: string, pathSteps: any[]): string {
  let result = renderedRoot;

  for (const step of pathSteps) {
    if (step.tag === 'attr') {
      result += `.${step.attr_name}`;
    } else if (step.tag === 'item_int') {
      result += `[${step.key}]`;
    } else if (step.tag === 'item_str') {
      result += `[${JSON.stringify(step.key)}]`;
    } else if (step.tag === 'item_cid') {
      result += `[${step.key}]`;
    } else if (step.tag === 'data_entity_class_typespec') {
      result += '.value_typespec';
    } else if (step.tag === 'protocol_spec') {
      result = `protocol_spec(${result})`;
    } else {
      // Fallback for unrecognized tags
      result += `[unrecognized=${JSON.stringify(step)}]`;
    }
  }

  return result;
}

export function renderCanonicalPythonPath(moduleName: string, pathSteps: any[]): string {
  return renderPythonPath(`collection(${moduleName})`, pathSteps);
}

interface HubValueViewProps {
  value: any;
  spec: any;
  ipldPath: any[];
}

const USER_DEFINED_ENTITY_KINDS = [
  'data_entity'
];
const UNION_LIKE_ENTITY_KINDS = [
  'ent_union', 'any_entity', 'protocol_impl_plan', 'any_ext_svc_id', 'data_entity_fwdref'
];
const BUILTIN_ENTITY_TYPE_SPECS = {
  'data_ent_schema': {
    "typeName": "DataEntitySchema",
    "contentSpec": {
      'namespace_id': { '$kind': 'str' },
      'type_id': { '$kind': 'str' },
      'field_type_specs': {
        '$kind': 'fdict_str',
        'value_type': { '$kind': 'type_spec' }
      }
      // Note: _doc field is not serialized so we don't include it
    }
  },
  'protocol_spec': {
    "typeName": "ProtocolSpec",
    "contentSpec": {
      'namespace_id': { '$kind': 'str' },
      'type_id': { '$kind': 'str' }
    }
  },
  'external_service_identifier': {
    "typeName": "ExternalServiceIdentifier",
    "contentSpec": {
      'namespace_id': { '$kind': 'str' },
      'service_id': { '$kind': 'str' },
      'protocol_spec': { '$kind': 'protocol_spec' }
    }
  },
  'task_run_result_handle': {
    "typeName": "TaskResultHandle",
    "contentSpec": {
      'task_ref': { '$kind': 'external_cid' }
    }
  },
  'task': {
    "typeName": "Task",
    "contentSpec": {
      'function': { '$kind': 'protocol_impl_plan' },
      'tag': { '$kind': 'any_frozen_ipldable' }
    }
  },
  'run': {
    "typeName": "Run",
    "contentSpec": {
      'task': { '$kind': 'external_cid' },
      'provenance': { '$kind': 'external_cid' },
      'run_data': { '$kind': 'external_cid' },
      'run_info': { '$kind': 'basic_run_info' }
    }
  },
  'impl_provenance_set': {
    "typeName": "ImplProvenanceSet",
    "contentSpec": {
      'provenances': {
        '$kind': 'fdict_cid',
        'value_type': { '$kind': 'any_entity' }
      }
    }
  },
  'adhoc_metadata': {
    "typeName": "AdHocMetadata",
    "contentSpec": {
      'metadata': { '$kind': 'any_frozen_ipldable' }
    }
  },
  'basic_run_info': {
    "typeName": "BasicRunInfo",
    "contentSpec": {
      'author': { '$kind': 'str' },
      'author_email': { '$kind': 'str' },
      'timestamp': { '$kind': 'str' },
      'message': {
        '$kind': 'optional',
        'inner_type': { '$kind': 'str' }
      },
      'metadata': { '$kind': 'adhoc_metadata' }
    }
  }
};

// Helper function to check if a spec should be rendered as an entity
export function isEntitySpec(spec: any): boolean {
  if (!spec || typeof spec !== 'object' || !spec.$kind) {
    return false;
  }

  const kind = spec.$kind;

  // Known entity types
  if (USER_DEFINED_ENTITY_KINDS.includes(kind)) {
    return true;
  }
  if (BUILTIN_ENTITY_TYPE_SPECS[kind] !== undefined) {
    return true;
  }

  // Union-like entity types
  if (UNION_LIKE_ENTITY_KINDS.includes(kind)) {
    return true;
  }

  return false;
}

export function isSpecificEntitySpec(spec: any): boolean {
  if (!spec || typeof spec !== 'object' || !spec.$kind) {
    return false;
  }
  const kind = spec.$kind;
  if (USER_DEFINED_ENTITY_KINDS.includes(kind)) {
    return true;
  }
  if (BUILTIN_ENTITY_TYPE_SPECS[kind] !== undefined) {
    return true;
  }
  return false;
}

export function HubValueView({ value, spec, ipldPath }: HubValueViewProps) {
  // If no spec provided, fall back to unrecognized spec view
  if (!spec || typeof spec !== 'object') {
    return <HubUnrecognizedSpecValueView value={value} spec={spec} reason="missing" ipldPath={ipldPath} />;
  }

  const kind = spec.$kind;
  if (!kind) {
    return <HubUnrecognizedSpecValueView value={value} spec={spec} reason="missing_kind" ipldPath={ipldPath} />;
  }

  // Handle primitive types - use BasicCollapsibleView
  if (['int', 'float', 'str', 'bool', 'none'].includes(kind)) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

  // Handle external_cid spec
  if (kind === 'external_cid') {
    return <ExternalCidReferenceView cid={new CidString(value)} />;
  }

  // Handle any_frozen_ipldable type
  if (kind === 'any_frozen_ipldable') {
    return <AnyFrozenIPLDableView value={value} ipldPath={ipldPath} />;
  }

  // If value is a CID, always use the link renderer with spec
  if (value instanceof CidString) {
    return <LinkWithSpecView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  // Handle optional types
  if (kind === 'optional') {
    if (value === null) {
      return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
    } else {
      // Recursively call HubValueView with the inner type
      return <HubValueView value={value} spec={spec.inner_type} ipldPath={ipldPath} />;
    }
  }

  // Handle tuple types
  if (kind === 'fixed_tuple') {
    return <HubFixedTupleView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  if (kind === 'var_tuple') {
    return <HubVariableTupleView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  // Handle string-keyed frozendict
  if (kind === 'fdict_str') {
    return <HubFrozenDictStringKeyView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  // Handle CID-keyed frozendict
  if (kind === 'fdict_cid') {
    return <HubFrozenDictCIDKeyView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  // Handle entity-keyed frozendict
  if (kind === 'fdict_ent') {
    return <HubFrozenDictEntityKeyView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  // Handle frozenset of entities
  if (kind === 'fset_ent') {
    return <HubFrozenSetOfEntitiesView value={value} spec={spec} ipldPath={ipldPath} />;
  }

  // Handle typespec (bare-bones implementation)
  if (kind === 'typespec') {
    return <span>typespec(<BasicCollapsibleView value={value} ipldPath={ipldPath} />)</span>;
  }

  // Entity rendering does NOT need to go here; entity rendering should instead live in EntityView

  // For all other types, fall back to unrecognized spec view
  return <HubUnrecognizedSpecValueView value={value} spec={spec} reason="unrecognized_kind" ipldPath={ipldPath} />;
}

interface HubFixedTupleViewProps {
  value: any[];
  spec: any;
  ipldPath: any[];
}

function HubFixedTupleView({ value, spec, ipldPath }: HubFixedTupleViewProps) {
  const itemTypes = spec.item_types || [];

  if (!Array.isArray(value)) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

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
              <HubValueView
                value={item}
                spec={itemTypes[index]}
                ipldPath={[...ipldPath, {tag: 'item_int', key: index}]}
              />
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
              <HubValueView
                value={item}
                spec={itemTypes[index]}
                ipldPath={[...ipldPath, {tag: 'item_int', key: index}]}
              />
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

interface LinkWithSpecProps {
  value: CidString;
  spec: any | null;
  ipldPath: any[];
}

function LinkWithSpecView({ value, spec, ipldPath }: LinkWithSpecProps) {
  const linkRenderer = useLinkRenderer();
  return <>{linkRenderer(value, spec, ipldPath)}</>;
}

interface HubVariableTupleViewProps {
  value: any[];
  spec: any;
  ipldPath: any[];
}

function HubVariableTupleView({ value, spec, ipldPath }: HubVariableTupleViewProps) {
  const repeatedType = spec.repeated_type;

  if (!Array.isArray(value)) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

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
              <HubValueView
                value={item}
                spec={repeatedType}
                ipldPath={[...ipldPath, {tag: 'item_int', key: index}]}
              />
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
              <HubValueView
                value={item}
                spec={repeatedType}
                ipldPath={[...ipldPath, {tag: 'item_int', key: index}]}
              />
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

interface HubFrozenDictStringKeyViewProps {
  value: object;
  spec: any;
  ipldPath: any[];
}

function HubFrozenDictStringKeyView({ value, spec, ipldPath }: HubFrozenDictStringKeyViewProps) {
  const valueType = spec.value_type;

  if (typeof value !== 'object' || value === null) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

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
              <span style={{ color: '#032f62' }}>"{key}"</span>:{' '}
              <HubValueView
                value={val}
                spec={valueType}
                ipldPath={[...ipldPath, {tag: 'item_str', key: key}]}
              />
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
              <span style={{ color: '#032f62' }}>"{key}"</span>:{' '}
              <HubValueView
                value={val}
                spec={valueType}
                ipldPath={[...ipldPath, {tag: 'item_str', key: key}]}
              />
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

interface HubFrozenDictCIDKeyViewProps {
  value: object;
  spec: any;
  ipldPath: any[];
}

function HubFrozenDictCIDKeyView({ value, spec, ipldPath }: HubFrozenDictCIDKeyViewProps) {
  const valueType = spec.value_type;

  if (typeof value !== 'object' || value === null) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

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
          {entries.map(([key, val], index) => {
            // Convert base32 CID string back to CidString object for ExternalCidReferenceView
            const cidKey = new CidString(key);
            return (
              <React.Fragment key={key}>
                <ExternalCidReferenceView cid={cidKey} />:{' '}
                <HubValueView
                  value={val}
                  spec={valueType}
                  ipldPath={[...ipldPath, {tag: 'item_str', key: key}]}
                />
                {index < entries.length - 1 && ', '}
              </React.Fragment>
            );
          })}
          {"}"}
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
          <CollapseTarget>{"{"}</CollapseTarget>
          {entries.map(([key, val], index) => {
            // Convert base32 CID string back to CidString object for ExternalCidReferenceView
            const cidKey = new CidString(key);
            return (
              <React.Fragment key={key}>
                {'\n'}{indentString}{'  '}
                <ExternalCidReferenceView cid={cidKey} />:{' '}
                <HubValueView
                  value={val}
                  spec={valueType}
                  ipldPath={[...ipldPath, {tag: 'item_str', key: key}]}
                />
                {index < entries.length - 1 && ','}
              </React.Fragment>
            );
          })}
          {'\n'}{indentString}
          <CollapseTarget>{"}"}</CollapseTarget>
        </span>
      )}
    />
  );
}

interface HubFrozenDictEntityKeyViewProps {
  value: any[];
  spec: any;
  ipldPath: any[];
}

function HubFrozenDictEntityKeyView({ value, spec, ipldPath }: HubFrozenDictEntityKeyViewProps) {
  const keyType = spec.key_type;
  const valueType = spec.value_type;

  if (!Array.isArray(value)) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

  if (value.length === 0) {
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
          {value.map((pair, index) => {
            if (!Array.isArray(pair) || pair.length !== 2) {
              return <span key={index} style={{ color: '#cc0000' }}>Invalid pair</span>;
            }
            const [key, val] = pair;
            return (
              <React.Fragment key={index}>
                <HubValueView
                  value={key}
                  spec={keyType}
                  ipldPath={[...ipldPath, {tag: 'item_int', key: index}, {tag: 'item_int', key: 0}]}
                />:{' '}
                <HubValueView
                  value={val}
                  spec={valueType}
                  ipldPath={[...ipldPath, {tag: 'item_int', key: index}, {tag: 'item_int', key: 1}]}
                />
                {index < value.length - 1 && ', '}
              </React.Fragment>
            );
          })}
          {"}"}
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
          <CollapseTarget>{"{"}</CollapseTarget>
          {value.map((pair, index) => {
            if (!Array.isArray(pair) || pair.length !== 2) {
              return (
                <React.Fragment key={index}>
                  {'\n'}{indentString}{'  '}
                  <span style={{ color: '#cc0000' }}>Invalid pair</span>
                  {index < value.length - 1 && ','}
                </React.Fragment>
              );
            }
            const [key, val] = pair;
            return (
              <React.Fragment key={index}>
                {'\n'}{indentString}{'  '}
                <HubValueView
                  value={key}
                  spec={keyType}
                  ipldPath={[...ipldPath, {tag: 'item_int', key: index}, {tag: 'item_int', key: 0}]}
                />:{' '}
                <HubValueView
                  value={val}
                  spec={valueType}
                  ipldPath={[...ipldPath, {tag: 'item_int', key: index}, {tag: 'item_int', key: 1}]}
                />
                {index < value.length - 1 && ','}
              </React.Fragment>
            );
          })}
          {'\n'}{indentString}
          <CollapseTarget>{"}"}</CollapseTarget>
        </span>
      )}
    />
  );
}

interface HubFrozenSetOfEntitiesViewProps {
  value: any[];
  spec: any;
  ipldPath: any[];
}

function HubFrozenSetOfEntitiesView({ value, spec, ipldPath }: HubFrozenSetOfEntitiesViewProps) {
  const itemType = spec.item_type;

  if (!Array.isArray(value)) {
    return <BasicCollapsibleView value={value} ipldPath={ipldPath} />;
  }

  if (value.length === 0) {
    return <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>frozenset()</span>;
  }

  return (
    <CollapsibleContainer
      incrementIndent={2}
      renderCollapsed={() => (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
        }}>
          frozenset({"{"}
          {value.map((item, index) => (
            <React.Fragment key={index}>
              <HubValueView
                value={item}
                spec={itemType}
                ipldPath={[...ipldPath, {tag: 'item_int', key: index}]}
              />
              {index < value.length - 1 && ', '}
            </React.Fragment>
          ))}
          {"}"})
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
          <CollapseTarget>frozenset({"{"}</CollapseTarget>
          {value.map((item, index) => (
            <React.Fragment key={index}>
              {'\n'}{indentString}{'  '}
              <HubValueView
                value={item}
                spec={itemType}
                ipldPath={[...ipldPath, {tag: 'item_int', key: index}]}
              />
              {index < value.length - 1 && ','}
            </React.Fragment>
          ))}
          {'\n'}{indentString}
          <CollapseTarget>{"}"})</CollapseTarget>
        </span>
      )}
    />
  );
}

interface HubUnrecognizedSpecValueViewProps {
  value: any;
  spec: any;
  reason: 'missing' | 'missing_kind' | 'unrecognized_kind';
  ipldPath: any[];
}

function HubUnrecognizedSpecValueView({ value, spec, reason, ipldPath }: HubUnrecognizedSpecValueViewProps) {
  const [typespecExpanded, setTypespecExpanded] = useState(false);

  const handleTypespecToggle = (e: React.MouseEvent) => {
    setTypespecExpanded(!typespecExpanded);
    e.stopPropagation();
  };

  const getReasonText = () => {
    switch (reason) {
      case 'missing': return 'No typespec provided';
      case 'missing_kind': return 'Typespec missing $kind field';
      case 'unrecognized_kind': return `Unrecognized $kind: ${spec?.$kind}`;
      default: return 'Unknown issue with typespec';
    }
  };

  return (
    <CollapsibleContainer
      incrementIndent={2}
      renderCollapsed={() => (
        <span style={{
          background: 'rgba(255, 193, 7, 0.1)', // Light yellow/warning background
          padding: '2px 4px',
          borderRadius: '3px'
        }}>
          <span style={{ marginRight: '4px' }}>⚠️</span>
          <BasicCollapsibleView value={value} ipldPath={ipldPath} />
        </span>
      )}
      renderExpanded={(indentString, CollapseTarget) => (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
        }}>
          {'\n'}{indentString}{'  '}
          <div style={{
            background: 'rgba(139, 69, 19, 0.1)', // Light reddish-brown background
            border: '1px solid rgba(139, 69, 19, 0.3)',
            borderRadius: '4px',
            padding: '8px',
            margin: '4px 0',
            whiteSpace: 'pre',
            display: 'inline-block',
            minWidth: '200px'
          }}>
            <CollapsibleProvider indentLevel={0} forceCollapse={false}>
              {/* Typespec warning card */}
              <div style={{
                background: 'rgba(139, 69, 19, 0.2)',
                border: '1px solid rgba(139, 69, 19, 0.4)',
                borderRadius: '3px',
                cursor: 'pointer'
              }} onClick={handleTypespecToggle}>
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  fontSize: '12px',
                  color: '#8b4513'
                }}>
                  <span style={{ marginRight: '6px' }}>⚠️</span>
                  <span style={{ fontWeight: 'bold' }}>{getReasonText()}</span>
                  <span style={{ marginLeft: 'auto' }}>{typespecExpanded ? '▼' : '▶'}</span>
                </div>
                {typespecExpanded && (
                  <div style={{
                    marginTop: '8px',
                    padding: '6px',
                    background: 'rgba(255, 255, 255, 0.3)',
                    borderRadius: '2px',
                    fontSize: '11px'
                  }}>
                    {/* Note: this is not strictly a real IPLD path, but we don't actually know the
                    path to the typespec at this point. However we also don't really care about
                    this part for rendering purposes; this is a fallback anyways.*/}
                    <BasicCollapsibleView value={spec} ipldPath={[...ipldPath, {tag: 'unrecognized_typespec'}]} />
                  </div>
                )}
              </div>
              {/* Value content */}
              <BasicCollapsibleView value={value} ipldPath={ipldPath} />
            </CollapsibleProvider>
          </div>
          {'\n'}{indentString}
        </span>
      )}
    />
  );
}

interface AnyFrozenIPLDableViewProps {
  value: any;
  ipldPath: any[];
}

function AnyFrozenIPLDableView({ value, ipldPath }: AnyFrozenIPLDableViewProps) {
  const parentLinkRenderer = useLinkRenderer();
  const anyFrozenLinkRenderer = (cid: CidString, spec: any | null, ipldPath?: any[]) => {
    // Assert that we got null as spec (any_frozen_ipldable doesn't provide type info)
    if (spec !== null) {
      console.warn('Expected null spec for any_frozen_ipldable link renderer, got:', spec);
    }
    // Call parent renderer with any_entity spec
    return parentLinkRenderer(cid, { $kind: 'any_entity' }, ipldPath);
  };

  return (
    <LinkRendererProvider linkRenderer={anyFrozenLinkRenderer}>
      <BasicCollapsibleView value={value} ipldPath={ipldPath} />
    </LinkRendererProvider>
  );
}

interface ExternalCidReferenceViewProps {
  cid: CidString;
}

export function ExternalCidReferenceView({ cid }: ExternalCidReferenceViewProps) {
  const [expanded, setExpanded] = useState(false);
  const [entityData, setEntityData] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleToggle = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();

    if (!expanded && !entityData && !loading) {
      // Fetch the entity data
      setLoading(true);
      try {
        const response = await fetch(`/block/${cid.base32_cid_str}`);
        if (!response.ok) {
          throw new Error(`Failed to fetch entity: ${response.statusText}`);
        }
        const rawData = await response.json();
        const convertedData = convertEscapedJson(rawData);
        setEntityData(convertedData);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    }

    setExpanded(!expanded);
  };

  const handleNavigate = (e: React.MouseEvent) => {
    if (e.button === 1 || (e.button === 0 && (e.ctrlKey || e.metaKey))) {
      // Middle-click or ctrl+click navigates to the entity page
      window.open(`/view/entity/${cid.base32_cid_str}`, '_blank');
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return (
    <span>
      <span
        onClick={handleToggle}
        onAuxClick={handleNavigate}
        style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
          color: '#228b22', // Green color
          fontStyle: 'italic',
          textDecoration: 'none',
          border: '1px solid #228b22',
          borderRadius: '8px',
          padding: '0 0.6ch',
          display: 'inline-block',
          cursor: 'pointer'
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.backgroundColor = 'rgba(34, 139, 34, 0.1)';
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.backgroundColor = 'transparent';
        }}
      >
        <span style={{paddingRight: '0.8ch'}}>⛓</span>{cid.base32_cid_str}
      </span>
      {expanded && (
        <div style={{
          background: 'rgba(34, 139, 34, 0.1)', // Light green background
          border: '1px solid rgba(34, 139, 34, 0.3)',
          borderRadius: '4px',
          padding: '8px',
          margin: '4px 0',
          whiteSpace: 'pre',
          display: 'block',
          minWidth: '200px'
        }}>
          <CollapsibleProvider indentLevel={0} forceCollapse={false}>
            {loading && <span style={{ color: '#666' }}>Loading entity data...</span>}
            {error && <span style={{ color: '#cc0000' }}>Error: {error}</span>}
            {entityData && !loading && !error && (
              <>
                <EntityView
                  entitySpec={entityData.$typespec}
                  entityData={entityData}
                  collapseBehavior={null}
                  startExpanded={true}
                />
              </>
            )}
          </CollapsibleProvider>
        </div>
      )}
    </span>
  );
}

////////////////////////////////////////
//// Entity views                   ////
////////////////////////////////////////

// Types for collapse behavior
export type CollapseBehavior = null | CIDLink | CanonicalPath;

export interface CIDLink {
  type: 'CIDLink';
  cid: CidString;
}

export interface CanonicalPath {
  type: 'CanonicalPath';
  path: string;
}

interface EntityViewProps {
  entitySpec: any;
  entityData?: any;
  collapseBehavior: CollapseBehavior;
  onExpandWithoutData?: () => void;
  startExpanded?: boolean;
}

export function EntityView({ entitySpec, entityData, collapseBehavior, onExpandWithoutData, startExpanded = false }: EntityViewProps) {
  // Check the $kind of the spec to determine what kind of entity this is
  const kind = entitySpec?.$kind;

  if (USER_DEFINED_ENTITY_KINDS.includes(kind)) {
    return (
      <UserDefinedEntityContents
        entitySpec={entitySpec}
        entityData={entityData}
        collapseBehavior={collapseBehavior}
        onExpandWithoutData={onExpandWithoutData}
        startExpanded={startExpanded}
      />
    );
  }

  if (BUILTIN_ENTITY_TYPE_SPECS[kind] !== undefined) {
    return (
      <BuiltinEntityContents
      entitySpec={entitySpec}
        entityData={entityData}
        collapseBehavior={collapseBehavior}
        onExpandWithoutData={onExpandWithoutData}
        startExpanded={startExpanded}
      />
    );
  }

  // For other entity types, fall back to unrecognized entity rendering
  return (
    <UnrecognizedEntityContents
      entitySpec={entitySpec}
      entityData={entityData}
      collapseBehavior={collapseBehavior}
      onExpandWithoutData={onExpandWithoutData}
      startExpanded={startExpanded}
    />
  );
}

interface UserDefinedEntityContentsProps {
  entitySpec: any;
  entityData?: any;
  collapseBehavior: CollapseBehavior;
  onExpandWithoutData?: () => void;
  startExpanded?: boolean;
}

interface BuiltinEntityContentsProps {
  entitySpec: any;
  entityData?: any;
  collapseBehavior: CollapseBehavior;
  onExpandWithoutData?: () => void;
  startExpanded?: boolean;
}

// Placeholder component that triggers onExpandWithoutData when rendered
interface ExpandWithoutDataPlaceholderProps {
  onExpandWithoutData: () => void;
}

function ExpandWithoutDataPlaceholder({ onExpandWithoutData }: ExpandWithoutDataPlaceholderProps) {
  useEffect(() => {
    onExpandWithoutData();
  }, [onExpandWithoutData]);

  return <span style={{ color: '#666' }}>Loading entity data...</span>;
}

const SERVICE_IDENTIFIER_FIELD_TYPE_SPECS = {
  'service_id': { '$kind': 'str' },
  'namespace_id': { '$kind': 'str' },
};

function UserDefinedEntityContents({ entitySpec, entityData, collapseBehavior, onExpandWithoutData, startExpanded = false }: UserDefinedEntityContentsProps) {
  const [schemaData, setSchemaData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadSchemaOrServiceType = async () => {
      try {
        // The spec should have a schema field that's a CID link
        const schemaCid = entitySpec.schema;
        if (!schemaCid || !(schemaCid instanceof CidString)) {
          throw new Error('Invalid data entity spec: missing or invalid schema CID');
        }

        const response = await fetch(`/block/${schemaCid.base32_cid_str}`);
        if (!response.ok) {
          throw new Error(`Failed to fetch schema: ${response.statusText}`);
        }
        const rawSchemaData = await response.json();
        const convertedSchemaData = convertEscapedJson(rawSchemaData);
        setSchemaData(convertedSchemaData);
        setLoading(false);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
        setLoading(false);
      }
    };

    loadSchemaOrServiceType();
  }, [entitySpec]);

  if (loading) {
    return <span style={{ color: '#666' }}>Loading entity schema...</span>;
  }

  if (error) {
    console.error(error);
    return <span style={{ color: '#cc0000' }}>Error loading schema: {error}</span>;
  }

  if (!schemaData) {
    return <span style={{ color: '#cc0000' }}>No schema data available</span>;
  }

  const typeId = schemaData.type_id || 'UnknownEntity';
  const fieldTypeSpecs = entitySpec.$kind === 'data_entity' ? schemaData.field_type_specs : SERVICE_IDENTIFIER_FIELD_TYPE_SPECS;
  const fieldEntries = Object.entries(fieldTypeSpecs);

  if (fieldEntries.length === 0) {
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
        color: '#0066cc'
      }}>
        {typeId}()
      </span>
    );
  }

  return (
    <CollapsibleContainer
      incrementIndent={2}
      startCollapsed={!startExpanded}
      renderCollapsed={() => renderEntityCollapsed(typeId, collapseBehavior, fieldEntries, entityData)}
      renderExpanded={(indentString, CollapseTarget) => renderEntityExpanded(typeId, fieldEntries, entityData, onExpandWithoutData, indentString, CollapseTarget)}
    />
  );
}

function BuiltinEntityContents({ entitySpec, entityData, collapseBehavior, onExpandWithoutData, startExpanded = false }: BuiltinEntityContentsProps) {
  const key = entitySpec.$kind;
  const spec = BUILTIN_ENTITY_TYPE_SPECS[key];
  if (!spec) {
    return <span style={{ color: '#cc0000' }}>Unknown entity kind: {key}</span>;
  }
  const {typeName, contentSpec} = spec;
  const fieldEntries = Object.entries(contentSpec);

  return (
    <CollapsibleContainer
      incrementIndent={2}
      startCollapsed={!startExpanded}
      renderCollapsed={() => renderEntityCollapsed(typeName, collapseBehavior, fieldEntries, entityData)}
      renderExpanded={(indentString, CollapseTarget) => renderEntityExpanded(typeName, fieldEntries, entityData, onExpandWithoutData, indentString, CollapseTarget)}
    />
  );
}

interface UnrecognizedEntityContentsProps {
  entitySpec: any;
  entityData?: any;
  collapseBehavior: CollapseBehavior;
  onExpandWithoutData?: () => void;
  startExpanded?: boolean;
}

function UnrecognizedEntityContents({ entitySpec, entityData, collapseBehavior, onExpandWithoutData, startExpanded = false }: UnrecognizedEntityContentsProps) {
  const kind = entitySpec?.$kind || 'Unknown';

  return (
    <CollapsibleContainer
      incrementIndent={2}
      startCollapsed={!startExpanded}
      renderCollapsed={() => renderUnrecognizedEntityCollapsed(kind, collapseBehavior, entityData)}
      renderExpanded={(indentString, CollapseTarget) => renderUnrecognizedEntityExpanded(kind, entitySpec, entityData, onExpandWithoutData, indentString, CollapseTarget)}
    />
  );
}

function renderEntityCollapsed(typeId: string, collapseBehavior: CollapseBehavior, fieldEntries: [string, any][], entityData: any): React.ReactNode {
  if (collapseBehavior === null) {
    // For null collapse behavior, show all fields inline like dict rendering
    if (!entityData) {
      return (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace',
          color: '#0066cc'
        }}>
          {typeId}(...)
        </span>
      );
    }
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
      }}>
        <span style={{ color: '#0066cc' }}>{typeId}</span>(
        {fieldEntries.map(([fieldName, fieldSpec], index) => (
          <React.Fragment key={fieldName}>
            <span style={{ color: '#666' }}>{fieldName}</span>=
            <HubValueView
              value={entityData[fieldName]}
              spec={fieldSpec}
              ipldPath={[{tag: 'item_str', key: fieldName}]}
            />
            {index < fieldEntries.length - 1 && ', '}
          </React.Fragment>
        ))}
        )
      </span>
    );
  }

  if (collapseBehavior.type === 'CIDLink') {
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
      }}>
        <span style={{ color: '#0066cc' }}>{typeId}(<span style={{
          color: '#aaa',
          fontSize: '0.9em'
        }}>cid:{collapseBehavior.cid.base32_cid_str}</span>)</span>
      </span>
    );
  }

  if (collapseBehavior.type === 'CanonicalPath') {
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
      }}>
        <span style={{ color: '#0066cc' }}>{typeId}(<span style={{
          color: '#aba',
          fontSize: '0.9em'
        }}>{collapseBehavior.path}</span>)</span>
      </span>
    );
  }

  return null;
}

function renderEntityExpanded(
  typeId: string,
  fieldEntries: [string, any][],
  entityData: any,
  onExpandWithoutData?: () => void,
  indentString?: string,
  CollapseTarget?: React.ComponentType<{ children: React.ReactNode }>
): React.ReactNode {
  if (!entityData && onExpandWithoutData) {
    return <ExpandWithoutDataPlaceholder onExpandWithoutData={onExpandWithoutData} />;
  }

  if (!entityData) {
    return <span style={{ color: '#666' }}>Loading entity data...</span>;
  }

  if (!CollapseTarget) {
    // Fallback for when used outside CollapsibleContainer
    CollapseTarget = ({children}: {children: React.ReactNode}) => <>{children}</>;
  }

  return (
    <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
      <CollapseTarget><span style={{ color: '#0066cc' }}>{typeId}</span>(</CollapseTarget>
      {fieldEntries.map(([fieldName, fieldSpec], index) => (
        <React.Fragment key={fieldName}>
          {'\n'}{indentString}{'  '}
          <span style={{ color: '#666' }}>{fieldName}</span>=
          <HubValueView
            value={entityData[fieldName]}
            spec={fieldSpec}
            ipldPath={[{tag: 'item_str', key: fieldName}]}
          />
          {index < fieldEntries.length - 1 && ','}
        </React.Fragment>
      ))}
      {'\n'}{indentString}
      <CollapseTarget>)</CollapseTarget>
    </span>
  );
}

function renderUnrecognizedEntityCollapsed(kind: string, collapseBehavior: CollapseBehavior, entityData: any): React.ReactNode {
  if (collapseBehavior === null) {
    // For null collapse behavior, show all fields inline like dict rendering
    if (!entityData) {
      return (
        <span style={{
          fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
        }}>
          <span style={{ marginRight: '4px' }}>⚠️</span>
          <span style={{ color: '#cc0000' }}>{kind}</span>?(...)
        </span>
      );
    }
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
      }}>
        <span style={{ marginRight: '4px' }}>⚠️</span>
        <span style={{ color: '#cc0000' }}>{kind}</span>?(
        <HubUnrecognizedSpecValueView
          value={entityData}
          spec={entityData.$typespec}
          reason="unrecognized_kind"
          ipldPath={[]}
        />
        )
      </span>
    );
  }

  if (collapseBehavior.type === 'CIDLink') {
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
      }}>
        <span style={{ marginRight: '4px' }}>⚠️</span>
        <span style={{ color: '#cc0000' }}>{kind}</span>?(<span style={{
          color: '#aaa',
          fontSize: '0.9em'
        }}>cid:{collapseBehavior.cid.base32_cid_str}</span>)
      </span>
    );
  }

  if (collapseBehavior.type === 'CanonicalPath') {
    return (
      <span style={{
        fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace'
      }}>
        <span style={{ marginRight: '4px' }}>⚠️</span>
        <span style={{ color: '#cc0000' }}>{kind}</span>?(<span style={{
          color: '#87ceeb',
          fontSize: '0.9em'
        }}>path:{collapseBehavior.path}</span>)
      </span>
    );
  }

  return null;
}

function renderUnrecognizedEntityExpanded(
  kind: string,
  entitySpec: any,
  entityData: any,
  onExpandWithoutData?: () => void,
  indentString?: string,
  CollapseTarget?: React.ComponentType<{ children: React.ReactNode }>
): React.ReactNode {
  if (!entityData && onExpandWithoutData) {
    return <ExpandWithoutDataPlaceholder onExpandWithoutData={onExpandWithoutData} />;
  }

  if (!entityData) {
    return <span style={{ color: '#666' }}>Loading entity data...</span>;
  }

  if (!CollapseTarget) {
    CollapseTarget = ({children}: {children: React.ReactNode}) => <>{children}</>;
  }

  return (
    <span style={{ fontFamily: 'SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace' }}>
      <CollapseTarget>
        <span style={{ marginRight: '4px' }}>⚠️</span>
        <span style={{ color: '#cc0000' }}>{kind}</span>?(
      </CollapseTarget>
      <HubUnrecognizedSpecValueView
        value={entityData}
        spec={entitySpec}
        reason="unrecognized_kind"
        ipldPath={[]}
      />
      <CollapseTarget>)</CollapseTarget>
    </span>
  );
}

// Link renderer that creates EntityViews with CID collapse behavior
export function renderNestedEntitiesByCid(cid: CidString, spec: any | null, ipldPath?: any[]): React.ReactNode {
  return (
    <NestedEntityByCid cid={cid} spec={spec} />
  );
}

interface NestedEntityByCidProps {
  cid: CidString;
  spec: any | null;
}

function NestedEntityByCid({ cid, spec }: NestedEntityByCidProps) {
  // If spec is not an entity spec, use default link renderer
  if (!isEntitySpec(spec)) {
    return defaultLinkRenderer(cid, spec);
  }

  const [entityData, setEntityData] = useState<any>(null);
  const [entitySpec, setEntitySpec] = useState<any>(isSpecificEntitySpec(spec) ? spec : null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // If we don't have a spec, fetch the entity to get the spec
  useEffect(() => {
    if (!entitySpec) {
      const fetchEntityForSpec = async () => {
        setLoading(true);
        try {
          const response = await fetch(`/block/${cid.base32_cid_str}`);
          if (!response.ok) {
            throw new Error(`Failed to fetch entity: ${response.statusText}`);
          }
          const rawData = await response.json();
          const convertedData = convertEscapedJson(rawData);
          setEntityData(convertedData);
          setEntitySpec(convertedData.$typespec);
          setLoading(false);
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Unknown error');
          setLoading(false);
        }
      };
      fetchEntityForSpec();
    }
  }, [cid, entitySpec]);

  const handleExpandWithoutData = async () => {
    if (entityData) return; // Already have data

    setLoading(true);
    try {
      const response = await fetch(`/block/${cid.base32_cid_str}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch entity: ${response.statusText}`);
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

  const handleNavigationClick = (evt: React.MouseEvent<HTMLDivElement>) => {
    console.log("navigation click", evt);
    if (evt.button == 1 || (evt.button == 0 && (evt.ctrlKey || evt.metaKey))) {
      window.location.href = `/view/entity/${cid.base32_cid_str}`;
      evt.preventDefault();
      evt.stopPropagation();
    }
  };


  if (error) {
    console.error(error);
    return <span style={{ color: '#cc0000' }}>Error: {error}</span>;
  }

  if (!entitySpec) {
    if (loading) {
      return <span style={{ color: '#666' }}>Loading entity spec...</span>;
    } else {
      return <span style={{ color: '#cc0000' }}>No entity spec available</span>;
    }
  }

  const collapseBehavior: CIDLink = {
    type: 'CIDLink',
    cid: cid
  };

  return (
    <div onAuxClick={handleNavigationClick} onClick={handleNavigationClick} style={{ display: 'inline' }}>
      <LinkRendererProvider linkRenderer={renderNestedEntitiesByCid}>
        <EntityView
          entitySpec={entitySpec}
          entityData={entityData}
          collapseBehavior={collapseBehavior}
          onExpandWithoutData={handleExpandWithoutData}
        />
      </LinkRendererProvider>
    </div>
  );
}

// Path-aware link renderer for entity definition pages following Stage 8b spec
export function createEntityDefinitionPathAwareLinkRenderer(
  childPathMapping: Map<string, { moduleCid: string; offset: number }>,
  activeModuleCid?: string,
  activeCanonicalPath?: any[]
): (cid: CidString, spec: any | null, ipldPath?: any[]) => React.ReactNode {
  return (cid: CidString, spec: any | null, ipldPath?: any[]) => {
    return (
      <NestedEntityWithDefnLookup
        cid={cid}
        spec={spec}
        ipldPath={ipldPath}
        childPathMapping={childPathMapping}
        activeModuleCid={activeModuleCid}
        activeCanonicalPath={activeCanonicalPath}
      />
    );
  };
}

interface NestedEntityWithDefnLookupProps {
  cid: CidString;
  spec: any | null;
  ipldPath?: any[];
  childPathMapping: Map<string, { moduleCid: string; offset: number }>;
  activeModuleCid?: string;
  activeCanonicalPath?: any[];
}

function NestedEntityWithDefnLookup({ cid, spec, ipldPath, childPathMapping, activeModuleCid, activeCanonicalPath }: NestedEntityWithDefnLookupProps) {
  // Look up the IPLD path in the mapping
  const ipldPathKey = canonicalKeyStrForIPLDPath(ipldPath || []);
  const pathInfo = childPathMapping.get(ipldPathKey);

  if (!pathInfo) {
    console.warn(`NestedEntityWithDefnLookup: No path info found for IPLD path ${ipldPathKey}, falling back to NestedEntityByCid`);
    return <NestedEntityByCid cid={cid} spec={spec} />;
  }
  console.log("Found path info", pathInfo, "for IPLD path", ipldPath);

  const [entityData, setEntityData] = useState<any>(null);
  const [entitySpec, setEntitySpec] = useState<any>(isSpecificEntitySpec(spec) ? spec : null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [defnInfo, setDefnInfo] = useState<{
    loadedDefn: any;
    moduleName: string;
    nestedLinkRenderer: any;
    defnIsCanonicalChild: boolean;
  } | null>(null);

  // Use the async function to load the actual module name and definition object
  useEffect(() => {
    const loadDefn = async () => {
      try {
        const { entityDefinition, moduleName: loadedModuleName } = await loadEntityDefinition(
          pathInfo.moduleCid,
          pathInfo.offset
        );

        // Determine if this entity is a canonical child of the active entity
        let defnIsCanonicalChild = false;
        if (activeModuleCid && activeCanonicalPath &&
            pathInfo.moduleCid === activeModuleCid &&
            entityDefinition.canonical_access_path) {

          // Check if activeCanonicalPath is a prefix of entityDefinition.canonical_access_path
          const activePath = activeCanonicalPath;
          const entityPath = entityDefinition.canonical_access_path;

          if (activePath.length < entityPath.length) {
            defnIsCanonicalChild = activePath.every((step: any, index: number) =>
              JSON.stringify(step) === JSON.stringify(entityPath[index])
            );
          }
        }

        // Create new child path mapping for this entity's definition
        const newChildPathMapping = createChildPathMapping(pathInfo.moduleCid, entityDefinition);

        // Create new link renderer for this entity - pass the new module CID and canonical path
        const newLinkRenderer = createEntityDefinitionPathAwareLinkRenderer(
          newChildPathMapping,
          pathInfo.moduleCid,
          entityDefinition.canonical_access_path
        );

        setDefnInfo({
          loadedDefn: entityDefinition,
          moduleName: loadedModuleName,
          nestedLinkRenderer: newLinkRenderer,
          defnIsCanonicalChild
        });

      } catch (err) {
        console.warn(`Failed to load definition for ${pathInfo.moduleCid}@${pathInfo.offset}: ${err instanceof Error ? err.message : 'Unknown error'}`);
        setError(err instanceof Error ? err.message : 'Unknown error');
      }
    };

    loadDefn();
  }, [pathInfo.moduleCid, pathInfo.offset, activeModuleCid, activeCanonicalPath]);

  // Determine collapse behavior based on loaded definition
  const collapseBehavior: CollapseBehavior = defnInfo?.loadedDefn?.canonical_access_path
    ? {
        type: 'CanonicalPath',
        path: renderCanonicalPythonPath(defnInfo.moduleName, defnInfo.loadedDefn.canonical_access_path)
      }
    : {
        type: 'CIDLink',
        cid: cid
      };

  // If we don't have a spec, fetch the entity to get the spec
  useEffect(() => {
    if (!entitySpec) {
      const fetchEntityForSpec = async () => {
        setLoading(true);
        try {
          const response = await fetch(`/block/${cid.base32_cid_str}`);
          if (!response.ok) {
            throw new Error(`Failed to fetch entity: ${response.statusText}`);
          }
          const rawData = await response.json();
          const convertedData = convertEscapedJson(rawData);
          setEntityData(convertedData);
          setEntitySpec(convertedData.$typespec);
          setLoading(false);
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Unknown error');
          setLoading(false);
        }
      };
      fetchEntityForSpec();
    }
  }, [cid, entitySpec]);

  const handleExpandWithoutData = async () => {
    if (entityData) return; // Already have data

    setLoading(true);
    try {
      const response = await fetch(`/block/${cid.base32_cid_str}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch entity: ${response.statusText}`);
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

  const handleNavigationClick = (evt: React.MouseEvent<HTMLDivElement>) => {
    if ((evt.button == 1 || (evt.button == 0 && (evt.ctrlKey || evt.metaKey))) && pathInfo) {
      // Middle-click or ctrl+click goes to the definition page
      window.location.href = `/view/collection/${pathInfo.moduleCid}/entdefn/${pathInfo.offset}`;
      evt.preventDefault();
      evt.stopPropagation();
    }
  };

  // If we failed to load definition, fall back to NestedEntityByCid
  if (error && !defnInfo) {
    return <NestedEntityByCid cid={cid} spec={spec} />;
  }

  if (!entitySpec) {
    if (loading) {
      return <span style={{ color: '#666' }}>Loading entity spec...</span>;
    } else {
      return <span style={{ color: '#cc0000' }}>No entity spec available</span>;
    }
  }
  if (!defnInfo) {
    if (loading) {
      return <span style={{ color: '#666' }}>Loading entity defn...</span>;
    } else {
      return <span style={{ color: '#cc0000' }}>No entity defn available</span>;
    }
  }

  // Wrap the EntityView with the new link renderer if we have one
  const linkRenderer = defnInfo?.nestedLinkRenderer || renderNestedEntitiesByCid;

  return (
    <div onAuxClick={handleNavigationClick} onClick={handleNavigationClick} style={{ display: 'inline' }}>
      <LinkRendererProvider linkRenderer={linkRenderer}>
        <EntityView
          entitySpec={entitySpec}
          entityData={entityData}
          collapseBehavior={collapseBehavior}
          onExpandWithoutData={handleExpandWithoutData}
          startExpanded={defnInfo?.defnIsCanonicalChild || false}
        />
      </LinkRendererProvider>
    </div>
  );
}
