import { EntityHoverPreview, EntityInlineLink, EntityPanel, renderer } from "fxtr-view";
import { BehaviorInlineLink, BehaviorPanel, BehaviorPreview } from "./entities";
import { CHAT_ROLLOUT_CONVERSATION, TRANSCRIPT_CONVERSATION } from "./selector";
import { AGENT_RUN, CHAT_ROLLOUT, SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP } from "./types";

export * from "./types";
export { CHAT_ROLLOUT_CONVERSATION, TRANSCRIPT_CONVERSATION, isMessageSelector, type MessageSelector } from "./selector";
export { BehaviorInlineLink, BehaviorPanel, BehaviorPreview, ChatRolloutView } from "./entities";
export { ComparedRollouts } from "./comparison";

/** Entity renderers shared by projects that store behaviors conversations. A `ChatRollout`'s and a
 *  `Transcript`'s conversation views read a `selector` (`MessageSelector`). */
export const behaviorsRenderers = [
  renderer(CHAT_ROLLOUT_CONVERSATION, BehaviorPanel),
  renderer(TRANSCRIPT_CONVERSATION, BehaviorPanel),
  ...[TRANSCRIPT_GROUP, AGENT_RUN, SET_SYSTEM_MESSAGE].map((type) => renderer(EntityPanel, type, "conversation", BehaviorPanel)),
  ...[CHAT_ROLLOUT, TRANSCRIPT, TRANSCRIPT_GROUP, AGENT_RUN, SET_SYSTEM_MESSAGE].flatMap((type) => [
    renderer(EntityInlineLink, type, "summary", BehaviorInlineLink),
    renderer(EntityHoverPreview, type, "summary", BehaviorPreview),
  ]),
];
