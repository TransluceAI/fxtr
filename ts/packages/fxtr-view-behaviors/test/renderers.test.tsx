import { fireEvent, render, screen } from "@testing-library/react";
import { atom } from "jotai";
import {
  EntityHoverPreview, EntityID, EntityInlineLink, EntityPanel,
  ViewShellProvider, type EntityData, type JsonValue,
  type ViewShell,
} from "fxtr-view";
import { RegistryProvider, RenderEntity, dispatchRegistry } from "fxtr-view-shell-util";
import { PassthroughEntityLinkImpl, TableShellProvider } from "fxtr-view-shell-util/testing";
import {
  AGENT_RUN, CHAT_ROLLOUT, CONTEXT_TURN, END_ROLLOUT, MODEL_RESPONSE, MODEL_TURN,
  SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP,
  behaviorsRenderers, type ChatRolloutData, type MessageData, type TurnData,
} from "../src";
import { usageOf } from "../src/messages";

const registry = dispatchRegistry(behaviorsRenderers);
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
    metadata: [{ $type: "org.example.QuestionsAsked.v1", questions_asked: 2 }],
  },
  metadata: [],
};

function show(data: EntityData) {
  return render(
    <TableShellProvider registry={registry} entities={new Map([["entity", data]])}>
      <RenderEntity slot={EntityPanel} id={new EntityID("entity")} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
}

it("interleaves context/model turns instead of grouping all answers together", () => {
  const { container } = show(rollout);
  expect([...container.querySelectorAll(".message")].map((node) => node.textContent)).toEqual([
    "userFirst question", "assistantFirst answer", "userFollow-up question", "assistantSecond answer",
  ]);
  expect(screen.getByText("Completed")).toBeTruthy();
  expect(screen.getByText("2 responses")).toBeTruthy();
});

const initialization = (text: string) => ({
  $type: SET_SYSTEM_MESSAGE,
  message: { ...message("user", text), $type: tag("SystemMessage"), role: "system" as const },
});

it("shows the recorded system prompt before the first conversation turn", () => {
  const { container } = show({ ...rollout, initialization: initialization("Full system instructions") });
  const messages = [...container.querySelectorAll(".message")].map((node) => node.textContent);
  expect(messages.slice(0, 2)).toEqual(["systemFull system instructions", "userFirst question"]);
});

it("distinguishes absent and custom initialization", () => {
  const empty = show(rollout);
  expect(screen.getByText("No session initialization.")).toBeTruthy();
  empty.unmount();
  show({ ...rollout, initialization: { $type: "custom.Initialization", environment: "custom setup" } });
  fireEvent.click(screen.getByText("Initialization data"));
  expect(screen.getByText(/"environment": "custom setup"/)).toBeTruthy();
});

it("preserves an unanswered question and the provider's failure", () => {
  show({
    ...rollout, model_turns: [turn("assistant", "First answer")], end: null,
    status: {
      $type: tag("RolloutFailed"), kind: "rollout_failed",
      failure: { $type: tag("ModelCallFailure"), category: "context_length", description: "Context too long", provider_http_status: 400 },
    },
  });
  expect(screen.getByText("Follow-up question")).toBeTruthy();
  expect(screen.getByText("No model response recorded.")).toBeTruthy();
  expect(screen.getByText("Failed · context_length")).toBeTruthy();
  expect(screen.getByText("Context too long")).toBeTruthy();
});

it("distinguishes a turn limit from completion, including an empty rollout", () => {
  show({ ...rollout, context_turns: [], model_turns: [], end: null,
    status: { $type: tag("RolloutMaxTurns"), kind: "rollout_max_turns", max_turns: 0 } });
  expect(screen.getByText("Turn limit reached (0)")).toBeTruthy();
  expect(screen.getByText("No turns recorded.")).toBeTruthy();
  expect(screen.queryByText("Completed")).toBeNull();
});

it("renders content variants and tool results without exposing opaque reasoning as prose", () => {
  const { container } = show({
    $type: TRANSCRIPT, name: "Tool session", description: null, metadata: {}, messages: [
      { $type: tag("AssistantMessage"), role: "assistant", content: [
        { $type: tag("ContentReasoning"), type: "reasoning", text: "Consider the lookup" },
        { $type: tag("ContentSummarizedReasoning"), type: "summarized_reasoning", text: "", signature: "opaque-signature" },
        { $type: tag("ContentRedactedReasoning"), type: "redacted_reasoning", data: "encrypted-payload" },
        { $type: tag("ContentToolUse"), type: "tool_use", id: "call-1", name: "lookup", input: {}, parse_error: "Invalid JSON arguments" },
      ] },
      { $type: tag("ToolMessage"), role: "tool", tool_call_id: "call-1", is_error: true,
        content: [{ $type: tag("CacheableContentText"), type: "text", text: "<script>alert(1)</script>", cache_control: null }] },
      { $type: tag("AssistantMessage"), role: "assistant", content: [] },
    ],
  });
  fireEvent.click(screen.getByText("Tool call · lookup"));
  expect(screen.getByText("Tool call · lookup").closest("details")?.open).toBe(true);
  expect(screen.getByText("Tool error")).toBeTruthy();
  expect(screen.getByText("Invalid JSON arguments")).toBeTruthy();
  expect(screen.getByText("Reasoning redacted by provider.")).toBeTruthy();
  expect(screen.getByText("No summary text recorded.")).toBeTruthy();
  expect(screen.getByText("No content recorded.")).toBeTruthy();
  expect(container.textContent).not.toContain("encrypted-payload");
  expect(container.textContent).not.toContain("opaque-signature");
  expect(container.querySelector("script")).toBeNull();
  expect(screen.getByText("<script>alert(1)</script>")).toBeTruthy();
});

it("renders directly contained nested transcripts and empty groups", () => {
  show({
    $type: AGENT_RUN, name: "Agent evaluation", description: "Two branches", metadata: {}, transcripts: [],
    groups: [{
      $type: TRANSCRIPT_GROUP, name: "Branch A", description: null, metadata: {}, groups: [],
      transcripts: [{ $type: TRANSCRIPT, name: "Trial", description: null, metadata: {}, messages: [message("user", "Nested question")] }],
    }, { $type: TRANSCRIPT_GROUP, name: "Empty branch", description: null, metadata: {}, groups: [], transcripts: [] }],
  });
  fireEvent.click(screen.getByText("TranscriptGroup · Branch A · 1 transcript"));
  fireEvent.click(screen.getByText("Transcript · Trial · 1 message"));
  expect(screen.getByText("Nested question")).toBeTruthy();
  expect(screen.getByText("No transcripts recorded.")).toBeTruthy();
});

it("dispatches system messages, useful inline labels and bounded hover previews", () => {
  const { unmount } = show({ $type: SET_SYSTEM_MESSAGE, message: {
    $type: tag("SystemMessage"), role: "system", content: [{ $type: tag("ContentText"), type: "text", text: "Be concise." }],
  } });
  expect(screen.getByText("Be concise.")).toBeTruthy();
  unmount();
  const { container } = render(
    <TableShellProvider registry={registry} entities={new Map([["rollout", rollout]])}>
      <RenderEntity slot={EntityInlineLink} id={new EntityID("rollout")} />
      <RenderEntity slot={EntityHoverPreview} id={new EntityID("rollout")} />
    </TableShellProvider>,
  );
  expect(container.textContent).toContain("ChatRollout 2 responses · Completed");
  expect(screen.getByText("First question")).toBeTruthy();
  expect(screen.queryByText("Second answer")).toBeNull();
});

it("shows each metadata entry under its type tag", () => {
  show(rollout);
  fireEvent.click(screen.getByText("Completion metadata"));
  expect(screen.getByText("org.example.QuestionsAsked.v1")).toBeTruthy();
  expect(screen.getByText(/"questions_asked": 2/)).toBeTruthy();
  expect(screen.queryByText("Metadata")).toBeNull();
});

it("reads usage from recorded model responses, validating it and labeling partial totals", () => {
  const response = (usage: EntityData["usage"]) => ({ $type: MODEL_RESPONSE, id: "r", output: [], usage });
  const other = { $type: "org.example.Other.v1", usage: { input_tokens: 100, output_tokens: 100 } };
  const valid = {
    ...turn("assistant", "answer"),
    metadata: [other, response({ $type: tag("Usage"), input_tokens: 7, output_tokens: 3 })],
  };
  const invalid = { ...valid, metadata: [response({ $type: tag("Usage"), input_tokens: "7", output_tokens: 3 })] };
  expect(usageOf([valid, invalid])).toBe("7 input · 3 output tokens · 1/2 responses reporting usage");
  expect(usageOf([invalid])).toBeNull();
  expect(usageOf([{ ...valid, metadata: [other] }])).toBeNull();
});

it("lets the host show pending and failed entity loads", () => {
  const pending: ViewShell = { useEntity: () => ({ status: "loading", promise: new Promise(() => {}) }), EntityLinkImpl: PassthroughEntityLinkImpl };
  const view = (shell: ViewShell) => (
    <RegistryProvider registry={registry}>
      <ViewShellProvider shell={shell}>
        <RenderEntity slot={EntityPanel} id={new EntityID("unknown")} storage={atom<JsonValue>(null)}
          fallbacks={{ loading: <span>Waiting for entity</span>, error: () => <span>Entity unavailable</span> }} />
      </ViewShellProvider>
    </RegistryProvider>
  );
  const { rerender } = render(view(pending));
  expect(screen.getByText("Waiting for entity")).toBeTruthy();
  rerender(view({ ...pending, useEntity: () => ({ status: "error", error: new Error("missing") }) }));
  expect(screen.getByText("Entity unavailable")).toBeTruthy();
});
