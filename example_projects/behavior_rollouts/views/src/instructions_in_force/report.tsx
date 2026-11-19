// The instructions-in-force experiment's report (`InstructionsInForceReport` in
// instructions_in_force.py): what the workflow
// returns, referring to every stage's array. The workflow's panels read their arrays through it.

import type { ReactNode } from "react";
import {
  EntityDataError, isEntityID, rowsOf, unwrap, useEntity,
  type ArrayData, type EntityData, type EntityID,
} from "fxtr-view";

export const INSTRUCTIONS_IN_FORCE_REPORT = "org.transluce.examples.behavior_rollouts.InstructionsInForceReport.v1";

export interface ReportData extends EntityData {
  readonly $type: typeof INSTRUCTIONS_IN_FORCE_REPORT;
  /** `[model, sequence]`: each conversation, a `ChatRollout`. */
  readonly rollouts: EntityID<ArrayData>;
  /** `[model, sequence]`: each conversation as the judges read it, a `Transcript`. */
  readonly transcripts: EntityID<ArrayData>;
  /** `[model, sequence, judge, sample]`: every `Judgment`. */
  readonly judgments: EntityID<ArrayData>;
  /** `[model, sequence, judge, sample]`: each judgment's `{ violation, answers }`. */
  readonly verdicts: EntityID<ArrayData>;
}

/** The report an output array holds, or null when it holds something else (a conversation step's
 *  rollout). */
export function reportIdOf(array: ArrayData): EntityID<ReportData> | null {
  const schema = array.schema;
  if (array.dims.length !== 0 || typeof schema === "string" || !("id" in schema) || schema.id !== INSTRUCTIONS_IN_FORCE_REPORT) return null;
  const value = rowsOf(array)[0]?.value;
  return isEntityID(value) ? (value as EntityID<ReportData>) : null;
}

function LoadedReport({ id, children }: { id: EntityID<ReportData>; children: (report: ReportData) => ReactNode }) {
  const report = unwrap(useEntity(id));
  if (report.$type !== INSTRUCTIONS_IN_FORCE_REPORT) throw new EntityDataError(`Expected the experiment's report, got ${report.$type}`);
  return <>{children(report)}</>;
}

/** An invocation's output read through the report it holds, or `otherwise` for any other output. */
export function WithReport({ output, children, otherwise }: {
  output: EntityID<ArrayData>;
  children: (report: ReportData) => ReactNode;
  otherwise: (output: EntityID<ArrayData>) => ReactNode;
}) {
  const id = reportIdOf(unwrap(useEntity(output)));
  return id === null ? <>{otherwise(output)}</> : <LoadedReport id={id}>{children}</LoadedReport>;
}
