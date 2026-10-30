import { convertEscapedJson, CidString } from './EscapedJsonTypes';

/**
 * Converts an IPLD path to a canonical string representation by extracting
 * just the keys and stringifying them. This ensures consistent ordering
 * regardless of the order of object properties in the original path objects.
 */
export function canonicalKeyStrForIPLDPath(ipldPath: any[]): string {
  const keys = ipldPath.map(step => {
    if (step.tag === 'item_int' || step.tag === 'item_str') {
      return step.key;
    } else {
      throw new Error(`Unsupported IPLD path step: ${JSON.stringify(step)}`);
    }
  });

  return JSON.stringify(keys);
}

/**
 * Self-contained async function which takes a module CID and definition offset,
 * fetches the corresponding module, looks up the definition offset, fetches the
 * definition by its CID, and finally returns the loaded entity definition object
 * (a LocalEntityDefn) along with the name of the module it was in.
 */
export async function loadEntityDefinition(
  moduleCid: string,
  definitionOffset: number
): Promise<{
  entityDefinition: any;
  moduleName: string;
}> {
  try {
    // First, fetch the module data
    const moduleResponse = await fetch(`/block/${moduleCid}`);
    if (!moduleResponse.ok) {
      throw new Error(`Failed to fetch module: ${moduleResponse.statusText}`);
    }
    const rawModuleData = await moduleResponse.json();
    const moduleData = convertEscapedJson(rawModuleData);

    // Get the module name
    const moduleName = moduleData.name || moduleData.namespace_id || 'Unknown';

    // Look up the entity definition at the specified offset
    const entityDefns = moduleData.entity_defns || [];
    if (definitionOffset >= entityDefns.length) {
      throw new Error(`Definition offset ${definitionOffset} out of range (0-${entityDefns.length - 1})`);
    }

    const entityDefnCid = (entityDefns[definitionOffset] as CidString).base32_cid_str;
    if (!entityDefnCid) {
      throw new Error('No entity definition CID found');
    }

    // Fetch the actual entity definition
    const defnResponse = await fetch(`/block/${entityDefnCid}`);
    if (!defnResponse.ok) {
      throw new Error(`Failed to fetch entity definition: ${defnResponse.statusText}`);
    }
    const rawDefnData = await defnResponse.json();
    const entityDefinition = convertEscapedJson(rawDefnData);

    return {
      entityDefinition,
      moduleName
    };
  } catch (error) {
    throw new Error(`Failed to load entity definition: ${error instanceof Error ? error.message : 'Unknown error'}`);
  }
}

/**
 * Self-contained sync function which takes a current-module CID and LocalEntityDefn
 * object and creates a map from child path (serialized to JSON) to a new definition
 * key {moduleCid: ..., offset: ...}. The module CID comes either from the import key
 * for imported children, or copies the current-module CID if the child path leads to
 * a local definition key.
 */
export function createChildPathMapping(
  currentModuleCid: string,
  entityDefinition: any
): Map<string, { moduleCid: string; offset: number }> {
  const mapping = new Map();

  // Process child_entities
  const childEntities = entityDefinition.child_entities || [];
  for (const childInfo of childEntities) {
    const ipldPath = childInfo.ipld_path;
    const entityKey = childInfo.entity_key;

    if (ipldPath && entityKey) {
      const pathKey = canonicalKeyStrForIPLDPath(ipldPath);

      if (entityKey.tag === 'local') {
        // Local entity key - use current module CID
        mapping.set(pathKey, {
          moduleCid: currentModuleCid,
          offset: entityKey.definition_offset
        });
      } else if (entityKey.tag === 'import') {
        // Imported entity key - get module CID from the import
        const importModuleCid = (entityKey.module as CidString).base32_cid_str;
        mapping.set(pathKey, {
          moduleCid: importModuleCid,
          offset: entityKey.definition_offset
        });
      }
    }
  }

  return mapping;
}
