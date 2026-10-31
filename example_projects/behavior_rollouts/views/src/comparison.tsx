import styles from "./rollouts.module.css";
import { Suspense, useMemo } from "react";
import { useAtom } from "jotai";
import {
  EntityDataError,
  isEntityID,
  rowsOf,
  unwrap,
  useEntity,
  validatedAtom,
  type ArrayData,
  type EntityID,
  useInvocation,
  type InvocationPanelProps,
  type Spawn,
  type StorageAtom,
} from "fxtr-view";
import { ComparedRollouts } from "fxtr-view-behaviors";

type Selection = { version: 1; sequence: string; leftModel: string; rightModel: string };
const initialSelection: Selection = { version: 1, sequence: "", leftModel: "", rightModel: "" };
function isSelection(value: unknown): value is Selection {
  return typeof value === "object" && value !== null && "version" in value && value.version === 1 &&
    "sequence" in value && typeof value.sequence === "string" &&
    "leftModel" in value && typeof value.leftModel === "string" &&
    "rightModel" in value && typeof value.rightModel === "string";
}

function ComparisonPicker({ output, storage }: { output: EntityID<ArrayData>; storage: StorageAtom }) {
  const selectionAtom = useMemo(() => validatedAtom(storage, initialSelection, isSelection), [storage]);
  const [saved, setSaved] = useAtom(selectionAtom);
  const array = unwrap(useEntity(output));
  const modelIndex = array.dims.findIndex(([name]) => name === "model");
  const sequenceIndex = array.dims.findIndex(([name]) => name === "sequence");
  if (array.dims.length !== 2 || modelIndex < 0 || sequenceIndex < 0) {
    return <p className={styles["muted"]}>This view needs an experiment with model and sequence dimensions.</p>;
  }
  const rows = rowsOf(array).map(({ key, value }) => {
    const model = key[modelIndex];
    const sequence = key[sequenceIndex];
    if (typeof model !== "string" || typeof sequence !== "string" || !isEntityID(value)) {
      throw new EntityDataError("Expected model/sequence keys and ChatRollout references");
    }
    return { model, sequence, id: value };
  });
  const sequences = [...new Set(rows.map((row) => row.sequence))];
  if (sequences.length === 0) return <p className={styles["muted"]}>No conversations recorded.</p>;
  const sequence = sequences.includes(saved.sequence) ? saved.sequence : sequences[0]!;
  const available = rows.filter((row) => row.sequence === sequence);
  const models = available.map((row) => row.model);
  const leftModel = models.includes(saved.leftModel) ? saved.leftModel : models[0]!;
  const rightModel = models.includes(saved.rightModel) && saved.rightModel !== leftModel
    ? saved.rightModel : models.filter((model) => model !== leftModel).at(-1) ?? "";
  const select = (patch: Partial<Selection>) => setSaved({ version: 1, sequence, leftModel, rightModel, ...patch });
  const left = available.find((row) => row.model === leftModel)!;
  const right = available.find((row) => row.model === rightModel);
  return <>
    <div className={`${styles["compare-pickers"]} ${styles["toolbar"]}`}>
      <label className={styles["compare-sequence"]}>Sequence
        <select value={sequence} onChange={(event) => select({ sequence: event.target.value })}>
          {sequences.map((name) => <option key={name}>{name}</option>)}
        </select>
      </label>
      <label>Left model
        <select value={leftModel} onChange={(event) => select({ leftModel: event.target.value })}>
          {models.map((name) => <option key={name} disabled={name === rightModel}>{name}</option>)}
        </select>
      </label>
      <label>Right model
        <select value={rightModel} disabled={models.length < 2} onChange={(event) => select({ rightModel: event.target.value })}>
          {!right && <option value="">No second model</option>}
          {models.map((name) => <option key={name} disabled={name === leftModel}>{name}</option>)}
        </select>
      </label>
      <div className={styles["compare-actions"]}>
        <button type="button" disabled={!right} onClick={() => select({ leftModel: rightModel, rightModel: leftModel })}>Swap models</button>
      </div>
    </div>
    {right ? (
      <Suspense fallback={<p className={styles["muted"]}>Loading selected conversations…</p>}>
        <ComparedRollouts left={left} right={right} />
      </Suspense>
    ) : <p className={styles["muted"]}>Two models are needed for this sequence ({models.length} available).</p>}
  </>;
}

function RolloutComparison({ spawn, storage }: { spawn: Spawn; storage: StorageAtom }) {
  const invocation = unwrap(useInvocation(spawn.ref));
  return (
    <section className={styles["invocation"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>workflow</span>
        <span className={styles["muted"]}>{invocation.state}</span>
        <h2>Compare rollouts</h2>
      </header>
      {invocation.error && <p className={styles["error"]}>{invocation.error.typeName}: {invocation.error.message}</p>}
      {invocation.output ? <ComparisonPicker output={invocation.output} storage={storage} />
        : <p className={styles["muted"]}>Comparison will be available when the experiment has output · {invocation.state}</p>}
    </section>
  );
}

/** The experiment workflow's two models side by side. It is the root workflow, so it is handed its
 *  one run. */
export function OpenAIRolloutComparisonPanel({ invocations, storage }: InvocationPanelProps) {
  const run = invocations[0];
  return run ? <RolloutComparison spawn={run} storage={storage} /> : null;
}
