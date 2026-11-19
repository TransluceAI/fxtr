import { fireEvent, render, screen } from "@testing-library/react";
import { describe } from "vitest";
import { atom } from "jotai";
import {
  EntityHoverPreview, EntityID, EntityInlineLink, EntityPanel, PartialEntityInline, PartialEntityPanel, markPartial,
  ViewShellProvider, type EntityData, type JsonValue,
  type ViewShell,
} from "fxtr-view";
import { RegistryProvider, RenderEntity, RenderValue, dispatchRegistry } from "fxtr-view-shell-util";
import { PassthroughEntityLinkImpl, TableShellProvider } from "fxtr-view-shell-util/testing";
import {
  AGENT_RUN, CHAT_ROLLOUT, CONTEXT_TURN, END_ROLLOUT, MODEL_RESPONSE, MODEL_TURN,
  SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP,
  behaviorsRenderers, type ChatRolloutData, type MessageData, type TurnData,
} from "../src";
import { usageLine, rolloutUsageLine } from "../src/messages";

const registry = dispatchRegistry(behaviorsRenderers);
const tag = (name: string) => `org.transluce.behaviors.${name}.v1`;
const REASONING = "org.transluce.behaviors.ContentReasoning.v2";
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

it("marks the message a selector picks, counting turn by turn, and ignores one of another shape", () => {
  const marked = (params: Record<string, JsonValue>) => {
    const { container, unmount } = render(
      <TableShellProvider registry={registry} entities={new Map([["entity", rollout]])}>
        <RenderEntity slot={EntityPanel} id={new EntityID("entity")} storage={atom<JsonValue>(null)} params={params} />
      </TableShellProvider>,
    );
    const found = [...container.querySelectorAll(".message[data-selected]")].map((node) => node.textContent);
    unmount();
    return found;
  };
  expect(marked({ selector: { message: 2 } })).toEqual(["userFollow-up question"]);
  expect(marked({ selector: { message: 3 } })).toEqual(["assistantSecond answer"]);
  expect(marked({ selector: { message: 9 } })).toEqual([]);
  expect(marked({ selector: "2" })).toEqual([]);
  expect(marked({})).toEqual([]);
});

it("opens an agent run at the transcript, message and lines a selector picks, a tool call being one line", () => {
  const talk = (name: string, ...texts: string[]) => ({ $type: TRANSCRIPT, name, description: null, metadata: {}, messages: texts.map((text) => message("assistant", text)) });
  const tooled = {
    $type: TRANSCRIPT, name: "tooled", description: null, metadata: {},
    messages: [message("user", "Go"), { $type: tag("AssistantMessage"), role: "assistant", content: [
      { $type: tag("ContentText"), type: "text", text: "Looking.\n\nNow." },
      { $type: tag("ContentToolUse"), type: "tool_use", id: "call-1", name: "lookup", input: { q: 1 }, parse_error: null },
    ] }],
  };
  // The run's transcripts in the judge's order: "top" (T0), then the group's "inner" (T1), then
  // the nested group's "tooled" (T2).
  const run = {
    $type: AGENT_RUN, name: "Run", description: null, metadata: {}, transcripts: [talk("top", "a")],
    groups: [{ $type: TRANSCRIPT_GROUP, name: "G", description: null, metadata: {}, transcripts: [talk("inner", "b", "c")],
      groups: [{ $type: TRANSCRIPT_GROUP, name: "H", description: null, metadata: {}, transcripts: [tooled], groups: [] }] }],
  };
  const opened = (selector: JsonValue) => {
    const { container, unmount } = render(
      <TableShellProvider registry={registry} entities={new Map([["entity", run as unknown as EntityData]])}>
        <RenderEntity slot={EntityPanel} id={new EntityID("entity")} storage={atom<JsonValue>(null)} params={{ selector }} />
      </TableShellProvider>,
    );
    const result = {
      open: [...container.querySelectorAll("details[open] > summary")].map((node) => node.textContent?.split(" · ").slice(0, 2).join(" · ")),
      selected: container.querySelector(".message[data-selected]")?.textContent,
      lines: [...container.querySelectorAll("[data-line]")].map((node) => `${(node as HTMLElement).dataset["line"]}:${node.tagName}`),
    };
    unmount();
    return result;
  };
  expect(opened({ transcript: 1, message: 1 })).toEqual({ open: ["TranscriptGroup · G", "Transcript · inner"], selected: "assistantc", lines: [] });
  // Line 2 of the message is its tool call, after the two non-empty lines of its text.
  const call = opened({ transcript: 2, message: 1, lines: [1, 2] });
  expect(call.open).toEqual(["TranscriptGroup · G", "TranscriptGroup · H", "Transcript · tooled", "Tool call · lookup"]);
  expect(call.lines).toEqual(["1:MARK", "2:DETAILS"]);
  expect(opened({ message: 0 }).selected).toBe("assistanta");
});

it("highlights the lines of a transcript's message a selector picks, counting its non-empty lines", () => {
  const transcript = {
    $type: TRANSCRIPT, name: "Lines", description: null, metadata: {},
    messages: [message("user", "Question"), message("assistant", "First\n\nSecond\nThird\n\nFourth")],
  };
  const picked = (selector: JsonValue) => {
    const { container, unmount } = render(
      <TableShellProvider registry={registry} entities={new Map([["entity", transcript as EntityData]])}>
        <RenderEntity slot={EntityPanel} id={new EntityID("entity")} storage={atom<JsonValue>(null)} params={{ selector }} />
      </TableShellProvider>,
    );
    const found = [...container.querySelectorAll("mark")].map((node) => node.textContent?.trim());
    const text = container.querySelector(".message[data-selected]")?.textContent;
    unmount();
    return { found, text };
  };
  expect(picked({ message: 1, lines: [1, 2] }).found).toEqual(["Second", "Third"]);
  expect(picked({ message: 1, lines: [3, 3] }).found).toEqual(["Fourth"]);
  // The message's text is unchanged by the highlight, and a whole-message selector marks no line.
  expect(picked({ message: 1, lines: [0, 0] }).text).toBe("assistantFirst\n\nSecond\nThird\n\nFourth");
  expect(picked({ message: 1 }).found).toEqual([]);
  expect(picked({ message: 1, lines: [2, 1] }).found).toEqual([]);
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
        { $type: REASONING, type: "reasoning", text: "Consider the lookup", form: "full", reasoning_state: null },
        { $type: REASONING, type: "reasoning", text: "", form: "summary",
          reasoning_state: { $type: tag("AnthropicSignature"), signature: "opaque-signature" } },
        { $type: REASONING, type: "reasoning", text: "", form: "redacted",
          reasoning_state: { $type: tag("AnthropicRedactedThinking"), data: "encrypted-payload" } },
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
  expect(screen.getByText("Reasoning")).toBeTruthy();
  expect(screen.getByText("Consider the lookup")).toBeTruthy();
  expect(screen.getByText("Reasoning summary")).toBeTruthy();
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
  expect(container.textContent).toContain("ChatRollout · 2 responses · Completed");
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
  expect(rolloutUsageLine([valid, invalid])).toBe("7 input · 3 output tokens · 1/2 responses reporting usage");
  expect(rolloutUsageLine([invalid])).toBeNull();
  expect(rolloutUsageLine([{ ...valid, metadata: [other] }])).toBeNull();
});

it("counts cached input as input, noting it, and notes the output spent on reasoning", () => {
  const cached = { input_tokens: 1000, cache_read_input_tokens: 20000, cache_write_input_tokens: 345, output_tokens: 900, reasoning_output_tokens: 600 };
  const plain = { input_tokens: 0, cache_read_input_tokens: null, output_tokens: 334, reasoning_output_tokens: null };
  expect(usageLine([cached, plain])).toBe("21,345 input (20,000 cached) · 1,234 output (600 reasoning) tokens");
  // Without cached or reasoning tokens, neither is noted.
  expect(usageLine([plain])).toBe("0 input · 334 output tokens");
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

describe("a rollout still being produced", () => {
  function showPartial(value: EntityData, { partial = true, live = true } = {}) {
    return render(
      <TableShellProvider registry={registry} entities={new Map()}>
        <RenderValue slot={PartialEntityPanel} value={value} partial={partial} live={live} storage={atom<JsonValue>(null)} />
      </TableShellProvider>,
    );
  }
  const usageEntry = { $type: MODEL_RESPONSE, usage: { input_tokens: 10, output_tokens: 5 } };
  const answered = { ...turn("assistant", "First answer"), metadata: [usageEntry] };
  /** The reply the model is generating, as the viewer hands it over: unwrapped, and marked. */
  function inFlight(...content: EntityData[]): EntityData {
    return markPartial({
      $type: MODEL_TURN, kind: "model_turn", metadata: [],
      messages: [markPartial({ $type: tag("AssistantMessage"), role: "assistant", content })],
    });
  }
  function rolloutWith(modelTurns: readonly EntityData[], contextTurns = [turn("user", "First question"), turn("user", "Follow-up question")]) {
    return markPartial({
      $type: CHAT_ROLLOUT, initialization: null, context_turns: contextTurns, model_turns: modelTurns,
      status: null, end: null, metadata: [],
    } as unknown as EntityData);
  }
  const streaming = rolloutWith([
    answered,
    inFlight(
      { $type: REASONING, type: "reasoning", text: "Thinking it", form: "summary", reasoning_state: null },
      { $type: tag("ContentText"), type: "text", text: "Second ans" },
      markPartial({ $type: tag("ContentToolUse"), type: "tool_use", id: "call_1", name: "search", input: null, parse_error: null }),
    ),
  ]);

  it("marks the reply being generated as streaming, and counts finished replies only", () => {
    const { container } = showPartial(streaming);
    expect(screen.getByText("In progress")).toBeTruthy();
    expect(screen.getByText("· streaming…")).toBeTruthy();
    expect([...container.querySelectorAll(".message")].map((node) => node.querySelector("header")?.textContent)).toEqual([
      "user", "assistant", "user", "assistant",
    ]);
    expect(container.textContent).toContain("Second ans");
    // The reply in flight is neither a response yet nor short of usage.
    expect(screen.getByText("1 response · 10 input · 5 output tokens")).toBeTruthy();
  });

  it("says a tool call's arguments are not yet received instead of showing them empty", () => {
    const { container } = showPartial(streaming);
    expect(screen.getByText("Tool call · search")).toBeTruthy();
    expect(screen.getByText("Arguments not yet received.")).toBeTruthy();
    expect(container.textContent).not.toContain("{}");
  });

  it("after a finished reply, waits on the user, or on tools when the reply called one", () => {
    showPartial(rolloutWith([answered, turn("assistant", "Second answer")]));
    expect(screen.getByText("· waiting for the user…")).toBeTruthy();
    expect(screen.queryByText("· streaming…")).toBeNull();
    const calling = {
      ...turn("assistant", "Let me look"),
      messages: [{ ...message("assistant", "Let me look"), content: [
        { $type: tag("ContentText"), type: "text", text: "Let me look" },
        { $type: tag("ContentToolUse"), type: "tool_use", id: "c", name: "search", input: { q: "x" }, parse_error: null },
      ] }],
    };
    showPartial(rolloutWith([answered, calling]));
    expect(screen.getByText("· waiting for tools…")).toBeTruthy();
  });

  it("waits for the model on a turn it has not answered, and reads lists not yet known as empty", () => {
    const waiting = { ...rolloutWith([answered]), metadata: null } as unknown as EntityData;
    showPartial(waiting);
    expect(screen.getByText("· waiting for the model…")).toBeTruthy();
    expect(screen.queryByText("No model response recorded.")).toBeNull();
  });

  it("once its step has stopped, says it stopped before finishing, not that it is in progress", () => {
    showPartial(streaming, { live: false });
    expect(screen.getByText("Stopped before finishing")).toBeTruthy();
    expect(screen.getByText("· reply cut off")).toBeTruthy();
    expect(screen.queryByText("In progress")).toBeNull();
    expect(screen.queryByText("· streaming…")).toBeNull();
  });

  const completed = { $type: tag("RolloutCompleted"), kind: "rollout_completed" };
  const failedStatus = {
    $type: tag("RolloutFailed"), kind: "rollout_failed",
    failure: { $type: tag("ModelCallFailure"), category: "context_length", description: "Context too long", provider_http_status: 400 },
  };
  /** A rollout that has ended, while its step has yet to store it: still a preview, its status known. */
  function ended(value: EntityData, status: object): EntityData {
    return markPartial({ ...value, status } as unknown as EntityData);
  }

  it("once the rollout has ended, says how it ended and waits on nothing", () => {
    showPartial(ended(rolloutWith([answered, turn("assistant", "Second answer")]), completed));
    expect(screen.getByText("Completed")).toBeTruthy();
    expect(screen.queryByText("In progress")).toBeNull();
    expect(document.body.textContent).not.toContain("waiting");
  });

  it("once the rollout has ended on a failure, shows the failure, not a wait for the model", () => {
    showPartial(ended(rolloutWith([answered]), failedStatus));
    expect(screen.getByText("Failed · context_length")).toBeTruthy();
    expect(screen.getByText("Context too long")).toBeTruthy();
    expect(screen.getByText("No model response recorded.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("waiting");
  });

  it("shows a finished preview as the stored rollout", () => {
    showPartial(rollout, { partial: false });
    expect(screen.getByText("Completed")).toBeTruthy();
    expect(screen.queryByText("In progress")).toBeNull();
    expect(document.body.textContent).not.toContain("waiting");
  });

  function lineOf(value: EntityData, { partial = true, live = true } = {}) {
    return render(
      <TableShellProvider registry={registry} entities={new Map()}>
        <RenderValue slot={PartialEntityInline} value={value} partial={partial} live={live} />
      </TableShellProvider>,
    ).container.textContent;
  }

  it("says in one line the turn it is on and what that turn waits on", () => {
    expect(lineOf(streaming)).toBe("ChatRollout · turn 2 · streaming…");
    expect(lineOf(rolloutWith([answered, turn("assistant", "Second answer")]))).toBe("ChatRollout · turn 2 · waiting for the user…");
    expect(lineOf(rolloutWith([answered]))).toBe("ChatRollout · turn 2 · waiting for the model…");
    expect(lineOf(rolloutWith([], []))).toBe("ChatRollout · waiting for the model…");
    expect(lineOf(streaming, { live: false })).toBe("ChatRollout · turn 2 · reply cut off");
  });

  it("reads a rollout that has ended in one line as the stored rollout's link does", () => {
    expect(lineOf(ended(rolloutWith([answered, turn("assistant", "Second answer")]), completed))).toBe("ChatRollout · 2 responses · Completed");
    expect(lineOf(ended(rolloutWith([answered]), failedStatus))).toBe("ChatRollout · 1 response · Failed · context_length");
  });

  it("reads a finished preview in one line as the stored rollout's link does", () => {
    expect(lineOf(rollout, { partial: false })).toBe(`ChatRollout · ${rollout.model_turns.length} responses · Completed`);
  });
});

describe("a transcript, or a group of them, still being produced", () => {
  function show(slot: typeof PartialEntityPanel | typeof PartialEntityInline, value: EntityData, { partial = true, live = true } = {}) {
    return render(
      <TableShellProvider registry={registry} entities={new Map()}>
        {slot === PartialEntityPanel
          ? <RenderValue slot={PartialEntityPanel} value={value} partial={partial} live={live} storage={atom<JsonValue>(null)} />
          : <RenderValue slot={PartialEntityInline} value={value} partial={partial} live={live} />}
      </TableShellProvider>,
    ).container;
  }
  /** A message still being written, as the viewer hands it over: marked, its text so far. */
  const writing = (text: string) => markPartial({ ...message("assistant", text) } as unknown as EntityData);
  const transcript = (messages: readonly EntityData[], name: string | null = "Session") => markPartial({
    $type: TRANSCRIPT, name, description: null, messages, metadata: null,
  } as unknown as EntityData);
  const streaming = transcript([message("user", "Fix the bug") as unknown as EntityData, writing("Looking at the")]);
  const finished = { $type: TRANSCRIPT, name: "Setup", description: null, metadata: {},
    messages: [message("user", "Install it"), message("assistant", "Done")] } as unknown as EntityData;
  const run = markPartial({ $type: AGENT_RUN, name: "fix-bug", description: null, metadata: null,
    transcripts: [finished, streaming], groups: null } as unknown as EntityData);

  it("shows a transcript's messages so far, counting finished ones, the one being written marked", () => {
    const container = show(PartialEntityPanel, streaming);
    expect(screen.getByText("In progress")).toBeTruthy();
    expect(screen.getByText("1 message")).toBeTruthy();
    expect(container.textContent).toContain("Looking at the");
    expect(screen.getByText("streaming…")).toBeTruthy();
  });

  it("once its step has stopped, says the transcript stopped and its last message was cut off", () => {
    show(PartialEntityPanel, streaming, { live: false });
    expect(screen.getByText("Stopped before finishing")).toBeTruthy();
    expect(screen.getByText("message cut off")).toBeTruthy();
    expect(screen.queryByText("streaming…")).toBeNull();
  });

  it("shows an agent run's transcripts, the one being written saying so", () => {
    const container = show(PartialEntityPanel, run);
    // The run, and the transcript still being written inside it; the finished one says nothing.
    expect(screen.getAllByText("In progress")).toHaveLength(2);
    expect(screen.getByText("2 transcripts")).toBeTruthy();
    expect([...container.querySelectorAll("summary")].map((node) => node.textContent).filter((text) => text?.startsWith("Transcript"))).toEqual([
      "Transcript · Setup · 2 messages", "Transcript · Session · 1 message · streaming…",
    ]);
  });

  it("says in one line how far each has got", () => {
    expect(show(PartialEntityInline, streaming).textContent).toBe("Transcript · 1 message · streaming…");
    expect(show(PartialEntityInline, transcript([message("user", "Hi") as unknown as EntityData])).textContent).toBe("Transcript · 1 message · in progress");
    expect(show(PartialEntityInline, streaming, { live: false }).textContent).toBe("Transcript · 1 message · message cut off");
    expect(show(PartialEntityInline, run).textContent).toBe("AgentRun · fix-bug · 2 transcripts · 1 streaming…");
    expect(show(PartialEntityInline, run, { live: false }).textContent).toBe("AgentRun · fix-bug · 2 transcripts · 1 cut off");
    // A transcript still going but writing nothing (its tool call out) is not streaming.
    const waiting = markPartial({ ...(run as object), transcripts: [finished, transcript([message("user", "Fix the bug") as unknown as EntityData])] } as unknown as EntityData);
    expect(show(PartialEntityInline, waiting).textContent).toBe("AgentRun · fix-bug · 2 transcripts · in progress");
    expect(show(PartialEntityInline, finished, { partial: false }).textContent).toBe("Transcript · Setup");
  });

  it("leaves a system prompt's preview to the generic views", () => {
    const prompt = { $type: SET_SYSTEM_MESSAGE, message: message("user", "Be brief") } as unknown as EntityData;
    expect(() => show(PartialEntityPanel, prompt)).not.toThrow();
    expect(screen.queryByText("In progress")).toBeNull();
  });
});
