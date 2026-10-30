/**
 * Ownership graph computation and helper functions.
 */

import type { Entity, EntityId, EntityType } from './schema';
import { ownershipDeclarations } from './schema';

/**
 * Computed ownership relationships between entities.
 */
export interface OwnershipInfo {
  /** For each entity, which entities own it */
  owners: Map<EntityId, EntityId[]>;
  /** For each entity, which entities reference it (without owning) */
  referencedBy: Map<EntityId, EntityId[]>;
  /** For each entity, which entities it owns */
  owns: Map<EntityId, EntityId[]>;
  /** For each entity, which entities it references (without owning) */
  refs: Map<EntityId, EntityId[]>;
}

/**
 * Bundle of entities and their ownership info, passed as props.
 */
export interface DataBundle {
  entities: Map<EntityId, Entity>;
  ownership: OwnershipInfo;
}

/**
 * Extract entity IDs from a field value.
 * Handles: single ID, array of IDs, Record<string, ID>.
 */
function extractIds(value: unknown): EntityId[] {
  if (value === null || value === undefined) {
    return [];
  }
  if (typeof value === 'string') {
    return [value];
  }
  if (Array.isArray(value)) {
    return value.filter((v): v is string => typeof v === 'string');
  }
  if (typeof value === 'object') {
    // Record<string, EntityId>
    return Object.values(value).filter((v): v is string => typeof v === 'string');
  }
  return [];
}

/**
 * Compute ownership graph from entities.
 */
export function computeOwnership(entities: Map<EntityId, Entity>): OwnershipInfo {
  const owners = new Map<EntityId, EntityId[]>();
  const referencedBy = new Map<EntityId, EntityId[]>();
  const owns = new Map<EntityId, EntityId[]>();
  const refs = new Map<EntityId, EntityId[]>();

  // Initialize empty arrays for all entities
  for (const id of entities.keys()) {
    owners.set(id, []);
    referencedBy.set(id, []);
    owns.set(id, []);
    refs.set(id, []);
  }

  // Process each entity
  for (const [entityId, entity] of entities) {
    const declarations = ownershipDeclarations[entity.type as EntityType];
    if (!declarations) continue;

    // Process owned fields
    for (const field of declarations.owned) {
      const value = (entity as unknown as Record<string, unknown>)[field];
      const ownedIds = extractIds(value);

      for (const ownedId of ownedIds) {
        // Add to this entity's "owns" list
        owns.get(entityId)?.push(ownedId);
        // Add this entity to the owned entity's "owners" list
        if (owners.has(ownedId)) {
          owners.get(ownedId)?.push(entityId);
        }
      }
    }

    // Process ref fields
    for (const field of declarations.refs) {
      const value = (entity as unknown as Record<string, unknown>)[field];
      const refIds = extractIds(value);

      for (const refId of refIds) {
        // Add to this entity's "refs" list
        refs.get(entityId)?.push(refId);
        // Add this entity to the referenced entity's "referencedBy" list
        if (referencedBy.has(refId)) {
          referencedBy.get(refId)?.push(entityId);
        }
      }
    }
  }

  return { owners, referencedBy, owns, refs };
}

/**
 * Create a DataBundle from entities.
 */
export function createDataBundle(entities: Map<EntityId, Entity>): DataBundle {
  return {
    entities,
    ownership: computeOwnership(entities),
  };
}

// =============================================================================
// Helper functions that operate on DataBundle
// =============================================================================

/**
 * Get entity data by ID.
 */
export function getData(bundle: DataBundle, id: EntityId): Entity | undefined {
  return bundle.entities.get(id);
}

/**
 * Get entities that own the given entity.
 */
export function getOwners(bundle: DataBundle, id: EntityId): EntityId[] {
  return bundle.ownership.owners.get(id) ?? [];
}

/**
 * Get entities that reference (but don't own) the given entity.
 */
export function getReferencedBy(bundle: DataBundle, id: EntityId): EntityId[] {
  return bundle.ownership.referencedBy.get(id) ?? [];
}

/**
 * Get entities owned by the given entity.
 */
export function getOwns(bundle: DataBundle, id: EntityId): EntityId[] {
  return bundle.ownership.owns.get(id) ?? [];
}

/**
 * Get entities referenced (but not owned) by the given entity.
 */
export function getRefs(bundle: DataBundle, id: EntityId): EntityId[] {
  return bundle.ownership.refs.get(id) ?? [];
}

/**
 * Get top-level entities (entities with no owners).
 */
export function getTopLevelEntities(bundle: DataBundle): EntityId[] {
  const result: EntityId[] = [];
  for (const [id, ownerList] of bundle.ownership.owners) {
    if (ownerList.length === 0) {
      result.push(id);
    }
  }
  return result;
}
