/**
 * Entity type definitions for the panel spike.
 *
 * Each entity has a unique ID and a type. The type determines the schema
 * of the data and how ownership/references work.
 */

export type EntityId = string;

export type EntityType =
  | 'transcript'
  | 'turn'
  | 'judgement'
  | 'judgement_set'
  | 'collection'
  | 'rubric'
  | 'misconfigured';

export interface BaseEntity {
  id: EntityId;
  type: EntityType;
}

export interface Transcript extends BaseEntity {
  type: 'transcript';
  topic: string;
  turns: EntityId[]; // owned
}

export interface Turn extends BaseEntity {
  type: 'turn';
  role: 'user' | 'assistant';
  message: string;
  previousTurn: EntityId | null; // ref
}

export interface Judgement extends BaseEntity {
  type: 'judgement';
  rubric: EntityId; // ref
  turn: EntityId; // ref
  explanation: string;
  score: number;
}

export interface JudgementSet extends BaseEntity {
  type: 'judgement_set';
  judgements: EntityId[]; // owned
}

export interface Collection extends BaseEntity {
  type: 'collection';
  items: Record<string, EntityId>; // owned
}

export interface Rubric extends BaseEntity {
  type: 'rubric';
  name: string;
  description: string;
}

/**
 * NOTE: This type is intentionally misconfigured for testing edge cases.
 * The renderer will be set up to treat ownedTurns as refs and
 * refJudgements as owned, to test the mismatch detection.
 */
export interface MisconfiguredObject extends BaseEntity {
  type: 'misconfigured';
  refJudgements: EntityId[]; // actually refs, but renderer will think owned
  ownedTurns: EntityId[]; // actually owned, but renderer will think refs
}

export type Entity =
  | Transcript
  | Turn
  | Judgement
  | JudgementSet
  | Collection
  | Rubric
  | MisconfiguredObject;

/**
 * Ownership declarations for each entity type.
 * - owned: fields containing IDs of entities this entity owns
 * - refs: fields containing IDs of entities this entity references (but doesn't own)
 */
export const ownershipDeclarations: Record<
  EntityType,
  { owned: string[]; refs: string[] }
> = {
  transcript: { owned: ['turns'], refs: [] },
  turn: { owned: [], refs: ['previousTurn'] },
  judgement: { owned: [], refs: ['rubric', 'turn'] },
  judgement_set: { owned: ['judgements'], refs: [] },
  collection: { owned: ['items'], refs: [] },
  rubric: { owned: [], refs: [] },
  misconfigured: { owned: ['ownedTurns'], refs: ['refJudgements'] },
};
