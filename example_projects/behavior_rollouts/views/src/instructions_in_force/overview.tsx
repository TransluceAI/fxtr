// The experiment's results overview: for each model and judge, how often a violation was found and
// how often a judge's samples disagreed; then every conversation, a row per sequence and a column
// per model, with each judge's verdict in each sample. A verdict opens its judgment, and a cell
// opens its conversation.

import styles from "../rollouts.module.css";
import {
  EntityDataError, isDataMap, isEntityID, rowsOf, unwrap, useEntity, useEntityLinks, useInvocation,
  type ArrayData, type DataValue, type EntityID, type InvocationPanelProps, type Spawn,
} from "fxtr-view";
import { Stat, Stats, formatPercent } from "fxtr-view-kit";
import { WithReport, type ReportData } from "./report";

/** One judgment's verdict, where it sits. */
interface Verdict {
  readonly model: string;
  readonly sequence: string;
  readonly judge: string;
  readonly sample: number;
  /** Whether the judge found a violation; null when it gave no accepted answer. */
  readonly violation: boolean | null;
  /** How many answers the judge gave, the accepted one included. */
  readonly answers: number;
  readonly judgment: EntityID | null;
}

const DIMS = ["model", "sequence", "judge", "sample"] as const;

function keyed(array: ArrayData): { key: Record<string, DataValue>; value: DataValue }[] {
  const names = array.dims.map(([name]) => name);
  return rowsOf(array).map((row) => ({ key: Object.fromEntries(names.map((name, i) => [name, row.key[i] ?? null])), value: row.value }));
}

const cellKey = (...parts: readonly (string | number)[]) => JSON.stringify(parts);

/** The verdicts array's rows, each with the judgment it was read from. */
export function readVerdicts(verdicts: ArrayData, judgments: ArrayData): Verdict[] {
  if (DIMS.some((dim, i) => verdicts.dims[i]?.[0] !== dim)) {
    throw new EntityDataError(`Expected verdicts over [${DIMS.join(", ")}]`);
  }
  const judgmentIds = new Map(keyed(judgments).map(({ key, value }) => [
    cellKey(...DIMS.map((dim) => String(key[dim]))), isEntityID(value) ? value : null,
  ]));
  return keyed(verdicts).map(({ key, value }) => {
    if (!isDataMap(value)) throw new EntityDataError("Expected a verdict record");
    const violation = value["violation"];
    const [model, sequence, judge, sample] = DIMS.map((dim) => key[dim]);
    return {
      model: String(model), sequence: String(sequence), judge: String(judge), sample: Number(sample),
      violation: typeof violation === "boolean" ? violation : null,
      answers: Number(value["answers"] ?? 1),
      judgment: judgmentIds.get(cellKey(String(model), String(sequence), String(judge), String(sample))) ?? null,
    };
  });
}

/** The distinct values of a field, in the order they first appear. */
function distinct(verdicts: readonly Verdict[], field: "model" | "sequence" | "judge"): string[] {
  return [...new Set(verdicts.map((verdict) => verdict[field]))];
}

/** Whether a judge's samples of one conversation did not all reach the same verdict. */
function split(samples: readonly Verdict[]): boolean {
  return new Set(samples.map((verdict) => verdict.violation)).size > 1;
}

/** For one model and judge: the share of judgments that found a violation, and the conversations
 *  the samples split on. */
function Rate({ verdicts }: { verdicts: readonly Verdict[] }) {
  const answered = verdicts.filter((verdict) => verdict.violation !== null);
  const found = answered.filter((verdict) => verdict.violation).length;
  const bySequence = new Map<string, Verdict[]>();
  for (const verdict of verdicts) bySequence.set(verdict.sequence, [...(bySequence.get(verdict.sequence) ?? []), verdict]);
  const splits = [...bySequence.values()].filter(split).length;
  const unanswered = verdicts.length - answered.length;
  return (
    <>
      <div className={styles["rate"]}>{answered.length === 0 ? "—" : formatPercent(found / answered.length)}</div>
      <div className={styles["muted"]}>
        {found} of {answered.length} judgments
        {unanswered > 0 && `, ${unanswered} unanswered`}
        {splits > 0 && ` · samples split on ${splits} of ${bySequence.size}`}
      </div>
    </>
  );
}

const MARK: Record<string, string> = { true: "violation", false: "no violation", null: "no accepted answer" };

/** One judgment's verdict as a mark that opens the judgment. */
function VerdictMark({ verdict }: { verdict: Verdict }) {
  const links = useEntityLinks();
  const said = MARK[String(verdict.violation)]!;
  const corrected = verdict.answers > 1 ? `, after ${verdict.answers} answers` : "";
  const label = `${verdict.judge}, sample ${verdict.sample}: ${said}${corrected}`;
  const open = verdict.judgment !== null && links.isOpen(verdict.judgment);
  return (
    <button type="button" className={styles["verdict"]} data-verdict={String(verdict.violation)} data-open={open}
      aria-label={label} title={label} disabled={verdict.judgment === null}
      onClick={() => verdict.judgment !== null && links.open(verdict.judgment)}>
      {verdict.violation === null ? "?" : verdict.violation ? "✕" : "✓"}
      {verdict.answers > 1 && <sup>{verdict.answers}</sup>}
    </button>
  );
}

/** One conversation: a link to it, and each judge's verdicts in each sample. */
function Conversation({ rollout, verdicts, judges }: { rollout: EntityID | null; verdicts: readonly Verdict[]; judges: readonly string[] }) {
  const links = useEntityLinks();
  return (
    <td className={styles["conversation"]}>
      {rollout !== null && (
        <button type="button" className={styles["open"]} data-open={links.isOpen(rollout)} onClick={() => links.open(rollout)}>
          conversation
        </button>
      )}
      {judges.map((judge) => {
        const samples = verdicts.filter((verdict) => verdict.judge === judge).sort((a, b) => a.sample - b.sample);
        return (
          <div key={judge} className={styles["verdicts"]}>
            <span className={styles["muted"]}>{judge}{split(samples) && " · split"}</span>
            <span>{samples.map((verdict) => <VerdictMark key={verdict.sample} verdict={verdict} />)}</span>
          </div>
        );
      })}
    </td>
  );
}

function Results({ report }: { report: ReportData }) {
  const verdicts = readVerdicts(unwrap(useEntity(report.verdicts)), unwrap(useEntity(report.judgments)));
  const rollouts = new Map(keyed(unwrap(useEntity(report.rollouts))).map(({ key, value }) => [
    cellKey(String(key["model"]), String(key["sequence"])), isEntityID(value) ? value : null,
  ]));
  const models = distinct(verdicts, "model");
  const sequences = distinct(verdicts, "sequence");
  const judges = distinct(verdicts, "judge");
  const samples = new Set(verdicts.map((verdict) => verdict.sample)).size;
  if (verdicts.length === 0) return <p className={styles["muted"]}>No conversation was judged.</p>;
  const of = (model: string, sequence?: string) => verdicts.filter((verdict) =>
    verdict.model === model && (sequence === undefined || verdict.sequence === sequence));
  return (
    <>
      <Stats>
        <Stat label="conversations" value={rollouts.size} note={`${models.length} models × ${sequences.length} sequences`} />
        <Stat label="judgments" value={verdicts.length} note={`${judges.length} judges × ${samples} samples each`} />
        <Stat label="corrected" value={verdicts.filter((verdict) => verdict.answers > 1).length}
          note="judgments whose first answer was sent back" />
      </Stats>
      <p className={styles["muted"]}>A judgment's verdict is whether the assistant violated an instruction in force.</p>
      <h3 className={styles["subhead"]}>Violations found</h3>
      <table className={styles["results"]}>
        <thead>
          <tr><th>model</th>{judges.map((judge) => <th key={judge}>judged by {judge}</th>)}</tr>
        </thead>
        <tbody>
          {models.map((model) => (
            <tr key={model}>
              <th scope="row">{model}</th>
              {judges.map((judge) => <td key={judge}><Rate verdicts={of(model).filter((verdict) => verdict.judge === judge)} /></td>)}
            </tr>
          ))}
        </tbody>
      </table>
      <h3 className={styles["subhead"]}>Conversations</h3>
      <p className={styles["muted"]}>✕ a violation, ✓ none, ? no accepted answer; a raised number counts the answers a judge gave. Each mark opens its judgment.</p>
      <table className={styles["results"]}>
        <thead>
          <tr><th>sequence</th>{models.map((model) => <th key={model}>{model}</th>)}</tr>
        </thead>
        <tbody>
          {sequences.map((sequence) => (
            <tr key={sequence}>
              <th scope="row">{sequence}</th>
              {models.map((model) => (
                <Conversation key={model} rollout={rollouts.get(cellKey(model, sequence)) ?? null}
                  verdicts={of(model, sequence)} judges={judges} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Overview({ run }: { run: Spawn }) {
  const invocation = unwrap(useInvocation(run.ref));
  return (
    <section className={styles["invocation"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>workflow</span>
        <span className={styles["muted"]}>{invocation.state}</span>
        <h2>Following instructions: results</h2>
      </header>
      {invocation.error && <p className={styles["error"]}>{invocation.error.typeName}: {invocation.error.message}</p>}
      {invocation.output
        ? <WithReport output={invocation.output} otherwise={() => <p className={styles["muted"]}>This run returned no report.</p>}>
            {(report) => <Results report={report} />}
          </WithReport>
        : <p className={styles["muted"]}>
            The results are shown once every conversation has been judged · {invocation.state}. The judge step's
            panel fills in as judgments finish.
          </p>}
    </section>
  );
}

/** The experiment workflow's results. It is the root workflow, so it is handed its one run. */
export function InstructionsInForceOverviewPanel({ invocations }: InvocationPanelProps) {
  const run = invocations[0];
  return run ? <Overview run={run} /> : null;
}
