// A selector into a conversation, the param of the conversation views of a `ChatRollout`, a
// `Transcript` and an `AgentRun`: it picks one message, which the view marks and scrolls to, and
// may pick lines of that message, which the view highlights. In an agent run it also says which
// transcript the message is in. A link sets it (an
// entity holding a selector beside a reference to the conversation links there with it). It is
// provisional, and will give way to a fuller kind of selector.
//
// Nothing ties it to the Python side: a producer stores the same shape (a dataclass with a
// `message` int field), and the view ignores a value of another shape.

import { EntityPanel, declareView, type ParamValue } from "fxtr-view";
import { AGENT_RUN, CHAT_ROLLOUT, TRANSCRIPT } from "./types";

/** One message of a conversation: its index among the messages the conversation view draws, in
 *  order. A `Transcript`'s are its messages; a `ChatRollout`'s are each turn's context messages and
 *  then its model's, turn by turn (its initialization is not counted). */
export type MessageSelector = {
  readonly message: number;
  /** The first and last of the lines picked within the message, counting from 0 the lines a judge
   *  can cite (`lineStarts` in messages.tsx: each non-empty line of its text, and each tool call). */
  readonly lines?: readonly [number, number];
  /** In an agent run, which transcript the message is in: its place among the run's transcripts,
   *  the top-level ones first, then each group's, a group before the groups nested in it. The
   *  first when left out. */
  readonly transcript?: number;
};

/** The transcript of an agent run a selector picks: 0 when it names none. */
export function selectedTranscript(selector: MessageSelector): number {
  const transcript: unknown = selector.transcript;
  return typeof transcript === "number" && Number.isInteger(transcript) && transcript >= 0 ? transcript : 0;
}

/** The lines a selector picks, or null when it picks the whole message or names lines badly. */
export function selectedLines(selector: MessageSelector): readonly [number, number] | null {
  const lines: unknown = selector.lines;
  if (!Array.isArray(lines) || lines.length !== 2) return null;
  const [first, last] = lines as unknown[];
  if (typeof first !== "number" || typeof last !== "number") return null;
  return Number.isInteger(first) && Number.isInteger(last) && 0 <= first && first <= last ? [first, last] : null;
}

/** Whether a param's value is a `MessageSelector`. */
export function isMessageSelector(value: ParamValue | undefined): value is MessageSelector {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const message = (value as { readonly [key: string]: ParamValue })["message"];
  return typeof message === "number" && Number.isInteger(message) && message >= 0;
}

/** The conversation views reading a selector (`selector`), by the type they show. */
export const CHAT_ROLLOUT_CONVERSATION = declareView(EntityPanel, CHAT_ROLLOUT, "conversation", { params: ["selector"] });
export const TRANSCRIPT_CONVERSATION = declareView(EntityPanel, TRANSCRIPT, "conversation", { params: ["selector"] });
export const AGENT_RUN_CONVERSATION = declareView(EntityPanel, AGENT_RUN, "conversation", { params: ["selector"] });
