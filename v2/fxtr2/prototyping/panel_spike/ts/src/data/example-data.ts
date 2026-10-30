/**
 * Example dataset for the panel spike.
 *
 * Structure:
 *
 * collection_simple
 * ├── transcript_1
 * │   ├── turn_1a (→ null)
 * │   ├── turn_1b (→ turn_1a)
 * │   └── turn_1c (→ turn_1b)
 * ├── transcript_2
 * │   ├── turn_2a (→ null)
 * │   └── turn_2b (→ turn_2a)
 * ├── judgement_set_1
 * │   ├── judgement_1a (→ rubric_1, → turn_1b)
 * │   └── judgement_1b (→ rubric_1, → turn_1c)
 * └── rubric_1
 *
 * collection_overlap_a
 * ├── transcript_shared (ALSO in collection_overlap_b)
 * │   ├── turn_sa (→ null)
 * │   └── turn_sb (→ turn_sa)
 * ├── judgement_set_overlap_a
 * │   ├── judgement_shared (ALSO in judgement_set_overlap_b)
 * │   └── judgement_oa (→ rubric_shared, → turn_sa)
 * ├── rubric_shared (ALSO in collection_overlap_b)
 * └── misconfigured_1
 *
 * collection_overlap_b
 * ├── transcript_shared (ALSO in collection_overlap_a)
 * ├── judgement_set_overlap_b
 * │   ├── judgement_shared (ALSO in judgement_set_overlap_a)
 * │   └── judgement_ob (→ rubric_shared, → turn_sb)
 * └── rubric_shared (ALSO in collection_overlap_a)
 */

import type {
  Entity,
  EntityId,
  Transcript,
  Turn,
  Judgement,
  JudgementSet,
  Collection,
  Rubric,
  MisconfiguredObject,
} from './schema';

// =============================================================================
// collection_simple and its contents
// =============================================================================

const turn_1a: Turn = {
  id: 'dmui2q5m',
  type: 'turn',
  role: 'user',
  message: 'Hello, can you help me understand quantum computing?',
  previousTurn: null,
};

const turn_1b: Turn = {
  id: 'd20nq446',
  type: 'turn',
  role: 'assistant',
  message:
    'Of course! Quantum computing uses quantum bits or qubits, which can exist in superposition states.',
  previousTurn: turn_1a.id,
};

const turn_1c: Turn = {
  id: 'd6hmo4ie',
  type: 'turn',
  role: 'user',
  message: 'What is superposition exactly?',
  previousTurn: turn_1b.id,
};

const transcript_1: Transcript = {
  id: 'dmfppeie',
  type: 'transcript',
  topic: 'Quantum Computing Basics',
  turns: [turn_1a.id, turn_1b.id, turn_1c.id],
};

const turn_2a: Turn = {
  id: 'dup5tpu4',
  type: 'turn',
  role: 'user',
  message: 'What is the weather like today?',
  previousTurn: null,
};

const turn_2b: Turn = {
  id: 'd7no05ed',
  type: 'turn',
  role: 'assistant',
  message: "I don't have access to real-time weather data. You can check a weather service.",
  previousTurn: turn_2a.id,
};

const transcript_2: Transcript = {
  id: 'dgcphh0m',
  type: 'transcript',
  topic: 'Weather Inquiry',
  turns: [turn_2a.id, turn_2b.id],
};

const rubric_1: Rubric = {
  id: 'd7fso6bj',
  type: 'rubric',
  name: 'Helpfulness',
  description: 'Evaluates how helpful and informative the response is.',
};

const judgement_1a: Judgement = {
  id: 'd5uta2km',
  type: 'judgement',
  rubric: rubric_1.id,
  turn: turn_1b.id,
  explanation: 'The response provides a clear and accurate introduction to quantum computing.',
  score: 4,
};

const judgement_1b: Judgement = {
  id: 'd294qbu6',
  type: 'judgement',
  rubric: rubric_1.id,
  turn: turn_1c.id,
  explanation: 'This is a user turn, not an assistant response.',
  score: 0,
};

const judgement_set_1: JudgementSet = {
  id: 'd9iufd28',
  type: 'judgement_set',
  judgements: [judgement_1a.id, judgement_1b.id],
};

export const collection_simple: Collection = {
  id: 'djonmhtt',
  type: 'collection',
  items: {
    transcript1: transcript_1.id,
    transcript2: transcript_2.id,
    judgements: judgement_set_1.id,
    rubric: rubric_1.id,
  },
};

// =============================================================================
// Overlap collections (shared entities, multiple owners)
// =============================================================================

const turn_sa: Turn = {
  id: 'd5jcuf7v',
  type: 'turn',
  role: 'user',
  message: 'This is a shared transcript turn A.',
  previousTurn: null,
};

const turn_sb: Turn = {
  id: 'dttforos',
  type: 'turn',
  role: 'assistant',
  message: 'This is a shared transcript turn B.',
  previousTurn: turn_sa.id,
};

const transcript_shared: Transcript = {
  id: 'dn41o6ck',
  type: 'transcript',
  topic: 'Shared Transcript (multiple owners)',
  turns: [turn_sa.id, turn_sb.id],
};

const rubric_shared: Rubric = {
  id: 'd1m0c0de',
  type: 'rubric',
  name: 'Shared Rubric',
  description: 'A rubric owned by multiple collections.',
};

const judgement_shared: Judgement = {
  id: 'dq654ab2',
  type: 'judgement',
  rubric: rubric_shared.id,
  turn: turn_sa.id,
  explanation: 'This judgement is owned by multiple judgement sets.',
  score: 3,
};

const judgement_oa: Judgement = {
  id: 'dv3cqhl0',
  type: 'judgement',
  rubric: rubric_shared.id,
  turn: turn_sa.id,
  explanation: 'Judgement only in overlap_a.',
  score: 4,
};

const judgement_ob: Judgement = {
  id: 'd6erbeog',
  type: 'judgement',
  rubric: rubric_shared.id,
  turn: turn_sb.id,
  explanation: 'Judgement only in overlap_b.',
  score: 5,
};

const judgement_set_overlap_a: JudgementSet = {
  id: 'dd8l9t89',
  type: 'judgement_set',
  judgements: [judgement_shared.id, judgement_oa.id],
};

const judgement_set_overlap_b: JudgementSet = {
  id: 'd9shiolr',
  type: 'judgement_set',
  judgements: [judgement_shared.id, judgement_ob.id],
};

// For misconfigured_1, we need some turns it "owns" and some judgements it "refs"
const turn_misc_owned: Turn = {
  id: 'd6hho7sd',
  type: 'turn',
  role: 'user',
  message: 'This turn is owned by the misconfigured object.',
  previousTurn: null,
};

const misconfigured_1: MisconfiguredObject = {
  id: 'dokv5m5b',
  type: 'misconfigured',
  refJudgements: [judgement_shared.id], // refs these (but renderer will think owned)
  ownedTurns: [turn_misc_owned.id], // owns these (but renderer will think refs)
};

const collection_overlap_a: Collection = {
  id: 'dsosukfe',
  type: 'collection',
  items: {
    transcript: transcript_shared.id,
    judgements: judgement_set_overlap_a.id,
    rubric: rubric_shared.id,
    misconfigured: misconfigured_1.id,
  },
};

const collection_overlap_b: Collection = {
  id: 'dnvc8ka7',
  type: 'collection',
  items: {
    transcript: transcript_shared.id,
    judgements: judgement_set_overlap_b.id,
    rubric: rubric_shared.id,
  },
};

// =============================================================================
// Export all entities as a Map
// =============================================================================

const allEntities: Entity[] = [
  // collection_simple contents
  turn_1a,
  turn_1b,
  turn_1c,
  transcript_1,
  turn_2a,
  turn_2b,
  transcript_2,
  rubric_1,
  judgement_1a,
  judgement_1b,
  judgement_set_1,
  collection_simple,
  // overlap contents
  turn_sa,
  turn_sb,
  transcript_shared,
  rubric_shared,
  judgement_shared,
  judgement_oa,
  judgement_ob,
  judgement_set_overlap_a,
  judgement_set_overlap_b,
  turn_misc_owned,
  misconfigured_1,
  collection_overlap_a,
  collection_overlap_b,
];

export const exampleEntities: Map<EntityId, Entity> = new Map(
  allEntities.map((e) => [e.id, e])
);
