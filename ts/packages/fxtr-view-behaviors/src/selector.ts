// A selector into a conversation, the param of the conversation views of a `ChatRollout` and a
// `Transcript`: it picks one message, which the view marks and scrolls to. A link sets it (an
// entity holding a selector beside a reference to the conversation links there with it). It is
// provisional, and will give way to a fuller kind of selector.
//
// Nothing ties it to the Python side: a producer stores the same shape (a dataclass with a
// `message` int field), and the view ignores a value of another shape.

import { EntityPanel, declareView, type ParamValue } from "fxtr-view";
import { CHAT_ROLLOUT, TRANSCRIPT } from "./types";

/** One message of a conversation: its index among the messages the conversation view draws, in
 *  order. A `Transcript`'s are its messages; a `ChatRollout`'s are each turn's context messages and
 *  then its model's, turn by turn (its initialization is not counted). */
export type MessageSelector = { readonly message: number };

/** Whether a param's value is a `MessageSelector`. */
export function isMessageSelector(value: ParamValue | undefined): value is MessageSelector {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const message = (value as { readonly [key: string]: ParamValue })["message"];
  return typeof message === "number" && Number.isInteger(message) && message >= 0;
}

/** The conversation views reading a selector (`selector`), by the type they show. */
export const CHAT_ROLLOUT_CONVERSATION = declareView(EntityPanel, CHAT_ROLLOUT, "conversation", { params: ["selector"] });
export const TRANSCRIPT_CONVERSATION = declareView(EntityPanel, TRANSCRIPT, "conversation", { params: ["selector"] });
