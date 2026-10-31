import { fireEvent, render, screen, within } from "@testing-library/react";
import { atom } from "jotai";
import {
  ARRAY_TYPE, EntityID, InvocationPanel,
  invocationRefKey, type ArrayData, type EntityData, type InvocationRecord, type JsonValue,
} from "fxtr-view";
import { RenderInvocation, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import {
  CHAT_ROLLOUT, CONTEXT_TURN, END_ROLLOUT, MODEL_TURN, SET_SYSTEM_MESSAGE,
  type ChatRolloutData, type MessageData, type TurnData,
} from "fxtr-view-behaviors";
import { rolloutRenderers, OPENAI_CONVERSATION, OPENAI_EXPERIMENT } from "../src";

const registry = dispatchRegistry(rolloutRenderers);
const tag = (name: string) => `org.transluce.behaviors.${name}.v1`;
function message(role: "user" | "assistant", text: string): MessageData {
  return {
    $type: tag(role === "user" ? "UserMessage" : "AssistantMessage"), role,
    content: [{ $type: tag("ContentText"), type: "text", text }],
  };
}
function turn(role: "user" | "assistant", text: string): TurnData {
  return {
    $type: role === "user" ? CONTEXT_TURN : MODEL_TURN,
    kind: role === "user" ? "context_turn" : "model_turn",
    messages: [message(role, text)], metadata: [],
    ...(role === "user" ? { available_tools: null } : {}),
  };
}
const rollout: ChatRolloutData = {
  $type: CHAT_ROLLOUT,
  initialization: null,
  context_turns: [turn("user", "First question"), turn("user", "Follow-up question")],
  model_turns: [turn("assistant", "First answer"), turn("assistant", "Second answer")],
  status: { $type: tag("RolloutCompleted"), kind: "rollout_completed" },
  end: {
    $type: END_ROLLOUT, kind: "end_rollout",
    metadata: [{ $type: "org.transluce.examples.behavior_rollouts.QuestionsAsked.v1", questions_asked: 2 }],
  },
  metadata: [],
};

const initialization = (text: string) => ({
  $type: SET_SYSTEM_MESSAGE,
  message: { ...message("user", text), $type: tag("SystemMessage"), role: "system" as const },
});


it("updates an invocation from waiting to a stored conversation without changing hooks", () => {
  const ref = { kind: "workflow" as const, jobId: "job", pathname: "root" };
  const record: InvocationRecord = {
    ref, name: OPENAI_EXPERIMENT, state: "running", error: null,
    inputs: { model_name: new EntityID("model") }, output: null, children: [],
  };
  const entities = new Map<string, EntityData>([
    ["model", { $type: ARRAY_TYPE, dims: [], schema: "str", rows: [["test-model"]] }],
    ["output", { $type: ARRAY_TYPE, dims: [], schema: { id: CHAT_ROLLOUT }, rows: [[new EntityID("rollout")]] }],
    ["rollout", rollout],
  ]);
  const storage = atom<JsonValue>(null);
  const view = (invocation: InvocationRecord) => (
    <TableShellProvider registry={registry} entities={entities} invocations={new Map([[invocationRefKey(ref), invocation]])}>
      <RenderInvocation slot={InvocationPanel} name={OPENAI_EXPERIMENT}
        invocations={[{ ref, key: {}, output: invocation.output }]} storage={storage} />
    </TableShellProvider>
  );
  const { rerender } = render(view(record));
  expect(screen.getByText("No conversation output yet · running")).toBeTruthy();
  expect(screen.getByText("test-model")).toBeTruthy();
  rerender(view({ ...record, state: "failed", error: { typeName: "ProviderError", message: "Unavailable" } }));
  expect(screen.getByText("ProviderError: Unavailable")).toBeTruthy();
  rerender(view({ ...record, state: "completed", output: new EntityID("output") }));
  expect(screen.getByText("Second answer")).toBeTruthy();
  expect(screen.queryByText("ProviderError: Unavailable")).toBeNull();
});

it("renders a workflow's mapped outputs by sequence and retains scalar conversation views", () => {
  const ref = { kind: "workflow" as const, jobId: "job", pathname: "root" };
  const output = new EntityID<ArrayData>("mapped-output");
  const record: InvocationRecord = {
    ref, name: OPENAI_EXPERIMENT, state: "completed", error: null, inputs: {}, output, children: [],
  };
  const { container } = render(
    <TableShellProvider registry={registry} entities={new Map<string, EntityData>([
      ["mapped-output", { $type: ARRAY_TYPE, dims: [["sequence", "str"]], schema: { id: CHAT_ROLLOUT },
        rows: [["primes", new EntityID("primes")], ["story", new EntityID("story")]] }],
      ["primes", rollout], ["story", { ...rollout, context_turns: [turn("user", "Tell a story")], model_turns: [turn("assistant", "A robot waved.")] }],
    ])} invocations={new Map([[invocationRefKey(ref), record]])}>
      <RenderInvocation slot={InvocationPanel} name={OPENAI_EXPERIMENT}
        invocations={[{ ref, key: {}, output }]} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  expect(screen.getByText("2 question sequences")).toBeTruthy();
  const groups = [...container.querySelectorAll(".collection > summary")];
  expect(groups.map((group) => group.textContent)).toEqual([
    "sequence: primes · ChatRollout 2 responses · Completed", "sequence: story · ChatRollout 1 response · Completed",
  ]);
  fireEvent.click(groups[1]!);
  expect(groups[1]!.closest("details")?.open).toBe(true);
  expect(screen.getByText("A robot waved.")).toBeTruthy();
});

it("shows every selected mapped spawn, including running and failed conversations", () => {
  const spawns = ["primes", "story"].map((sequence) => ({
    key: { sequence }, ref: { kind: "step" as const, jobId: "job", cacheKey: sequence }, output: null,
  }));
  const records = spawns.map((spawn, index): InvocationRecord => ({
    ref: spawn.ref, name: OPENAI_CONVERSATION, state: index === 0 ? "running" : "failed",
    error: index === 0 ? null : { typeName: "ProviderError", message: "Unavailable" },
    inputs: {}, output: null, children: [],
  }));
  const view = (selected = spawns) => (
    <TableShellProvider registry={registry} invocations={new Map(records.map((record) => [invocationRefKey(record.ref), record]))}>
      <RenderInvocation slot={InvocationPanel} name={OPENAI_CONVERSATION} invocations={selected} storage={atom<JsonValue>(null)} />
    </TableShellProvider>
  );
  const { rerender } = render(view());
  expect(screen.getByText("Conversations · 2")).toBeTruthy();
  const rows = [...document.querySelectorAll("tbody tr")];
  expect(rows.map((row) => [...row.querySelectorAll("td")].map((cell) => cell.textContent))).toEqual([
    ["primes", "running"], ["story", "failed: ProviderError: Unavailable"],
  ]);
  expect(screen.queryByText("OpenAI conversation")).toBeNull();
  // This shell opens no views, so a row shows its conversation below the table.
  fireEvent.click(rows[1]!);
  expect(screen.getByText("OpenAI conversation")).toBeTruthy();
  rerender(view([spawns[0]!]));
  expect(screen.queryByText("Conversations · 2")).toBeNull();
  expect(screen.getByText("OpenAI conversation")).toBeTruthy();
});

it("compares model/sequence outputs with switchable grouping and array-valued model inputs", () => {
  const ref = { kind: "workflow" as const, jobId: "job", pathname: "root" };
  const output = new EntityID<ArrayData>("comparison");
  const record: InvocationRecord = {
    ref, name: OPENAI_EXPERIMENT, state: "completed", error: null,
    inputs: { model_name: new EntityID("models") }, output, children: [],
  };
  const { container } = render(
    <TableShellProvider registry={registry} entities={new Map<string, EntityData>([
      ["models", { $type: ARRAY_TYPE, dims: [["model", "str"]], schema: "str",
        rows: [["small", "small"], ["large", "large"]] }],
      ["comparison", { $type: ARRAY_TYPE, dims: [["model", "str"], ["sequence", "str"]],
        schema: { id: CHAT_ROLLOUT }, rows: [
          ["small", "logic", new EntityID("small-logic")],
          ["large", "logic", new EntityID("large-logic")],
          ["small", "story", new EntityID("small-story")],
          ["large", "story", new EntityID("large-story")],
        ] }],
      ...["small-logic", "large-logic", "small-story", "large-story"].map((name): [string, EntityData] => [
        name, { ...rollout, context_turns: [turn("user", `Question for ${name}`)],
          model_turns: [turn("assistant", `Answer from ${name}`)] },
      ]),
    ])} invocations={new Map([[invocationRefKey(ref), record]])}>
      <RenderInvocation slot={InvocationPanel} name={OPENAI_EXPERIMENT}
        invocations={[{ ref, key: {}, output }]} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  expect(screen.getByText("small, large")).toBeTruthy();
  expect(screen.getByText("4 conversations")).toBeTruthy();
  const groups = () => [...container.querySelectorAll(".group")];
  expect(groups().map((group) => group.querySelector("summary")?.textContent)).toEqual([
    "logic · 2 conversations", "story · 2 conversations",
  ]);
  expect(groups()[0]!.textContent).toContain("Answer from small-logic");
  expect(groups()[0]!.textContent).toContain("Answer from large-logic");
  expect(groups()[0]!.textContent).not.toContain("Answer from small-story");
  expect([...container.querySelectorAll(".collection")].every((row) => !(row as HTMLDetailsElement).open)).toBe(true);
  fireEvent.change(screen.getByLabelText("Group conversations by"), { target: { value: "model" } });
  expect(groups().map((group) => group.querySelector("summary")?.textContent)).toEqual([
    "small · 2 conversations", "large · 2 conversations",
  ]);
  expect(groups()[0]!.textContent).toContain("Answer from small-logic");
  expect(groups()[0]!.textContent).toContain("Answer from small-story");
  expect(groups()[0]!.textContent).not.toContain("Answer from large-logic");
});

function comparisonView(rows: [string, string, ChatRolloutData][], storage = atom<JsonValue>(null)) {
  const ref = { kind: "workflow" as const, jobId: "job", pathname: "root" };
  const output = new EntityID<ArrayData>("comparison-output");
  const record: InvocationRecord = {
    ref, name: OPENAI_EXPERIMENT, state: "completed", error: null, inputs: {}, output, children: [],
  };
  return (
    <TableShellProvider registry={registry} entities={new Map<string, EntityData>([
      ["comparison-output", { $type: ARRAY_TYPE, dims: [["sequence", "str"], ["model", "str"]],
        schema: { id: CHAT_ROLLOUT }, rows: rows.map(([sequence, model], index) => [sequence, model, new EntityID(`rollout-${index}`)]) }],
      ...rows.map(([, , data], index): [string, EntityData] => [`rollout-${index}`, data]),
    ])} invocations={new Map([[invocationRefKey(ref), record]])}>
      <RenderInvocation slot={InvocationPanel} name={OPENAI_EXPERIMENT} renderer="compare"
        invocations={[{ ref, key: {}, output }]} storage={storage} />
    </TableShellProvider>
  );
}

it("selects one sequence and two distinct models, aligns answers by turn, and swaps columns", () => {
  const rows: [string, string, ChatRolloutData][] = ["logic", "story"].flatMap((sequence) =>
    ["small", "medium", "large"].map((model): [string, string, ChatRolloutData] => [sequence, model, {
      ...rollout,
      context_turns: [turn("user", `${sequence} question 1`), turn("user", `${sequence} question 2`)],
      model_turns: [turn("assistant", `${model} ${sequence} answer 1`), turn("assistant", `${model} ${sequence} answer 2`)],
    }]),
  );
  render(comparisonView(rows));
  const first = screen.getByRole("region", { name: "Comparison turn 1" });
  expect(within(first).getAllByText("logic question 1")).toHaveLength(1);
  expect(within(first).getByText("small logic answer 1")).toBeTruthy();
  expect(within(first).getByText("large logic answer 1")).toBeTruthy();
  expect(within(first).queryByText("large logic answer 2")).toBeNull();
  expect(screen.queryByText("medium logic answer 1")).toBeNull();
  expect((within(screen.getByLabelText("Right model")).getByRole("option", { name: "small" }) as HTMLOptionElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Right model"), { target: { value: "medium" } });
  expect(within(first).getByText("medium logic answer 1")).toBeTruthy();
  expect(screen.queryByText("large logic answer 1")).toBeNull();
  fireEvent.change(screen.getByLabelText("Sequence"), { target: { value: "story" } });
  expect(screen.queryByText("logic question 1")).toBeNull();
  expect(within(first).getByText("story question 1")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Swap models" }));
  expect([...first.querySelectorAll(".compare-answer")].map((cell) => cell.textContent)).toEqual([
    "assistantmedium story answer 1", "assistantsmall story answer 1",
  ]);
  expect((screen.getByLabelText("Left model") as HTMLSelectElement).value).toBe("medium");
  expect((screen.getByLabelText("Right model") as HTMLSelectElement).value).toBe("small");
});

it("keeps unequal and failed rollouts aligned, and preserves different context messages", () => {
  render(comparisonView([
    ["logic", "failed", { ...rollout, model_turns: [turn("assistant", "Partial answer")], end: null,
      status: { $type: tag("RolloutFailed"), kind: "rollout_failed", failure: {
        $type: tag("ModelCallFailure"), category: "rate_limit", description: "Provider limit reached",
      } } }],
    ["logic", "complete", { ...rollout,
      context_turns: [turn("user", "Different first question"), ...rollout.context_turns.slice(1), turn("user", "Extra question")],
      model_turns: [...rollout.model_turns, turn("assistant", "Extra answer")],
    }],
  ]));
  expect(screen.getByText("Provider limit reached")).toBeTruthy();
  const first = screen.getByRole("region", { name: "Comparison turn 1" });
  expect(first.querySelector(".shared-context")).toBeNull();
  expect(within(first).getByText("First question")).toBeTruthy();
  expect(within(first).getByText("Different first question")).toBeTruthy();
  const second = screen.getByRole("region", { name: "Comparison turn 2" });
  expect(within(second).getAllByText("Follow-up question")).toHaveLength(1);
  expect([...second.querySelectorAll(".compare-answer")].map((cell) => cell.textContent)).toEqual([
    "No response recorded.", "assistantSecond answer",
  ]);
  const third = screen.getByRole("region", { name: "Comparison turn 3" });
  expect(within(third).getByText("No context recorded.")).toBeTruthy();
  expect(within(third).getByText("Extra question")).toBeTruthy();
  expect(within(third).getByText("Extra answer")).toBeTruthy();
});

it("shows each compared rollout's complete initialization in the matching column", () => {
  const { container } = render(comparisonView([
    ["pair", "small", { ...rollout, initialization: initialization("Left identity") }],
    ["pair", "large", { ...rollout, initialization: initialization("Right identity") }],
  ]));
  expect(screen.getAllByRole("region", { name: "Session initialization" }).map((node) => node.textContent))
    .toEqual(["System promptsystemLeft identity", "System promptsystemRight identity"]);
  expect(container.querySelector(".message[data-role='system']")?.textContent).toBe("systemLeft identity");
});

it("recovers stale selections and handles sequences without a second model", () => {
  render(comparisonView([
    ["single", "only", rollout],
    ["pair", "small", rollout], ["pair", "large", rollout],
  ], atom<JsonValue>({ version: 1, sequence: "removed", leftModel: "missing", rightModel: "missing" })));
  expect(screen.getByText("Two models are needed for this sequence (1 available).")).toBeTruthy();
  expect((screen.getByLabelText("Right model") as HTMLSelectElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Sequence"), { target: { value: "pair" } });
  expect(screen.getByRole("region", { name: "Comparison turn 1" })).toBeTruthy();
  expect((screen.getByLabelText("Left model") as HTMLSelectElement).value).toBe("small");
  expect((screen.getByLabelText("Right model") as HTMLSelectElement).value).toBe("large");
  fireEvent.change(screen.getByLabelText("Sequence"), { target: { value: "single" } });
  expect(screen.queryByRole("region", { name: "Comparison turn 1" })).toBeNull();
});
