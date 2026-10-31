// Invocation panels for the example experiments: for the `rollout` and `grade` steps, and for the
// `refine_bio` workflow, walking its subtree round by round. The reference example of the
// authoring conventions for invocation renderers:
//
// - A panel is handed `invocations`, whatever the host's selection picks out of a run, one or many,
//   and shows every count: one invocation in full (its record loaded with `useInvocation`), several
//   in a view of their own — a `DataTable` row each, linking to that one alone (`RolloutPanel`,
//   `GradePanel`), or `eachInvocation`'s section per spawn (`RefineBioRoundsPanel`).
// - A row links to its invocation with `useLink`, opening the step's own panel (declared in
//   `views.ts`) at the row's keys, beside this one. Where the host opens no views from here,
//   `useLink` yields null and the row instead shows its invocation below the table.
// - The input and output arrays in a record are ids, so a renderer loads what it needs with
//   `unwrap(useEntity(...))` exactly as an entity renderer does, and suspends until then: onto the
//   host's loading fallback for one invocation, and onto a cell's own `Suspense` (`Loading` below)
//   for one row among several, so the other rows show meanwhile. `scalarEntityId` reads the one id
//   out of a scalar input or output array.
// - A link says where its entity sits when it knows: an input as the step's input
//   (`to=".inputs.model"` at the spawn's keys), an output's value at the spawn's keys (`at`).
// - The output is null while the invocation runs or after it fails; a renderer shows the
//   invocation's state and error then (`InvocationOutcome`), not a broken view.
// - Anything deeper comes from `useInvocation(spawn.ref)` on the record's `children`. Never in a
//   loop over the spawns: one component per spawn (`Round` below), each reading its own
//   `LoadResult`, so the page fills in progressively instead of suspending whole on the slowest.
//   An aggregate over many outputs loads them with fxtr-view-kit's `LoadEach` (`GradePanel`).
// - The kind comes first: a panel starts with "step" / "workflow" and the registered name.

import { Suspense, useState, type ReactElement, type ReactNode } from "react";
import {
  EntityDataError,
  EntityLink,
  idKey,
  invocationRefKey,
  isArrayData,
  isDataMap,
  isEntityID,
  scalarEntityId,
  scalarValue,
  unwrap,
  useEntity,
  useInvocation,
  useLink,
  type ArrayData,
  type ChildRun,
  type EntityData,
  type EntityID,
  type InvocationPanelProps,
  type InvocationRecord,
  type Spawn,
  type StorageAtom,
  type ViewDecl,
} from "fxtr-view";
import {
  DataTable,
  excerpt,
  FieldList,
  FirstTurn,
  InvocationHead,
  invocationInput,
  InvocationOutcome,
  layout,
  MessageList,
  LoadEach,
  eachInvocation,
  type SingleInvocationProps,
  type TableColumn,
  type TableRowProps,
} from "fxtr-view-kit";
import { GRADE_STEP, REWRITE_STEP, ROLLOUT_STEP, SIMULATE_STEP } from "./invocation_names";
import type { BioData, SimulatedTranscriptData, TranscriptData } from "./types";
import { GRADES, ROLLOUTS } from "./views";
import styles from "./views.module.css";

/** `children` suspending on their own: a row's data loading without holding up the other rows. */
function Loading({ children, fallback = <span className={layout.muted}>loading…</span> }: { children: ReactNode; fallback?: ReactNode }) {
  return <Suspense fallback={fallback}>{children}</Suspense>;
}

/** The entity a scalar `id` array holds, linked. */
function ScalarLink({ array }: { array: EntityID<ArrayData> }) {
  return <EntityLink id={scalarEntityId(unwrap(useEntity(array)))} />;
}

/** The entity a scalar `id` input holds, linked as that input of the invocation at `keys`. */
function InputLink({ invocation, name, keys }: { invocation: InvocationRecord; name: string; keys: Spawn["key"] }) {
  const id = scalarEntityId(unwrap(useEntity(invocationInput(invocation, name))));
  return <EntityLink id={id} to={`.inputs.${name}`} params={keys} />;
}

/** The dims of the spawns' keys, in the order they first appear: a table column each. */
function keyDims(spawns: readonly Spawn[]): string[] {
  return [...new Set(spawns.flatMap((spawn) => Object.keys(spawn.key)))];
}

function keyColumns<Row>(spawns: readonly Spawn[], spawnOf: (row: Row) => Spawn): TableColumn<Row>[] {
  return keyDims(spawns).map((dim) => ({ key: `key:${dim}`, label: dim, value: (row: Row) => spawnOf(row).key[dim] ?? null }));
}

/** A row among several invocations, opening that one alone: through a link to `view` at its keys,
 *  or, where the host opens no views from here, by picking it to show below the table. */
function SpawnRow({
  spawn,
  view,
  tr,
  picked,
  onPick,
}: {
  spawn: Spawn;
  view: ViewDecl<InvocationPanelProps>;
  tr: (props: TableRowProps, selected?: boolean) => ReactElement;
  picked: boolean;
  onPick: () => void;
}) {
  const link = useLink(view, spawn.key);
  return link ? tr(link.props, link.open) : tr({ onClick: onPick }, picked);
}

/** The spawn picked among `spawns` by its ref's key, if it is still among them. */
function pickedSpawn(spawns: readonly Spawn[], picked: string | null): Spawn | undefined {
  return picked === null ? undefined : spawns.find((spawn) => invocationRefKey(spawn.ref) === picked);
}

// ── rollout (step) ──

/** The transcript a rollout produced, as chat turns. */
function RolloutTranscript({ output, keys }: { output: EntityID<ArrayData>; keys: Spawn["key"] }) {
  const id = scalarEntityId(unwrap(useEntity(output))) as EntityID<TranscriptData>;
  const transcript = unwrap(useEntity(id));
  return (
    <>
      <div className={layout.fieldsLine}>
        <EntityLink id={id} at={{ keys }} />
      </div>
      <MessageList messages={transcript.messages} />
    </>
  );
}

/** The rollout in full: who talked to what, the chat, then its arrays. */
function RolloutOne({ spawn }: { spawn: Spawn }) {
  const invocation = unwrap(useInvocation(spawn.ref));
  return (
    <div className={layout.panel}>
      <div className={layout.card}>
        <InvocationHead kind="step" name={invocation.name}>
          {" · "}
          <InputLink invocation={invocation} name="model" keys={spawn.key} /> on{" "}
          <InputLink invocation={invocation} name="scenario" keys={spawn.key} />
        </InvocationHead>
        {invocation.output ? <RolloutTranscript output={invocation.output} keys={spawn.key} /> : <InvocationOutcome invocation={invocation} />}
      </div>
      <h3 className={layout.subtitle}>arrays</h3>
      <FieldList
        fields={[
          ...Object.entries(invocation.inputs).map(
            ([name, id]) => [name, <EntityLink key={name} id={id} to={`.inputs.${name}`} params={spawn.key} />] as const,
          ),
          ["output", invocation.output ? <EntityLink id={invocation.output} to=".output" params={spawn.key} /> : "none"],
        ]}
      />
    </div>
  );
}

function RolloutFirstTurn({ output, keys }: { output: EntityID<ArrayData>; keys: Spawn["key"] }) {
  const id = scalarEntityId(unwrap(useEntity(output))) as EntityID<TranscriptData>;
  const transcript = unwrap(useEntity(id));
  return (
    <>
      <EntityLink id={id} at={{ keys }} />
      <FirstTurn messages={transcript.messages} />
    </>
  );
}

/** One rollout in full; several as a row each (keys, state, first turn), a row opening that
 *  rollout alone. */
export function RolloutPanel({ invocations }: InvocationPanelProps) {
  const [picked, setPicked] = useState<string | null>(null);
  if (invocations.length === 0) return <p className={layout.empty}>no rollouts</p>;
  if (invocations.length === 1) return <RolloutOne spawn={invocations[0]!} />;
  const columns: TableColumn<Spawn>[] = [
    ...keyColumns(invocations, (spawn: Spawn) => spawn),
    { key: "state", label: "state", cell: (spawn) => <SpawnState spawn={spawn} /> },
    {
      key: "transcript",
      label: "transcript",
      cell: (spawn) =>
        spawn.output ? (
          <Loading>
            <RolloutFirstTurn output={spawn.output} keys={spawn.key} />
          </Loading>
        ) : (
          <span className={layout.muted}>no transcript</span>
        ),
    },
  ];
  const shown = pickedSpawn(invocations, picked);
  return (
    <div className={layout.panel}>
      <InvocationHead kind="step" name={ROLLOUT_STEP}>
        {" · "}
        {invocations.length} rollouts
      </InvocationHead>
      <DataTable
        rows={invocations}
        columns={columns}
        rowKey={(spawn) => invocationRefKey(spawn.ref)}
        renderRow={(spawn, tr) => {
          const key = invocationRefKey(spawn.ref);
          return <SpawnRow spawn={spawn} view={ROLLOUTS} tr={tr} picked={picked === key} onPick={() => setPicked(picked === key ? null : key)} />;
        }}
      />
      {shown && (
        <Loading>
          <RolloutOne spawn={shown} />
        </Loading>
      )}
    </div>
  );
}

// ── grade (step) ──

/** A grade's output struct: the score and the judge's rationale. */
function gradeOf(array: ArrayData): { score: number; rationale: string } {
  const value = scalarValue(array);
  if (!isDataMap(value) || typeof value["score"] !== "number") throw new Error("a grade's output is a struct with a numeric score");
  return { score: value["score"], rationale: String(value["rationale"] ?? "") };
}

/** A grade from its loaded output array. */
function parseGrade(data: EntityData): { score: number; rationale: string } {
  if (!isArrayData(data)) throw new EntityDataError("a grade's output is an array");
  return gradeOf(data);
}

/** One grade: its score and the judge's rationale in full. */
function GradeScore({ output }: { output: EntityID<ArrayData> }) {
  const grade = gradeOf(unwrap(useEntity(output)));
  return (
    <>
      <p className={layout.fieldsLine}>
        score <b className={styles.score}>{grade.score.toFixed(2)}</b>
      </p>
      <p className={layout.prose}>{grade.rationale}</p>
    </>
  );
}

/** A grade's score and rationale, or its state while it has none. */
function GradeOne({ spawn }: { spawn: Spawn }) {
  const invocation = unwrap(useInvocation(spawn.ref));
  return (
    <div className={layout.card}>
      <InvocationHead kind="step" name={invocation.name} />
      {invocation.output ? <GradeScore output={invocation.output} /> : <InvocationOutcome invocation={invocation} />}
    </div>
  );
}

/** A grade among several: its spawn, and its score and rationale once its output is loaded. */
interface GradeRow {
  readonly spawn: Spawn;
  readonly grade: { score: number; rationale: string } | undefined;
}

/** One grade in full; several as a row each with its score, a row opening that grade alone, then
 *  their mean over the scores loaded so far (every load starts at once), saying how many are still
 *  loading. */
export function GradePanel({ invocations }: InvocationPanelProps) {
  const [picked, setPicked] = useState<string | null>(null);
  if (invocations.length === 0) return <p className={layout.empty}>no grades</p>;
  if (invocations.length === 1) return <GradeOne spawn={invocations[0]!} />;
  const graded = invocations.flatMap((spawn) => (spawn.output ? [spawn.output] : []));
  const columns: TableColumn<GradeRow>[] = [
    ...keyColumns(invocations, (row: GradeRow) => row.spawn),
    {
      key: "score",
      label: "score",
      numeric: true,
      value: (row) => row.grade?.score ?? null,
      cell: (row) =>
        row.grade ? (
          <span className={styles.score}>{row.grade.score.toFixed(2)}</span>
        ) : row.spawn.output ? (
          <span className={layout.muted}>loading…</span>
        ) : (
          <SpawnState spawn={row.spawn} />
        ),
    },
    { key: "rationale", label: "rationale", value: (row) => row.grade?.rationale ?? null, cell: (row) => excerpt(row.grade?.rationale ?? "", 80) },
  ];
  const shown = pickedSpawn(invocations, picked);
  return (
    <div className={layout.panel}>
      <InvocationHead kind="step" name={GRADE_STEP}>
        {" · "}
        {invocations.length} grades
      </InvocationHead>
      <LoadEach ids={graded} parse={parseGrade}>
        {(loaded, pending) => {
          const rows = invocations.map((spawn) => ({ spawn, grade: spawn.output ? loaded.get(idKey(spawn.output)) : undefined }));
          const scores = [...loaded.values()].map((grade) => grade.score);
          const mean = scores.reduce((sum, score) => sum + score, 0) / scores.length;
          return (
            <>
              <DataTable
                rows={rows}
                columns={columns}
                rowKey={(row) => invocationRefKey(row.spawn.ref)}
                renderRow={(row, tr) => {
                  const key = invocationRefKey(row.spawn.ref);
                  return <SpawnRow spawn={row.spawn} view={GRADES} tr={tr} picked={picked === key} onPick={() => setPicked(picked === key ? null : key)} />;
                }}
              />
              {graded.length > 0 &&
                (scores.length === 0 ? (
                  <p className={layout.muted}>loading scores…</p>
                ) : (
                  <p className={layout.fieldsLine}>
                    mean score <b>{mean.toFixed(2)}</b> over {scores.length} grades
                    {pending > 0 && <span className={layout.muted}> ({pending} loading)</span>}
                  </p>
                ))}
            </>
          );
        }}
      </LoadEach>
      {shown && (
        <Loading>
          <GradeOne spawn={shown} />
        </Loading>
      )}
    </div>
  );
}

// ── refine_bio (workflow) ──

/** The simulate and rewrite runs of one round, paired by the round number in their node names
 *  (`simulate_0` / `rewrite_0`, as the workflow body names them). */
interface RoundRuns {
  readonly round: number;
  readonly simulate: ChildRun | null;
  readonly rewrite: ChildRun | null;
}

function roundsOf(invocation: InvocationRecord): RoundRuns[] {
  const byRound = new Map<number, { simulate: ChildRun | null; rewrite: ChildRun | null }>();
  for (const child of invocation.children) {
    const match = /^(simulate|rewrite)_(\d+)$/.exec(child.nodeName);
    if (!match || (child.name !== SIMULATE_STEP && child.name !== REWRITE_STEP)) continue;
    const round = Number(match[2]);
    const runs = byRound.get(round) ?? { simulate: null, rewrite: null };
    if (match[1] === "simulate") runs.simulate = child;
    else runs.rewrite = child;
    byRound.set(round, runs);
  }
  return [...byRound.entries()].sort(([a], [b]) => a - b).map(([round, runs]) => ({ round, ...runs }));
}

/** An unmapped run's one spawn, if it was scheduled. */
function onlySpawn(child: ChildRun | null): Spawn | null {
  return child?.spawns[0] ?? null;
}

/** One spawn's state, through `useInvocation` on the spawn's ref: the subtree walk. Reads the
 *  result directly so a pending spawn shows as such without suspending the round. */
function SpawnState({ spawn }: { spawn: Spawn }) {
  const result = useInvocation(spawn.ref);
  if (result.status === "loading") return <span className={layout.muted}>loading…</span>;
  if (result.status === "error") return <span className={layout.muted}>state unavailable</span>;
  const { state, error } = result.value;
  return <span className={layout.muted}>{error ? `${state}: ${error.message}` : state}</span>;
}

/** The review a rewrite produced: its message, and the proposed bio if any. */
function Review({ output }: { output: EntityID<ArrayData> }) {
  const review = scalarValue(unwrap(useEntity(output)));
  if (!isDataMap(review)) throw new Error("a rewrite's output is a review struct");
  const proposal = review["proposal"];
  return (
    <>
      <p className={layout.prose}>{String(review["message"] ?? "")}</p>
      {isEntityID(proposal) ? (
        <div className={layout.fieldsLine}>
          proposed <EntityLink id={proposal as EntityID<BioData>} />
        </div>
      ) : (
        <p className={layout.muted}>no proposal: the bio is final</p>
      )}
    </>
  );
}

/** The simulated transcript of a round, as a link with its turn count. */
function Simulation({ output }: { output: EntityID<ArrayData> }) {
  const id = scalarEntityId(unwrap(useEntity(output))) as EntityID<SimulatedTranscriptData>;
  const transcript = unwrap(useEntity(id));
  return (
    <div className={layout.fieldsLine}>
      <EntityLink id={id} /> · {transcript.messages.length} turns
    </div>
  );
}

function Round({ runs }: { runs: RoundRuns }) {
  const simulate = onlySpawn(runs.simulate);
  const rewrite = onlySpawn(runs.rewrite);
  return (
    <li className={styles.round}>
      <div className={styles.roundHead}>
        <b>round {runs.round}</b>
        {simulate && (
          <>
            {" · simulate "}
            <SpawnState spawn={simulate} />
          </>
        )}
        {rewrite && (
          <>
            {" · rewrite "}
            <SpawnState spawn={rewrite} />
          </>
        )}
      </div>
      {simulate?.output ? (
        <Loading>
          <Simulation output={simulate.output} />
        </Loading>
      ) : (
        <p className={layout.muted}>no simulation yet</p>
      )}
      {rewrite?.output ? (
        <Loading>
          <Review output={rewrite.output} />
        </Loading>
      ) : (
        <p className={layout.muted}>no review yet</p>
      )}
    </li>
  );
}

/** The refinement as rounds: the starting bio, each round's simulation and review, the result. */
function RefineBioRounds({ invocation }: SingleInvocationProps & { storage: StorageAtom }) {
  const rounds = roundsOf(invocation);
  return (
    <div className={layout.panel}>
      <InvocationHead kind="workflow" name={invocation.name} />
      <div className={layout.fieldsLine}>
        bio <ScalarLink array={invocationInput(invocation, "bio")} /> · model <ScalarLink array={invocationInput(invocation, "model")} /> · rewriter{" "}
        <ScalarLink array={invocationInput(invocation, "rewriter")} /> · instructions <ScalarLink array={invocationInput(invocation, "instructions")} />
      </div>
      {rounds.length === 0 ? (
        <p className={layout.empty}>no rounds yet</p>
      ) : (
        <ol className={styles.rounds}>
          {rounds.map((runs) => (
            <Round key={runs.round} runs={runs} />
          ))}
        </ol>
      )}
      <h3 className={layout.subtitle}>result</h3>
      {invocation.output ? (
        <div className={layout.fieldsLine}>
          <ScalarLink array={invocation.output} />
        </div>
      ) : (
        <InvocationOutcome invocation={invocation} />
      )}
    </div>
  );
}

/** A refinement's line in a list of several: how many rounds it has run, and its state. */
function RefineBioSummary({ invocation }: SingleInvocationProps) {
  const rounds = roundsOf(invocation).length;
  return (
    <span className={layout.muted}>
      {" · "}
      {rounds} {rounds === 1 ? "round" : "rounds"} · {invocation.state}
    </span>
  );
}

/** One refinement round by round; several as a section each, headed by its key, round count and
 *  state, opening to its rounds. */
export const RefineBioRoundsPanel = eachInvocation(RefineBioRounds, { Summary: RefineBioSummary });
