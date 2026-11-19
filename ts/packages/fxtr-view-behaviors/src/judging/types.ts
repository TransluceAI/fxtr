import type { DataMap, DataValue, EntityData, EntityID } from "fxtr-view";
import type { MessageData } from "../types";

export const JUDGMENT = "org.transluce.behaviors.Judgment.v1";
export const TEXT_ENTITY = "org.transluce.behaviors.TextEntity.v1";

/** Wire shapes of behaviors/judging (`judge.py`, `citations.py`, `text_entity.py`). */

export interface CitationData extends EntityData {
  readonly entity_id: EntityID;
  readonly address_in_entity: string | null;
  readonly path_to_output_field: readonly (string | number)[];
  /** Where the citation's marker starts and ends in the field's text, counted in Unicode code
   *  points as Python counts them, not in the UTF-16 units a JavaScript string is indexed by
   *  (`indexOfCodePoint` in judgment.tsx converts). */
  readonly start_in_field: number;
  readonly end_in_field: number;
}

export type AttemptResponse =
  | (EntityData & { readonly kind: "model_response"; readonly output: readonly MessageData[] })
  | (EntityData & { readonly kind: "model_call_failure"; readonly category: string; readonly description: string });

export interface InvalidOutputReasonData extends EntityData {
  /** Which part of the output is wrong. Empty when the reason is about the output as a whole. */
  readonly path_in_output: readonly (string | number)[];
  readonly description: string;
}

/** An attempt holds why its answer was not accepted as a list of reasons (empty when it was
 *  accepted, and when the call failed), or, as some stored attempts do, as one text (null then). */
export type AttemptData = EntityData & {
  readonly request: EntityData & { readonly context: readonly MessageData[]; readonly sampling: DataMap };
  readonly response: AttemptResponse;
} & ({ readonly invalid_output_reasons: readonly InvalidOutputReasonData[] } | { readonly invalid_output_reason: string | null });

export interface JudgmentData extends EntityData {
  readonly $type: typeof JUDGMENT;
  readonly entity_ids: { readonly [name: string]: EntityID };
  readonly output: DataMap | null;
  readonly citations: readonly CitationData[];
  readonly attempts: readonly AttemptData[];
}

export interface TextEntityData extends EntityData {
  readonly $type: typeof TEXT_ENTITY;
  readonly text: string;
}

/** One scalar field of a judge's answer, and the values its schema allows (`true/false`, an enum's
 *  values, a number's bounds; null when the schema does not say). `key` names it: its name, or
 *  for a field inside a nested map its path joined with dots (`suicidal_ideation.score`). `path`
 *  is where its value is in the answer, ending in `match` for a nested yes-or-no answer. A field
 *  that points at one place (its whole text one citation) has the address it points at as its
 *  `value`, and the citation as its `citation`: null when the field is empty, and undefined for
 *  any other field. */
export interface AnswerField {
  readonly key: string;
  readonly path: readonly string[];
  readonly value: DataValue;
  readonly range: string | null;
  readonly citation?: CitationData | null;
}

/** Whether `value` is a stored object (a map), as opposed to a scalar or a list. */
function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === "object" && value !== null && !Array.isArray(value) && !(value instanceof Uint8Array);
}

/** Whether one stored attempt has the fields the views read. Its reasons may be a list, or, as some
 *  stored attempts have them, one text. */
function isAttempt(value: unknown): value is AttemptData {
  if (!isRecord(value) || !isRecord(value["request"]) || !isRecord(value["response"])) return false;
  if (!Array.isArray(value["request"]["context"])) return false;
  return Array.isArray(value["invalid_output_reasons"]) || "invalid_output_reason" in value;
}

// What `judgmentOf` read from each stored object, so the same data always gives the same object
// (views compare judgments by identity, as effects' dependencies).
const read = new WeakMap<object, JudgmentData | null>();

/** A stored judgment read as `JudgmentData`, or null when its data is not in a shape these views
 *  read. Every judgment view reads its data through this. Earlier versions of behaviors stored
 *  judgments of other shapes under the same type name, such as an older judging library's; those
 *  come back null, and a view says so instead of failing. The same data gives the same result
 *  each time. */
export function judgmentOf(data: unknown): JudgmentData | null {
  if (!isRecord(data)) return null;
  if (read.has(data)) return read.get(data)!;
  const result = readJudgment(data);
  read.set(data, result);
  return result;
}

function readJudgment(data: Readonly<Record<string, unknown>>): JudgmentData | null {
  if (data["$type"] !== JUDGMENT) return null;
  const output = data["output"];
  if (!isRecord(data["entity_ids"]) || !(output === null || isRecord(output))) return null;
  if (!Array.isArray(data["citations"]) || !Array.isArray(data["attempts"]) || !data["attempts"].every(isAttempt)) return null;
  return data as unknown as JudgmentData;
}

/** The name a judgment gave the entity with this id, or null for an entity it was not given. */
export function entityNameOf(judgment: JudgmentData, id: EntityID): string | null {
  return Object.keys(judgment.entity_ids).find((name) => judgment.entity_ids[name]?.cid === id.cid) ?? null;
}
