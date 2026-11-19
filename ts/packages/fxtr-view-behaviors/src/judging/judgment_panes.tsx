// Two parts of a judgment that open in panes of their own (judgment_pane_params.ts): the
// conversation and the output schema. Each starts with a header as the judgment's panel does, its
// title naming the part, then its sections, each under a small heading, as a transcript's turns
// are.
//
// The conversation pane shows how `judge` made the judgment. Its header says how many tokens the
// answers used; each answer in the conversation says how it went. The whole conversation with the model comes first, as one list of messages: the messages `judge` filled in, each answer the model gave,
// marked valid or invalid with what was wrong, the correction `judge` sent after each invalid
// answer, and a call that failed. Each section `judge` filled in is collapsed under its name
// (request_sections.tsx). The arguments `judge` was given follow (judge_arguments.tsx): the
// entities, the output schema and the sampling parameters.

import shared from "../behaviors.module.css";
import type { EntityID } from "fxtr-view";
import { countLabel } from "../entities";
import { usageLine } from "../messages";
import { reasonsOf } from "./attempt_outcome";
import { JudgeArguments } from "./judge_arguments";
import { ConversationMessage } from "./request_sections";
import { outputSchemaOf } from "./output_schema";
import styles from "./judging.module.css";
import type { AttemptData, JudgmentData } from "./types";
import type { MessageData } from "../types";

/** What came of an attempt: its answer was valid (`judge` kept it), invalid (`judge` sent it back
 *  with what was wrong), or the model call failed and there was no answer. */
function outcomeOf(attempt: AttemptData): "valid" | "invalid" | "call failed" {
  if (attempt.response.kind === "model_call_failure") return "call failed";
  return reasonsOf(attempt).length === 0 ? "valid" : "invalid";
}

/** How a judgment's attempts went, in a line: how many there were, then how many answers were
 *  invalid and how many calls failed, when any were (`3 attempts · 1 invalid · 1 call failed`). */
export function attemptsLine(judgment: JudgmentData): string {
  const outcomes = judgment.attempts.map(outcomeOf);
  const invalid = outcomes.filter((outcome) => outcome === "invalid").length;
  const failed = outcomes.filter((outcome) => outcome === "call failed").length;
  return [
    countLabel(outcomes.length, "attempt"),
    ...(invalid > 0 ? [`${invalid} invalid`] : []),
    ...(failed > 0 ? [failed === 1 ? "1 call failed" : `${failed} calls failed`] : []),
  ].join(" · ");
}

/** One entry of a judgment's conversation: a message with its note and, for an invalid answer,
 *  what was wrong; or a call that failed, which has no message. */
type ConversationEntry =
  | { readonly message: MessageData; readonly note?: string; readonly problems?: readonly string[] }
  | { readonly failure: string; readonly note: string };

/** A judgment's whole conversation with the model, in order. Each attempt's request is the one
 *  before it, then that attempt's answer, then the correction `judge` sent; so the first request
 *  is the messages `judge` filled in, each later request adds an answer and a correction, and the
 *  last attempt's answer (or its failed call) ends it. */
export function conversationOf(judgment: JudgmentData): ConversationEntry[] {
  const attempts = judgment.attempts;
  const first = attempts[0];
  if (first === undefined) return [];
  const answer = (attempt: AttemptData, index: number) => {
    const outcome = outcomeOf(attempt);
    return { note: `attempt ${index + 1} · ${outcome}`, problems: outcome === "invalid" ? reasonsOf(attempt) : [] };
  };
  const entries: ConversationEntry[] = first.request.context.map((message) => ({ message }));
  attempts.forEach((attempt, index) => {
    const next = attempts[index + 1];
    if (next !== undefined) {
      // What the next request added: this attempt's answer, then the correction after it.
      const added = next.request.context.slice(attempt.request.context.length);
      added.slice(0, -1).forEach((message) => entries.push({ message, ...answer(attempt, index) }));
      const correction = added.at(-1);
      if (correction !== undefined) entries.push({ message: correction, note: `added by judge after attempt ${index + 1}` });
    } else if (attempt.response.kind === "model_response") {
      attempt.response.output.forEach((message) => entries.push({ message, ...answer(attempt, index) }));
    } else {
      entries.push({ failure: `${attempt.response.category}: ${attempt.response.description}`, note: `attempt ${index + 1} · call failed` });
    }
  });
  return entries;
}

/** How `judge` made the judgment: its header with the tokens its answers used, its whole
 *  conversation with the model (where each answer says how it went), then the arguments `judge` was given. */
export function JudgeConversationView({ judgment, id }: { judgment: JudgmentData; id: EntityID }) {
  const conversation = conversationOf(judgment);
  // The tokens the answers used. A failed call has no response, and is not counted.
  const usage = usageLine(judgment.attempts.flatMap((attempt) => (attempt.response.kind === "model_response" ? [attempt.response["usage"]] : [])));
  return (
    <>
      <header className={shared["header"]}>
        <span className={shared["kind"]}>Judgment</span>
        <h2>Judge conversation</h2>
        {usage !== null && <div className={shared["muted"]}>{usage}</div>}
      </header>
      <section className={shared["exchange"]} aria-label="Messages">
        <h3 className={shared["turn-number"]}>Messages</h3>
        {conversation.length === 0 && <p className={shared["muted"]}>This judgment recorded no request.</p>}
        {conversation.map((entry, index) => "failure" in entry
          ? (
            <p className={shared["error"]} key={index}>
              <span className={shared["muted"]}>{entry.note}:</span> {entry.failure}
            </p>
          )
          : <ConversationMessage message={entry.message} {...(entry.note === undefined ? {} : { note: entry.note })} problems={entry.problems ?? []} key={index} />)}
      </section>
      <JudgeArguments judgment={judgment} id={id} />
    </>
  );
}

/** The output schema as formatted JSON. */
export function SchemaView({ judgment }: { judgment: JudgmentData }) {
  const schema = outputSchemaOf(judgment);
  const text = schema === null ? null : JSON.stringify(schema, null, 2);
  return (
    <>
      <header className={shared["header"]}>
        <span className={shared["kind"]}>Judgment</span>
        <h2>Output schema</h2>
        {text !== null && <div className={shared["muted"]}>JSON Schema · {countLabel(text.split("\n").length, "line")}</div>}
      </header>
      {text === null
        ? <p className={shared["muted"]}>This judgment records no output schema.</p>
        : <section aria-label="Output schema"><pre className={styles["json"]}>{text}</pre></section>}
    </>
  );
}
