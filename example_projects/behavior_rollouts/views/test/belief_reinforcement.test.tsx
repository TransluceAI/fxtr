import { fireEvent, render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { ARRAY_TYPE, EntityID, EntityPanel, type ArrayData, type EntityData, type JsonValue } from "fxtr-view";
import { RenderEntity, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import { rolloutRenderers } from "../src";
import { BELIEF_REINFORCEMENT_REPORT, readBeliefReinforcementRows, verdictsDisagree } from "../src/belief_reinforcement";

const array = (dims: string[], rows: unknown[][]) => ({
  $type: ARRAY_TYPE, dims: dims.map((name) => [name, name === "sample" ? "int" : "str"]), schema: "str", rows,
}) as unknown as ArrayData;

const cases = array(["case"], [["friends_weak", { situation: "friends", evidence: "weak", turns: [] }]]);
const rollouts = array(["model", "case"], [["subject", "friends_weak", new EntityID("rollout")]]);
const dims = ["model", "case", "judge", "sample"];
const judgments = array(dims, [
  ["subject", "friends_weak", "judge-a", 0, new EntityID("judgment-a")],
  ["subject", "friends_weak", "judge-b", 0, new EntityID("judgment-b")],
]);
const verdicts = array(dims, [
  ["subject", "friends_weak", "judge-a", 0, { reinforcement: true, emotional_responsiveness: "supportive", overall: "mixed", answers: 1 }],
  ["subject", "friends_weak", "judge-b", 0, { reinforcement: false, emotional_responsiveness: "supportive", overall: "good", answers: 1 }],
]);

it("counts disagreement per metric and excludes missing answers from disagreement", () => {
  const rows = readBeliefReinforcementRows(cases, rollouts, judgments, verdicts);
  expect(rows).toHaveLength(1);
  expect(verdictsDisagree(rows[0]!.verdicts, "reinforcement")).toBe(true);
  expect(verdictsDisagree(rows[0]!.verdicts, "emotional_responsiveness")).toBe(false);
  const missing = array(dims, [
    ["subject", "friends_weak", "judge-a", 0, { reinforcement: true }],
    ["subject", "friends_weak", "judge-b", 0, { reinforcement: null }],
  ]);
  expect(verdictsDisagree(readBeliefReinforcementRows(cases, rollouts, judgments, missing)[0]!.verdicts, "reinforcement")).toBe(false);
});

it("switches metrics, filters disagreements and opens individual judgments", () => {
  const opened: string[] = [];
  const entities = new Map<string, EntityData>([
    ["cases", cases], ["rollouts", rollouts], ["judgments", judgments], ["verdicts", verdicts],
    ["report", {
      $type: BELIEF_REINFORCEMENT_REPORT, cases: new EntityID("cases"), rollouts: new EntityID("rollouts"),
      judgments: new EntityID("judgments"), verdicts: new EntityID("verdicts"), transcripts: new EntityID("transcripts"),
    }],
  ]);
  render(<TableShellProvider registry={dispatchRegistry(rolloutRenderers)} entities={entities}
    links={{ open: (id) => { opened.push(id.cid); }, isOpen: () => false }}>
    <RenderEntity slot={EntityPanel} id={new EntityID("report")} storage={atom<JsonValue>(null)} />
  </TableShellProvider>);
  fireEvent.click(screen.getByRole("button", { name: "judge-a, sample 1, reinforced" }));
  expect(opened).toEqual(["judgment-a"]);
  fireEvent.click(screen.getByLabelText("Show disagreements only"));
  expect(screen.getByRole("button", { name: "judge-b, sample 1, calibrated" })).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Judgment"), { target: { value: "emotional_responsiveness" } });
  expect(screen.queryByRole("button", { name: "judge-a, sample 1, supportive" })).toBeNull();
  fireEvent.click(screen.getByLabelText("Show disagreements only"));
  expect(screen.getByRole("button", { name: "judge-a, sample 1, supportive" })).toBeTruthy();
});
