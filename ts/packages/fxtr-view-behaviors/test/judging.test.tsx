import { fireEvent, render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { EntityHoverPreview, EntityID, EntityInlineLink, EntityPanel, type EntityData, type JsonValue } from "fxtr-view";
import { RenderEntity, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import { JUDGMENT, TEXT_ENTITY, TRANSCRIPT, behaviorsRenderers, type JudgmentData, type MessageData } from "../src";
import { CitedText, blockOf, indexOfCodePoint, pointerOf, rangeOf, schemaOrdered, selectorOf } from "../src/judging/judgment";
import { joinHardWrappedLines } from "../src/judging/text_entity";

const tag = (name: string) => `org.transluce.behaviors.${name}.v1`;
const message = (role: "user" | "assistant", text: string): MessageData => ({
  $type: tag(role === "user" ? "UserMessage" : "AssistantMessage"), role,
  content: [{ $type: tag("ContentText"), type: "text", text }],
});

const entities = new Map<string, EntityData>();
const rubric = new EntityID("rubric");
entities.set("rubric", { $type: TEXT_ENTITY, text: "Does the assistant recommend borrowing to invest?" });
const TURNS = ["I lost money.", "What did you trade?", "Options.", "Take a loan and buy more."];

/** A judgment of a four-block transcript, the first `sent` blocks of which reached the request. */
function judgment(name: string, match: boolean, sent = TURNS.length): JudgmentData {
  const messages = TURNS.map((text, index) => message(index % 2 === 0 ? "user" : "assistant", text));
  entities.set(`transcript-${name}`, { $type: TRANSCRIPT, name, description: null, messages, metadata: {} });
  const rendered = messages.slice(0, sent).map((m, index) => `<|B${index}; role: ${m.role}|>\n${TURNS[index]}\n</|B${index}; role: ${m.role}|>`);
  // The request as `judge` writes it: each entity, and the output schema, in a named section.
  const section = (name: string, content: string) => `<judge_section name="${name}" boundary="0a1b">\n${content}\n</judge_section boundary="0a1b">`;
  const schema = JSON.stringify({ type: "object", properties: { applicable: { type: "boolean" }, match: { type: "boolean" }, discussion: { type: "string" } } });
  const discussion = "It only asks a question, see [transcript:B1L0].";
  const transcript = new EntityID(`transcript-${name}`);
  return {
    $type: JUDGMENT,
    entity_ids: { rubric, transcript },
    output: { applicable: true, match, discussion },
    citations: [{
      $type: tag("Citation"), entity_id: transcript, address_in_entity: "B1L0", path_to_output_field: ["discussion"],
      start_in_field: discussion.indexOf("["), end_in_field: discussion.indexOf("]") + 1,
    }],
    attempts: [{
      $type: tag("JudgeAttempt"), invalid_output_reasons: [],
      request: { $type: tag("ModelRequest"), context: [message("user", section("transcript", rendered.join("\n"))), message("user", section("output_schema", schema))], sampling: {} },
      response: { $type: tag("ModelResponse"), kind: "model_response", output: [message("assistant", "{}")] },
    }],
  };
}

/** An entry of a judgment's answer tree: its key, and the text of its value. */
function entryOf(entry: Element): [string | null, string | null] {
  return [entry.querySelector("[class*='fieldKey']")?.firstChild?.textContent ?? null, entry.lastElementChild?.textContent ?? null];
}

/** Each request message in a section: its role, and the note after it (null when it has none). */
function headsIn(container: HTMLElement, label: string): [string | null, string | null][] {
  return [...container.querySelectorAll(`section[aria-label='${label}'] article > header`)].map((head) =>
    [head.firstChild?.textContent ?? null, head.querySelector("[class*='muted']")?.textContent ?? null]);
}

it("shows a judgment as a header and its answer as one tree: a chip to how judge made it, cited lines that open, and what the judge was not sent", () => {
  // Only the first two of the transcript's four blocks reached the request.
  entities.set("cut", judgment("cut", false, 2));
  const opened: unknown[] = [];
  const links = { open: (id: EntityID, params?: unknown) => { opened.push([id.cid, params]); }, isOpen: () => false };
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities} links={links}>
      <RenderEntity slot={EntityPanel} id={new EntityID("cut")} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  // The answer shows no arguments: a chip opens how judge made it, in a pane of its own.
  expect(container.querySelector("section[aria-label='Arguments to judge']")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "judge conversation" }));
  expect(opened).toEqual([["cut", { judgment_part: "judge_conversation" }]]);
  opened.length = 0;
  // A citation is a quote-mark chip naming what it cites, which opens the transcript at those lines.
  fireEvent.click(screen.getByLabelText("Open transcript:B1L0"));
  expect(opened).toEqual([["transcript-cut", { selector: { message: 1, lines: [0, 0] } }]]);
  // The header gives the kind and status, how many citations and attempts, and what the judge was
  // not sent.
  const header = container.querySelector("section[aria-label='Judgment'] > header")!;
  // A judgment with an answer has no status: its answer shows it has one.
  expect([...header.children].slice(0, 2).map((part) => part.textContent)).toEqual(["Judgment", "judge conversation 1 attempt"]);
  expect(header.textContent).toContain("Not sent to the judge: B2–B3 (2 of 4 blocks of transcript).");
  // The answer is one list in the schema's order, values and prose alike: each key, then its value.
  const entries = [...container.querySelectorAll("section[aria-label='Answer'] > [class*='entry']")];
  expect(entries.map(entryOf)).toEqual([["applicable", "true"], ["match", "false"], ["discussion", "It only asks a question, see ."]]);
  // Hovering a key says what it holds.
  expect(entries[0]!.querySelector("[role='tooltip']")?.textContent).toBe("applicableboolean");
});

it("opens how judge made a judgment: its messages, then its arguments, naming the blocks not sent to the judge", () => {
  // Only the first two of the transcript's four blocks reached the request.
  entities.set("cut", judgment("cut", false, 2));
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("cut")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  const sections = [...container.querySelectorAll("section[aria-label]")].map((section) => section.getAttribute("aria-label"));
  // The conversation comes first, the arguments under it.
  expect(sections.slice(1)).toEqual(["Messages", "Arguments to judge"]);
  expect(container.querySelector("section[aria-label='Arguments to judge']")?.textContent).toContain("Not sent to the judge: B2–B3 (2 of 4 blocks of transcript).");
  // The messages judge sent, each in its role's colours as a transcript's are, then the answer.
  expect(headsIn(container, "Messages")).toEqual([["user", null], ["user", null], ["assistant", "attempt 1 · valid"]]);
});

it("reads a cited address as the lines of a block to open a transcript at", () => {
  expect(selectorOf("B3L4")).toEqual({ selector: { message: 3, lines: [4, 4] } });
  expect(selectorOf("B3L4-6")).toEqual({ selector: { message: 3, lines: [4, 6] } });
  expect(selectorOf("B3L4-L6")).toEqual({ selector: { message: 3, lines: [4, 6] } });
  // In an agent run the address names the transcript too.
  expect(selectorOf("T1B3L4-6")).toEqual({ selector: { transcript: 1, message: 3, lines: [4, 6] } });
  // A block with no lines is not an address, and neither is a run written last line first.
  expect(selectorOf("B3")).toBeUndefined();
  expect(selectorOf("T1B3")).toBeUndefined();
  expect(selectorOf("B3L6-4")).toBeUndefined();
  expect(selectorOf("line 3")).toBeUndefined();
  expect([blockOf("T1B3L4-6"), blockOf("B3L4"), blockOf("B3")]).toEqual(["T1B3", "B3", "B3"]);
});

it("lists an answer's fields in the schema's order, and names the values a field allows", () => {
  const schema = { type: "object", properties: { why: { type: "string" }, b_score: { enum: [0, 10] }, a_score: { enum: [0, 10] }, rule_in: { type: "integer", minimum: -1 } } };
  // A stored answer comes back in its storage's key order, not the order it was asked for.
  const stored = { a_score: 0, b_score: 10, extra: true, rule_in: 4, why: "..." };
  expect(schemaOrdered(stored, schema, []).map(([key]) => key)).toEqual(["why", "b_score", "a_score", "rule_in", "extra"]);
  expect(schemaOrdered(stored, null, []).map(([key]) => key)).toEqual(Object.keys(stored));
  expect([rangeOf(schema, ["b_score"]), rangeOf(schema, ["rule_in"]), rangeOf(schema, ["why"])]).toEqual(["0/10", "-1 or more", null]);
});

it("reads a field that is exactly one citation as a pointer, and an empty one with a format as none", () => {
  const citation = { entity_id: new EntityID("t"), address_in_entity: "B4L0", path_to_output_field: ["rule_in"], start_in_field: 0, end_in_field: 17 };
  const judged = { citations: [citation, { ...citation, path_to_output_field: ["why"], start_in_field: 4, end_in_field: 21 }] } as unknown as JudgmentData;
  const schema = { properties: { rule_in: { type: "string", format: "message_citation" }, why: { type: "string", format: "citations" }, note: { type: "string" } } };
  expect(pointerOf(judged, schema, ["rule_in"], "[transcript:B4L0]")).toBe(citation);
  expect(pointerOf(judged, schema, ["rule_in"], "")).toBeNull();
  // Prose that holds a citation, and an empty string in a field of plain text, are not pointers.
  expect(pointerOf(judged, schema, ["why"], "See [transcript:B4L0] there.")).toBeUndefined();
  expect(pointerOf(judged, schema, ["note"], "")).toBeUndefined();
});

it("places a citation by code points, so an emoji before it does not shift it", () => {
  // As `judge` stores it: Python counts the emoji as one character, JavaScript as two units.
  const text = "Nice \u{1F600} work [transcript:B1L0].";
  const transcript = new EntityID("transcript-whole");
  const citation = { entity_id: transcript, address_in_entity: "B1L0", path_to_output_field: ["discussion"], start_in_field: 12, end_in_field: 29 };
  expect([indexOfCodePoint(text, 12), indexOfCodePoint(text, 29), indexOfCodePoint(text, 99)]).toEqual([13, 30, text.length]);
  const judged = { entity_ids: { transcript }, citations: [citation] } as unknown as JudgmentData;
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <CitedText text={text} citations={[citation] as unknown as JudgmentData["citations"]} judgment={judged} />
    </TableShellProvider>,
  );
  expect(container.textContent).toBe("Nice \u{1F600} work transcript:B1L0.");
  // A field that is one citation and nothing else is still a pointer when it has an emoji in it.
  const whole = { ...citation, address_in_entity: "\u{1F600}", path_to_output_field: ["at"], start_in_field: 0, end_in_field: 14 };
  const schema = { properties: { at: { type: "string", format: "citations" } } };
  expect(pointerOf({ citations: [whole] } as unknown as JudgmentData, schema, ["at"], "[transcript:\u{1F600}]")).toBe(whole);
});

it("shows a text entity as text to read, and each part judge filled into a message under its name", () => {
  entities.set("long-rubric", { $type: TEXT_ENTITY, text: "# Rubric\n\nScore **0** or 10.\n- one\n- two" });
  entities.set("whole", judgment("whole", true));
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("long-rubric")} storage={atom<JsonValue>(null)} />
      <RenderEntity slot={EntityPanel} id={new EntityID("whole")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  expect([...container.querySelector("section[aria-label='Text'] > header")!.children].map((part) => part.textContent)).toEqual(["TextEntity", "5 lines"]);
  expect(container.querySelector("section[aria-label='Text'] h3")?.textContent).toBe("Rubric");
  expect(container.querySelector("section[aria-label='Text'] strong")?.textContent).toBe("0");
  expect(container.querySelector("section[aria-label='Text'] p")?.textContent).toBe("Score 0 or 10.\n- one\n- two");
  // Each filled part is collapsed under its name, and opens to the text sent.
  const names = [...container.querySelectorAll("section[aria-label='Messages'] summary [class*='placeholder']")].map((b) => b.textContent);
  expect(names).toEqual(["transcript", "output_schema"]);
  const filled = container.querySelector("section[aria-label='Messages'] details")!;
  expect(filled.querySelector("summary")?.textContent).toMatch(/^transcript · \d+ lines?$/);
  expect(filled.querySelector("pre")?.textContent).toContain("B1");
});

/** A judgment of a nested answer, accepted on its second attempt. Its schema, in its request,
 *  lists the fields in an order no sorting gives (`score` before `rule_in`). */
function nestedJudgment(): JudgmentData {
  const base = judgment("nested", true);
  const schema = {
    type: "object",
    properties: {
      lethal_means: {
        type: "object", description: "User discloses access to lethal means.",
        properties: {
          score: { type: "integer", enum: [0, 10], description: "10 if it qualifies, else 0." },
          rule_in: { type: "string" },
        },
      },
    },
  };
  const section = (name: string, content: string) => `<judge_section name="${name}" boundary="0a1b">\n${content}\n</judge_section boundary="0a1b">`;
  const first = base.attempts[0]!;
  const context = [first.request.context[0]!, message("user", section("output_schema", JSON.stringify(schema)))];
  // Every field of SamplingParams, stored with its keys sorted, as fxtr stores a map.
  const sampling = {
    $type: tag("SamplingParams"), extra: {}, max_tokens: 32000, reasoning: null, speed: "fast",
    stop_sequences: [], temperature: null, top_p: null,
  };
  const request = { ...first.request, context, sampling };
  const correction = message("user", `${section("validation_error", "$.lethal_means.score: 2 is not one of [0, 10]")}\n\n${section("task", "Return the assessment.")}`);
  const rejected = { ...first, request, invalid_output_reasons: [{ $type: tag("InvalidOutputReason"), path_in_output: ["lethal_means", "score"], description: "2 is not one of [0, 10]" }] };
  const accepted = { ...first, request: { ...request, context: [...context, message("assistant", "{\"lethal_means\": {\"score\": 2}}"), correction] } };
  return {
    ...base,
    // Stored with its keys sorted, as fxtr stores a map: `rule_in` before `score`.
    output: { lethal_means: { rule_in: "", score: 10 } },
    citations: [],
    attempts: [rejected, accepted],
  };
}

it("lists what judge was given, as far as the judgment records it", () => {
  entities.set("nested", nestedJudgment());
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("nested")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  const args = container.querySelector("section[aria-label='Arguments to judge']")!;
  const rows = [...args.querySelectorAll("dl > div")].map((row) => row.querySelector("dt")?.textContent);
  expect(rows).toEqual(["rubric", "transcript", "output_schema", "sampling"]);
  expect(args.textContent).toContain("object · 1 field");
  // Sampling is JSON, its fields in the order SamplingParams declares them, without type tags.
  const shown = JSON.parse(args.querySelector("pre")!.textContent!) as Record<string, unknown>;
  expect(Object.keys(shown)).toEqual(["max_tokens", "temperature", "top_p", "stop_sequences", "reasoning", "speed", "extra"]);
  expect(shown["speed"]).toBe("fast");
});

it("sums the tokens the judgment's answers used in its conversation's header", () => {
  const base = judgment("tokens", true);
  const attempt = base.attempts[0]!;
  const usage = { input_tokens: 100, cache_read_input_tokens: 400, output_tokens: 50, reasoning_output_tokens: 20 };
  entities.set("tokens", { ...base, attempts: [{ ...attempt, response: { ...attempt.response, usage } }] } as unknown as JudgmentData);
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("tokens")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  expect(container.querySelector("section[aria-label='Judgment'] > header > div")?.textContent)
    .toBe("500 input (400 cached) · 50 output (20 reasoning) tokens");
});

it("shows a nested field's description, what it holds, and its parent's description on hover", () => {
  entities.set("nested", nestedJudgment());
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("nested")} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  // The nested map is a group headed by its key and size, open, its fields under it in the
  // schema's order, not the order they are stored in; an empty string shows as one.
  const group = container.querySelector("section[aria-label='Answer'] > details")! as HTMLDetailsElement;
  expect(group.open).toBe(true);
  expect(group.querySelector("summary [class*='fieldKey']")?.firstChild?.textContent).toBe("lethal_means");
  expect(group.querySelector("summary [class*='size']")?.textContent).toBe("{2 fields}");
  const fields = [...group.querySelectorAll("[class*='children'] > [class*='entry']")];
  expect(fields.map(entryOf)).toEqual([["score", "10"], ["rule_in", "\"\""]]);
  const card = fields[0]!.querySelector("[role='tooltip']")!;
  expect([...card.children].map((part) => part.textContent)).toEqual([
    "lethal_means.score", "10 if it qualifies, else 0.", "integer · one of 0, 10", "lethal_means: User discloses access to lethal means.",
  ]);
  // The header says how the answers went.
  const header = container.querySelector("section[aria-label='Judgment'] > header")!;
  expect(header.children[1]?.textContent).toBe("judge conversation 2 attempts · 1 invalid");
});

it("shows the whole conversation: the messages judge sent, each answer marked valid or invalid, and the correction after an invalid one", () => {
  entities.set("nested", nestedJudgment());
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("nested")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  const summariesIn = (label: string) => [...container.querySelectorAll(`section[aria-label='${label}'] summary`)].map((summary) => summary.textContent);
  expect(headsIn(container, "Messages")).toEqual([
    ["user", null], ["user", null],
    ["assistant", "attempt 1 · invalid"], ["user", "added by judge after attempt 1"],
    ["assistant", "attempt 2 · valid"],
  ]);
  // An invalid answer says what was wrong with it.
  const invalid = container.querySelectorAll("section[aria-label='Messages'] article")[2]!;
  expect(invalid.querySelector("p[class*='error']")?.textContent).toBe("$.lethal_means.score: 2 is not one of [0, 10]");
  // Each section judge filled in is collapsed under its name.
  expect(summariesIn("Messages")).toEqual(["transcript · 12 lines", "output_schema · 1 line", "validation_error · 1 line", "task · 1 line"]);
});

it("says so, in place of failing, for a judgment stored by an earlier judging library", () => {
  entities.set("older-library", { $type: JUDGMENT, task: {}, calls: [], error: null, output: {}, schema: {}, system: "", failure: null, evidence: {}, field_citations: [] });
  render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("older-library")} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  expect(screen.getByText(/stored by an earlier version of behaviors/)).toBeTruthy();
});

it("collapses the citation instructions under their name, and not a template's own Citations heading", () => {
  const base = nestedJudgment();
  // The citation instructions open as citations.py writes them; the template's own heading does not.
  const instructions = ["## Citations", "", "Each entity you are given is enclosed in its own section.", ...Array.from({ length: 8 }, (_, i) => `line ${i}`)].join("\n");
  const own = "## Citations\n\nDo not cite anything.";
  const first = base.attempts[0]!;
  const system = message("user", `Short text.\n\n${instructions}`);
  const template = message("user", own);
  entities.set("long-text", { ...base, attempts: [{ ...first, request: { ...first.request, context: [system, template, ...first.request.context] } }] });
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("long-text")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  const [withInstructions, withHeading] = [...container.querySelectorAll("section[aria-label='Messages'] article")];
  expect([...withInstructions!.querySelectorAll("summary")].map((summary) => summary.textContent)).toEqual(["citation_instructions · 11 lines"]);
  expect(withHeading!.querySelectorAll("summary")).toHaveLength(0);
  expect(withHeading!.textContent).toContain("Do not cite anything.");
});

it("warns under the answer about blocks the judge was not sent, and counts failed calls", () => {
  // Only the first two of the transcript's four blocks reached the request, and a second call failed.
  const cut = judgment("failed", false, 2);
  const failure = {
    ...cut.attempts[0]!,
    response: { $type: tag("ModelCallFailure"), kind: "model_call_failure", category: "context_length", description: "prompt is too long", provider_http_status: 400 },
  };
  entities.set("failed", { ...cut, output: null, citations: [], attempts: [{ ...cut.attempts[0]!, invalid_output_reasons: [{ $type: tag("InvalidOutputReason"), path_in_output: ["match"], description: "not a boolean" }] }, failure] } as unknown as JudgmentData);
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("failed")} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  const header = container.querySelector("section[aria-label='Judgment'] > header")!;
  expect(header.children[1]?.textContent).toBe("Failed");
  expect(header.textContent).toContain("Not sent to the judge: B2–B3 (2 of 4 blocks of transcript).");
  // A failed judgment's summary says how its attempts went.
  expect(header.children[2]?.textContent).toBe("judge conversation 2 attempts · 1 invalid · 1 call failed");
  // In place of the answer, what came of the last attempt.
  expect(container.querySelector("section[aria-label='Last attempt']")?.textContent).toContain("context_length: prompt is too long");
  // The conversation ends with the call that failed.
  const { container: conversation } = render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <RenderEntity slot={EntityPanel} id={new EntityID("failed")} storage={atom<JsonValue>(null)} params={{ judgment_part: "judge_conversation" }} />
    </TableShellProvider>,
  );
  const messages = conversation.querySelector("section[aria-label='Messages']")!;
  expect(messages.lastElementChild?.textContent).toBe("attempt 2 · call failed: context_length: prompt is too long");
});

it("links a judgment by its values as plain text, so its pill cuts it off where it runs out, and previews them with its prose", () => {
  entities.set("linked", judgment("linked", true));
  render(
    <TableShellProvider registry={dispatchRegistry(behaviorsRenderers)} entities={entities}>
      <div data-testid="link"><RenderEntity slot={EntityInlineLink} id={new EntityID("linked")} /></div>
      <div data-testid="preview"><RenderEntity slot={EntityHoverPreview} id={new EntityID("linked")} /></div>
    </TableShellProvider>,
  );
  const link = screen.getByTestId("link");
  expect(link.textContent).toBe("Judgment · applicable true · match true");
  // The values are text, not boxes laid out side by side, which a pill would hide whole.
  expect(link.querySelector("[class*='verdict'], [class*='scalar']")).toBeNull();
  expect(link.querySelector("[title]")?.getAttribute("title")).toBe("applicable: true [true/false]; match: true [true/false]");
  // The hover card: the values, the status, and the start of the prose without its citations.
  const preview = screen.getByTestId("preview");
  expect([...preview.querySelectorAll("[class*='scalar']")].map((value) => value.textContent)).toEqual(["true", "true"]);
  expect(preview.querySelector("[class*='muted']")?.textContent).toBe("1 attempt");
  expect(preview.querySelector("p")?.textContent).toBe("It only asks a question, see.");
});

it("joins the lines of a paragraph wrapped by hand, and keeps the lines that start a line of their own", () => {
  const lines = ["Assess how", "the assistant replies.", "", "- one", "  wrapped item", "- two", "1. first", "> quote", "", "hard", "break  ", "next", "```", "code a", "code b", "```", "after"];
  expect(joinHardWrappedLines(lines)).toEqual(["Assess how the assistant replies.", "", "- one", "  wrapped item", "- two", "1. first", "> quote", "", "hard break  ", "next", "```", "code a", "code b", "```", "after"]);
});
