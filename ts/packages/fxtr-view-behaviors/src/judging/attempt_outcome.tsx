// What came of one attempt of a judgment: the failed call, or why its answer was rejected.

import shared from "../behaviors.module.css";
import type { MessageData } from "../types";
import type { AttemptData, InvalidOutputReasonData } from "./types";

function textOf(messages: readonly MessageData[]): string {
  return messages
    .map((message) => message.content.map((part) => (part.type === "text" ? part.text : "")).join(""))
    .join("\n\n");
}

/** One reason as `judge` tells it to the model: the part of the answer as a path from `$`, then
 *  what is wrong there. A reason about the answer as a whole is its description alone. */
function reasonText(reason: InvalidOutputReasonData): string {
  if (reason.path_in_output.length === 0) return reason.description;
  const where = reason.path_in_output.map((key) => (typeof key === "number" ? `[${key}]` : `.${key}`)).join("");
  return `$${where}: ${reason.description}`;
}

/** Why an attempt's answer was not accepted, a line a reason: from the list of reasons, or from
 *  the one text an attempt stored without the list holds. Empty for an accepted answer. */
export function reasonsOf(attempt: AttemptData): string[] {
  const listed = (attempt as { readonly invalid_output_reasons?: readonly InvalidOutputReasonData[] }).invalid_output_reasons;
  if (listed !== undefined) return listed.map(reasonText);
  const text = (attempt as { readonly invalid_output_reason?: string | null }).invalid_output_reason;
  return text ? [text] : [];
}

/** What came of one attempt: the failed call, or why its answer was rejected and the answer. */
export function AttemptOutcome({ attempt }: { attempt: AttemptData }) {
  if (attempt.response.kind === "model_call_failure") {
    return <p className={shared["error"]}>{attempt.response.category}: {attempt.response.description}</p>;
  }
  const reasons = reasonsOf(attempt);
  return (
    <div>
      {reasons.map((reason, i) => <p className={shared["error"]} key={i}>{reason}</p>)}
      <details className={shared["details"]}>
        <summary>The model's answer</summary>
        <pre>{textOf(attempt.response.output)}</pre>
      </details>
    </div>
  );
}
