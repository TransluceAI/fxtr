import { EntityHoverPreview, EntityInlineLink, EntityPanel, renderer } from "fxtr-view";
import { BehaviorInlineLink, BehaviorPanel, BehaviorPreview } from "./entities";
import { AGENT_RUN, CHAT_ROLLOUT, SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP } from "./types";

export * from "./types";
export { BehaviorInlineLink, BehaviorPanel, BehaviorPreview, ChatRolloutView } from "./entities";
export { ComparedRollouts } from "./comparison";

/** Entity renderers shared by projects that store behaviors conversations. */
export const behaviorsRenderers = [
  ...[CHAT_ROLLOUT, TRANSCRIPT, TRANSCRIPT_GROUP, AGENT_RUN, SET_SYSTEM_MESSAGE].flatMap((type) => [
    renderer(EntityPanel, type, "conversation", BehaviorPanel),
    renderer(EntityInlineLink, type, "summary", BehaviorInlineLink),
    renderer(EntityHoverPreview, type, "summary", BehaviorPreview),
  ]),
];
