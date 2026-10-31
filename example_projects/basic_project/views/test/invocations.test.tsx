import { fireEvent, render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";
import {
  ARRAY_TYPE,
  EntityID,
  InvocationPanel,
  invocationRefKey,
  type ArrayData,
  type EntityData,
  type InvocationRecord,
  type InvocationRef,
  type JsonValue,
  type Spawn,
} from "fxtr-view";
import { RenderInvocation, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import {
  BIO,
  GRADE_STEP,
  MESSAGE,
  MODEL_CONFIG,
  REFINE_BIO_WORKFLOW,
  REWRITE_INSTRUCTIONS,
  REWRITE_STEP,
  ROLLOUT_STEP,
  SCENARIO,
  SIMULATED_TRANSCRIPT,
  SIMULATE_STEP,
  TRANSCRIPT,
  demoRenderers,
} from "../src/index";

/** A zero-dim array holding one value. */
function scalar(schema: ArrayData["schema"], value: ArrayData["rows"][number][number]): ArrayData {
  return { $type: ARRAY_TYPE, dims: [], schema, rows: [[value]] };
}

const entities = new Map<string, EntityData>([
  ["model1", { $type: MODEL_CONFIG, name: "gpt-x", temperature: 0.7 }],
  ["scen1", { $type: SCENARIO, title: "Lost parcel", prompt: "The user cannot find their parcel." }],
  [
    "tr1",
    {
      $type: TRANSCRIPT,
      model: new EntityID("model1"),
      scenario: new EntityID("scen1"),
      messages: [
        { $type: MESSAGE, role: "user", content: "Hello there." },
        { $type: MESSAGE, role: "assistant", content: "Hi! How can I help?" },
      ],
    },
  ],
  ["bio1", { $type: BIO, name: "Ada", persona: "A patient engineer.", detail: 2 }],
  ["bio2", { $type: BIO, name: "Ada", persona: "A patient engineer, terser detail 3", detail: 3 }],
  ["instr1", { $type: REWRITE_INSTRUCTIONS, style: "terser", target_detail: 3 }],
  ["sim1", { $type: SIMULATED_TRANSCRIPT, model: new EntityID("model1"), bio: new EntityID("bio1"), messages: [] }],
  ["arr_model", scalar({ id: MODEL_CONFIG }, new EntityID("model1"))],
  ["arr_scen", scalar({ id: SCENARIO }, new EntityID("scen1"))],
  ["arr_tr", scalar({ id: TRANSCRIPT }, new EntityID("tr1"))],
  ["arr_bio", scalar({ id: BIO }, new EntityID("bio1"))],
  ["arr_bio2", scalar({ id: BIO }, new EntityID("bio2"))],
  ["arr_instr", scalar({ id: REWRITE_INSTRUCTIONS }, new EntityID("instr1"))],
  ["arr_sim", scalar({ id: SIMULATED_TRANSCRIPT }, new EntityID("sim1"))],
  [
    "arr_review",
    scalar({ struct: { proposal: { nullable: { id: BIO } }, message: "str" } }, { proposal: new EntityID("bio2"), message: "editor: added terser detail" }),
  ],
  ["arr_grade_hi", scalar({ struct: { score: "float", rationale: "str" } }, { score: 1, rationale: "mentions every criterion" })],
  ["arr_grade_lo", scalar({ struct: { score: "float", rationale: "str" } }, { score: 0.5, rationale: "misses one" })],
]);

const rolloutRef: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/rollout[model:\"small\"]" };
const rollout: InvocationRecord = {
  ref: rolloutRef,
  name: ROLLOUT_STEP,
  state: "completed",
  error: null,
  inputs: { model: new EntityID("arr_model"), scenario: new EntityID("arr_scen") },
  output: new EntityID("arr_tr"),
  children: [],
};
const failedRef: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/rollout[model:\"large\"]" };
const failed: InvocationRecord = { ...rollout, ref: failedRef, state: "failed", error: { typeName: "RuntimeError", message: "boom" }, output: null };
const smallSpawn: Spawn = { key: { model: "small" }, ref: rolloutRef, output: new EntityID("arr_tr") };
const largeSpawn: Spawn = { key: { model: "large" }, ref: failedRef, output: null };
const gradeSpawns: Spawn[] = [
  { key: { model: "small", rubric: "politeness" }, ref: { jobId: "j", kind: "step", cacheKey: "g1" }, output: new EntityID("arr_grade_hi") },
  { key: { model: "small", rubric: "brevity" }, ref: { jobId: "j", kind: "step", cacheKey: "g2" }, output: new EntityID("arr_grade_lo") },
  { key: { model: "large", rubric: "brevity" }, ref: failedRef, output: null },
];

const simulate0: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/refine/simulate_0" };
const rewrite0: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/refine/rewrite_0" };
const rewrite1: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/refine/rewrite_1" };
const refineRef: InvocationRef = { jobId: "j", kind: "workflow", pathname: "root/refine" };
const refine: InvocationRecord = {
  ref: refineRef,
  name: REFINE_BIO_WORKFLOW,
  state: "running",
  error: null,
  inputs: {
    bio: new EntityID("arr_bio"),
    model: new EntityID("arr_model"),
    rewriter: new EntityID("arr_model"),
    instructions: new EntityID("arr_instr"),
  },
  output: null,
  children: [
    { nodeName: "simulate_0", name: SIMULATE_STEP, kind: "step", dims: [], spawns: [{ key: {}, ref: simulate0, output: new EntityID("arr_sim") }] },
    { nodeName: "rewrite_0", name: REWRITE_STEP, kind: "step", dims: [], spawns: [{ key: {}, ref: rewrite0, output: new EntityID("arr_review") }] },
    { nodeName: "rewrite_1", name: REWRITE_STEP, kind: "step", dims: [], spawns: [{ key: {}, ref: rewrite1, output: null }] },
  ],
};
const invocations = new Map(
  [
    rollout,
    failed,
    refine,
    { ...refine, ref: { ...refineRef, pathname: "root/refine2" } },
    { ...rollout, ref: simulate0, name: SIMULATE_STEP },
    { ...rollout, ref: rewrite0, name: REWRITE_STEP },
    { ...rollout, ref: gradeSpawns[0]!.ref, name: GRADE_STEP, output: new EntityID<ArrayData>("arr_grade_hi") },
  ].map(
    (record) => [invocationRefKey(record.ref), record] as const,
  ),
);

const registry = dispatchRegistry(demoRenderers);
const shell = { registry, entities, invocations };
const storage = atom<JsonValue>(null);

const id = (name: string) => `basic-project-views.${name}`;

describe("rollout panel", () => {
  const panel = (spawns: readonly Spawn[]) => (
    <TableShellProvider {...shell}>
      <RenderInvocation slot={InvocationPanel} name={ROLLOUT_STEP} renderer={id("RolloutPanel")} invocations={spawns} storage={storage} />
    </TableShellProvider>
  );
  it("shows one rollout in full: who talked to what, the turns, then its arrays", () => {
    render(panel([smallSpawn]));
    expect(document.querySelector(".header")?.textContent).toBe("step rollout · ModelConfig gpt-x on Scenario Lost parcel");
    expect(document.querySelectorAll(".turn").length).toBe(2);
    expect(screen.getByText("arrays")).toBeTruthy();
    const labels = Array.from(document.querySelectorAll(".field dt")).map((n) => n.textContent);
    expect(labels).toEqual(["model", "scenario", "output"]);
  });
  it("shows the state and error instead of an output that is not there", () => {
    render(panel([largeSpawn]));
    expect(screen.getByText("RuntimeError: boom")).toBeTruthy();
  });
  it("lists several as a row each, an unfinished one by its state, a row showing that rollout alone", () => {
    render(panel([smallSpawn, largeSpawn]));
    expect(document.querySelector(".header")?.textContent).toBe("step rollout · 2 rollouts");
    const rows = Array.from(document.querySelectorAll("tbody tr"));
    expect(rows.map((row) => row.querySelector("td")?.textContent)).toEqual(["small", "large"]);
    expect(rows[0]?.textContent).toContain("Hello there.");
    expect(rows[1]?.textContent).toContain("failed: boom");
    expect(rows[1]?.textContent).toContain("no transcript");
    expect(document.querySelectorAll(".turn").length).toBe(0);
    // This shell opens no views, so the row shows its rollout below the table.
    fireEvent.click(rows[0]!);
    expect(document.querySelectorAll(".turn").length).toBe(2);
    fireEvent.click(rows[0]!);
    expect(document.querySelectorAll(".turn").length).toBe(0);
  });
});

describe("grade renderers", () => {
  it("shows one grade's score and rationale", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name={GRADE_STEP} renderer={id("GradePanel")} invocations={[gradeSpawns[0] as Spawn]} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("1.00")).toBeTruthy();
    expect(screen.getByText("mentions every criterion")).toBeTruthy();
  });
  it("shows a score per grade and the mean over the graded ones in the panel", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name={GRADE_STEP} renderer={id("GradePanel")} invocations={gradeSpawns} storage={storage} />
      </TableShellProvider>,
    );
    const scores = Array.from(document.querySelectorAll("tbody .score")).map((n) => n.textContent);
    expect(scores).toEqual(["1.00", "0.50"]);
    expect(screen.getByText("0.75")).toBeTruthy();
    expect(document.querySelectorAll("tbody tr").length).toBe(3);
  });
});

describe("refine_bio rounds panel", () => {
  const refineSpawn: Spawn = { key: {}, ref: refineRef, output: null };
  it("walks the subtree: one round per simulate/rewrite pair, spawn states through useInvocation", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name={REFINE_BIO_WORKFLOW} renderer={id("RefineBioRoundsPanel")} invocations={[refineSpawn]} storage={storage} />
      </TableShellProvider>,
    );
    const rounds = Array.from(document.querySelectorAll(".round"));
    expect(rounds.length).toBe(2);
    expect(rounds[0]?.querySelector(".roundHead")?.textContent).toBe("round 0 · simulate completed · rewrite completed");
    expect(rounds[0]?.textContent).toContain("editor: added terser detail");
    expect(rounds[0]?.textContent).toContain("proposed Bio Ada");
    // The second round's rewrite has no record served and no output yet.
    expect(rounds[1]?.querySelector(".roundHead")?.textContent).toBe("round 1 · rewrite state unavailable");
    expect(rounds[1]?.textContent).toContain("no review yet");
    expect(screen.getByText("running · no output yet")).toBeTruthy();
  });
  it("gives several refinements a section each, headed by key, round count and state", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation
          slot={InvocationPanel}
          name={REFINE_BIO_WORKFLOW}
          renderer={id("RefineBioRoundsPanel")}
          invocations={[{ ...refineSpawn, key: { bio: "a" } }, { ...refineSpawn, key: { bio: "b" }, ref: { ...refineRef, pathname: "root/refine2" } }]}
          storage={storage}
        />
      </TableShellProvider>,
    );
    const heads = Array.from(document.querySelectorAll("details > summary")).map((n) => n.textContent);
    expect(heads[0]).toBe("a · 2 rounds · running");
    expect(heads[1]).toMatch(/^b/);
    expect(document.querySelectorAll(".round").length).toBe(0);
  });
});
