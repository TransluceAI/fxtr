import { fireEvent, render, screen, within } from "@testing-library/react";
import { atom } from "jotai";
import {
  ARRAY_TYPE, EntityID, InvocationPanel, invocationRefKey,
  type ArrayData, type EntityData, type InvocationRecord, type JsonValue,
} from "fxtr-view";
import { RenderInvocation, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import {
  CHAT_ROLLOUT, CONTEXT_TURN, JUDGMENT, MODEL_TURN, TEXT_ENTITY, TRANSCRIPT,
  type ChatRolloutData, type JudgmentData, type MessageData, type TurnData,
} from "fxtr-view-behaviors";
import { INSTRUCTIONS_IN_FORCE_WORKFLOW, rolloutRenderers } from "../src";
import { INSTRUCTIONS_IN_FORCE_REPORT } from "../src/instructions_in_force/report";

const registry = dispatchRegistry(rolloutRenderers);
const tag = (name: string) => `org.transluce.behaviors.${name}.v1`;
const message = (role: "user" | "assistant", text: string): MessageData => ({
  $type: tag(role === "user" ? "UserMessage" : "AssistantMessage"), role,
  content: [{ $type: tag("ContentText"), type: "text", text }],
});
const turn = (role: "user" | "assistant", text: string): TurnData => ({
  $type: role === "user" ? CONTEXT_TURN : MODEL_TURN, kind: role === "user" ? "context_turn" : "model_turn",
  messages: [message(role, text)], metadata: [], ...(role === "user" ? { available_tools: null } : {}),
});
const rollout = (reply: string): ChatRolloutData => ({
  $type: CHAT_ROLLOUT, initialization: null,
  context_turns: [turn("user", "Never say party.")], model_turns: [turn("assistant", reply)],
  status: { $type: tag("RolloutCompleted"), kind: "rollout_completed" }, end: null, metadata: [],
});
const array = (dims: string[], schema: unknown, rows: unknown[][]) =>
  ({ $type: ARRAY_TYPE, dims: dims.map((name) => [name, name === "sample" ? "int" : "str"]), schema, rows }) as unknown as ArrayData;

const MODELS = ["small", "large"];
const JUDGES = ["strict", "lenient"];
const SAMPLES = [0, 1];
/** Whether a judge found a violation in a conversation, sample by sample: "large" says "party",
 *  which "strict" always flags and "lenient" flags only in its second sample. */
const found = (model: string, judge: string, sample: number) => model === "large" && (judge === "strict" || sample === 1);

const entities = new Map<string, EntityData>();
entities.set("rubric", { $type: TEXT_ENTITY, text: "Did the assistant break an instruction?" });
const judgmentKeys: unknown[][] = [];
const verdictRows: unknown[][] = [];
for (const model of MODELS) {
  entities.set(`rollout-${model}`, rollout(model === "large" ? "Enjoy the party." : "Enjoy the gathering."));
  entities.set(`transcript-${model}`, {
    $type: TRANSCRIPT, name: null, description: null, metadata: {},
    messages: [message("user", "Never say party."), message("assistant", model === "large" ? "Enjoy the party." : "Enjoy the gathering.")],
  });
  for (const judge of JUDGES) {
    for (const sample of SAMPLES) {
      const cid = `judgment-${model}-${judge}-${sample}`;
      const match = found(model, judge, sample);
      const discussion = match ? "It says the banned word [transcript:B1L0]." : "It keeps to the rule [transcript:B0L0].";
      const judgment: JudgmentData = {
        $type: JUDGMENT,
        entity_ids: { rubric: new EntityID("rubric"), transcript: new EntityID(`transcript-${model}`) },
        output: { discussion, match, first_violation: match ? "[transcript:B1L0]" : "" },
        citations: [{
          $type: tag("Citation"), entity_id: new EntityID(`transcript-${model}`), address_in_entity: match ? "B1L0" : "B0L0",
          path_to_output_field: ["discussion"], start_in_field: discussion.indexOf("["), end_in_field: discussion.indexOf("]") + 1,
        }],
        attempts: [],
      };
      entities.set(cid, judgment);
      judgmentKeys.push([model, "never_say_party", judge, sample, new EntityID(cid)]);
      // The lenient judge's first answer about "large" was sent back once.
      const answers = model === "large" && judge === "lenient" && sample === 0 ? 2 : 1;
      verdictRows.push([model, "never_say_party", judge, sample, { violation: match, answers }]);
    }
  }
}
const DIMS = ["model", "sequence", "judge", "sample"];
entities.set("rollouts", array(["model", "sequence"], { id: CHAT_ROLLOUT },
  MODELS.map((model) => [model, "never_say_party", new EntityID(`rollout-${model}`)])));
entities.set("transcripts", array(["model", "sequence"], { id: TRANSCRIPT },
  MODELS.map((model) => [model, "never_say_party", new EntityID(`transcript-${model}`)])));
entities.set("judgments", array(DIMS, { id: JUDGMENT }, judgmentKeys));
entities.set("verdicts", array(DIMS, { struct: { violation: { nullable: "bool" }, answers: "int" } }, verdictRows));
entities.set("report", {
  $type: INSTRUCTIONS_IN_FORCE_REPORT, rollouts: new EntityID("rollouts"), transcripts: new EntityID("transcripts"),
  judgments: new EntityID("judgments"), verdicts: new EntityID("verdicts"),
});
entities.set("output", array([], { id: INSTRUCTIONS_IN_FORCE_REPORT }, [[new EntityID("report")]]));

const ref = { kind: "workflow" as const, jobId: "job", pathname: "root" };
const record: InvocationRecord = {
  ref, name: INSTRUCTIONS_IN_FORCE_WORKFLOW, state: "completed", error: null, inputs: {}, output: new EntityID("output"),
  children: [],
};

function workflowView(renderer: string, opened: unknown[] = []) {
  const links = { open: (id: EntityID, params?: unknown) => { opened.push([id.cid, params]); }, isOpen: () => false };
  return (
    <TableShellProvider registry={registry} entities={entities} links={links}
      invocations={new Map([[invocationRefKey(ref), record]])}>
      <RenderInvocation slot={InvocationPanel} name={INSTRUCTIONS_IN_FORCE_WORKFLOW} renderer={renderer}
        invocations={[{ ref, key: {}, output: record.output }]} storage={atom<JsonValue>(null)} />
    </TableShellProvider>
  );
}

it("shows the workflow's conversations from the report it returns", () => {
  render(workflowView("conversation"));
  expect(screen.getByText("2 conversations")).toBeTruthy();
  expect(screen.getByText("Enjoy the party.")).toBeTruthy();
  expect(screen.getByText("Enjoy the gathering.")).toBeTruthy();
});

it("sums up each model's verdicts by judge and opens a judgment or a conversation from its cell", () => {
  const opened: unknown[] = [];
  render(workflowView("overview", opened));
  const [rates, grid] = screen.getAllByRole("table");
  const rows = [...rates!.querySelectorAll("tbody tr")].map((row) => [...row.children].map((cell) => cell.textContent));
  expect(rows).toEqual([
    ["small", "0%0 of 2 judgments", "0%0 of 2 judgments"],
    ["large", "100%2 of 2 judgments", "50%1 of 2 judgments · samples split on 1 of 1"],
  ]);
  const large = within(grid!).getAllByRole("cell")[1]!;
  expect(large.textContent).toContain("lenient · split");
  fireEvent.click(within(large).getByRole("button", { name: "lenient, sample 0: no violation, after 2 answers" }));
  fireEvent.click(within(large).getByRole("button", { name: "conversation" }));
  expect(opened).toEqual([["judgment-large-lenient-0", undefined], ["rollout-large", undefined]]);
  expect(screen.getByText("1")).toBeTruthy();  // one judgment was corrected
});
