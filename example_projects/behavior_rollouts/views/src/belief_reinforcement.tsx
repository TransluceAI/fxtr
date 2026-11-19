import {
  EntityDataError, EntityLink, isDataMap, isEntityID, rowsOf, unwrap, useEntity, useEntityLinks,
  type ArrayData, type EntityData, type EntityID, type EntityPanelProps, type EntityViewProps,
  type InvocationRecord, type StorageAtom,
} from "fxtr-view";
import { DataTable, InvocationOutcome, Stat, Stats, eachInvocation, useStoredState, type TableColumn } from "fxtr-view-kit";
import { ChatRolloutView, type ChatRolloutData } from "fxtr-view-behaviors";
import styles from "./rollouts.module.css";

export const BELIEF_REINFORCEMENT_REPORT = "org.transluce.examples.behavior_rollouts.BeliefReinforcementReport.v1";

interface BeliefReinforcementReportData extends EntityData {
  readonly cases: EntityID<ArrayData>;
  readonly rollouts: EntityID<ArrayData>;
  readonly transcripts: EntityID<ArrayData>;
  readonly judgments: EntityID<ArrayData>;
  readonly verdicts: EntityID<ArrayData>;
}

type Metric = "reinforcement" | "emotional_responsiveness" | "overall";
const METRICS: Record<Metric, string> = {
  reinforcement: "Unwarranted reinforcement",
  emotional_responsiveness: "Emotional responsiveness",
  overall: "Overall quality",
};

interface Verdict {
  readonly judge: string;
  readonly sample: number;
  readonly reinforcement: boolean | null;
  readonly emotional_responsiveness: string | null;
  readonly overall: string | null;
  readonly judgment: EntityID | null;
}

interface Conversation {
  readonly key: string;
  readonly model: string;
  readonly case: string;
  readonly situation: string;
  readonly evidence: string;
  readonly rollout: EntityID | null;
  readonly verdicts: Verdict[];
}

const keyOf = (key: readonly unknown[]) => JSON.stringify(key);

/** Join report arrays by their dimension keys, retaining every sample and missing answer. */
export function readBeliefReinforcementRows(cases: ArrayData, rollouts: ArrayData, judgments: ArrayData, verdicts: ArrayData): Conversation[] {
  const metadata = new Map(rowsOf(cases).map((row) => [String(row.key[0]), row.value]));
  const judgmentIds = new Map(rowsOf(judgments).map((row) => [keyOf(row.key), row.value]));
  const conversations = new Map<string, Conversation>();
  for (const row of rowsOf(rollouts)) {
    const caseKey = String(row.key[1]);
    const info = metadata.get(caseKey);
    if (!isDataMap(info)) throw new EntityDataError(`Missing case metadata for ${caseKey}`);
    const key = keyOf(row.key);
    conversations.set(key, {
      key, model: String(row.key[0]), case: caseKey, situation: String(info["situation"]),
      evidence: String(info["evidence"]), rollout: isEntityID(row.value) ? row.value : null, verdicts: [],
    });
  }
  for (const row of rowsOf(verdicts)) {
    const value = row.value;
    if (!isDataMap(value)) throw new EntityDataError("Expected a belief-reinforcement verdict");
    const judgment = judgmentIds.get(keyOf(row.key));
    const conversation = conversations.get(keyOf(row.key.slice(0, 2)));
    if (!conversation) throw new EntityDataError("Verdict has no matching conversation");
    conversation.verdicts.push({
      judge: String(row.key[2]), sample: Number(row.key[3]),
      reinforcement: typeof value["reinforcement"] === "boolean" ? value["reinforcement"] : null,
      emotional_responsiveness: typeof value["emotional_responsiveness"] === "string" ? value["emotional_responsiveness"] : null,
      overall: typeof value["overall"] === "string" ? value["overall"] : null,
      judgment: isEntityID(judgment) ? judgment : null,
    });
  }
  const evidenceOrder = ["weak", "mixed", "strong"];
  return [...conversations.values()].sort((a, b) =>
    a.situation.localeCompare(b.situation)
    || evidenceOrder.indexOf(a.evidence) - evidenceOrder.indexOf(b.evidence)
    || a.model.localeCompare(b.model));
}

export function verdictsDisagree(verdicts: readonly Verdict[], metric: Metric): boolean {
  return new Set(verdicts.map((verdict) => verdict[metric]).filter((value) => value !== null)).size > 1;
}

function verdictLabel(verdict: Verdict, metric: Metric): string {
  const value = verdict[metric];
  if (value === null) return "missing";
  if (typeof value === "boolean") return value ? "reinforced" : "calibrated";
  return value;
}

function Samples({ verdicts, metric }: { verdicts: readonly Verdict[]; metric: Metric }) {
  const links = useEntityLinks();
  return <div className={styles["judge-samples"]}>
    {[...verdicts].sort((a, b) => a.sample - b.sample).map((verdict) => {
      const id = verdict.judgment;
      return <button key={verdict.sample} type="button" className={styles["verdict"]}
        disabled={id === null} data-open={id !== null && links.isOpen(id)}
        aria-label={`${verdict.judge}, sample ${verdict.sample + 1}, ${verdictLabel(verdict, metric)}`}
        onClick={() => id !== null && links.open(id)}>
        {verdict.sample + 1} · {verdictLabel(verdict, metric)}
      </button>;
    })}
  </div>;
}

function BeliefReinforcementResults({ report, storage }: { report: BeliefReinforcementReportData; storage: StorageAtom }) {
  const rows = readBeliefReinforcementRows(unwrap(useEntity(report.cases)), unwrap(useEntity(report.rollouts)),
    unwrap(useEntity(report.judgments)), unwrap(useEntity(report.verdicts)));
  const [metric, setMetric] = useStoredState<Metric>(storage, "metric", "reinforcement",
    (value): value is Metric => value === "reinforcement" || value === "emotional_responsiveness" || value === "overall");
  const [onlyDisagreements, setOnlyDisagreements] = useStoredState<boolean>(storage, "disagreements", false,
    (value): value is boolean => typeof value === "boolean");
  const judges = [...new Set(rows.flatMap((row) => row.verdicts.map((verdict) => verdict.judge)))];
  const disagreements = rows.filter((row) => verdictsDisagree(row.verdicts, metric));
  const verdicts = rows.flatMap((row) => row.verdicts);
  const columns: TableColumn<Conversation>[] = [
    { key: "situation", label: "Situation", value: (row) => row.situation },
    { key: "evidence", label: "Evidence", value: (row) => row.evidence },
    { key: "model", label: "Subject", value: (row) => row.model },
    ...judges.map((judge): TableColumn<Conversation> => ({
      key: judge, label: judge,
      value: (row) => row.verdicts.filter((v) => v.judge === judge).map((v) => verdictLabel(v, metric)).join(", "),
      cell: (row) => <Samples verdicts={row.verdicts.filter((v) => v.judge === judge)} metric={metric} />,
    })),
    { key: "conversation", label: "Conversation", cell: (row) => row.rollout && <EntityLink id={row.rollout} /> },
  ];
  return <>
    <Stats>
      <Stat label="conversations" value={rows.length} />
      <Stat label="judgments" value={verdicts.length} />
      <Stat label="disagreements" value={disagreements.length} note="conversations with differing judges or samples on this metric" />
      <Stat label="missing answers" value={verdicts.filter((v) => v.reinforcement === null).length} />
    </Stats>
    <div className={styles["toolbar"]}>
      <label>Judgment{" "}<select value={metric} onChange={(event) => setMetric(event.target.value as Metric)}>
        {Object.entries(METRICS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <label><input type="checkbox" checked={onlyDisagreements} onChange={(event) => setOnlyDisagreements(event.target.checked)} /> Show disagreements only</label>
    </div>
    <p className={styles["muted"]}>Each numbered verdict opens its judgment and citations. Evidence conditions describe the authored cases.</p>
    <DataTable rows={onlyDisagreements ? disagreements : rows} columns={columns} rowKey={(row) => row.key} />
  </>;
}

export function BeliefReinforcementReportPanel({ id, storage }: EntityPanelProps) {
  const report = unwrap(useEntity(id as EntityID<BeliefReinforcementReportData>));
  return <section className={styles["panel"]}>
    <h2 className={styles["subhead"]}>Emotional support and belief reinforcement</h2>
    <BeliefReinforcementResults report={report} storage={storage} />
  </section>;
}

export function BeliefReinforcementReportInline() {
  return <>Belief-reinforcement results</>;
}

function OutputReport({ output, storage }: { output: EntityID<ArrayData>; storage: StorageAtom }) {
  const value = rowsOf(unwrap(useEntity(output)))[0]?.value;
  if (!isEntityID(value)) throw new EntityDataError("Expected a belief-reinforcement report");
  return <BeliefReinforcementReportPanel id={value} storage={storage} />;
}

export const BeliefReinforcementOverview = eachInvocation(({ invocation, storage }: { invocation: InvocationRecord; storage: StorageAtom }) =>
  invocation.output ? <OutputReport output={invocation.output} storage={storage} /> : <InvocationOutcome invocation={invocation} />);

function ConversationOutput({ output }: { output: EntityID<ArrayData> }) {
  const value = rowsOf(unwrap(useEntity(output)))[0]?.value;
  if (!isEntityID(value)) throw new EntityDataError("Expected a conversation");
  return <ConversationEntity id={value} />;
}

function ConversationEntity({ id }: EntityViewProps) {
  return <><ChatRolloutView data={unwrap(useEntity(id as EntityID<ChatRolloutData>))} /><EntityLink id={id} /></>;
}

export const BeliefReinforcementConversations = eachInvocation(({ invocation }: { invocation: InvocationRecord }) =>
  invocation.output ? <ConversationOutput output={invocation.output} /> : <InvocationOutcome invocation={invocation} />);
