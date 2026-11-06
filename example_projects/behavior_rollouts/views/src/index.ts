import { EntityPanel, InvocationPanel, renderer } from "fxtr-view";
import { LONGEST_REPLY, LongestReplyPanel } from "./longest";
import { behaviorsRenderers } from "fxtr-view-behaviors";
import { OpenAIConversationsPanel } from "./invocations";
import { OpenAIRolloutComparisonPanel } from "./comparison";
import { CONVERSATIONS, OPENAI_EXPERIMENT } from "./views";

export { OPENAI_CONVERSATION, OPENAI_EXPERIMENT } from "./views";

export const rolloutRenderers = [
  ...behaviorsRenderers,
  renderer(EntityPanel, LONGEST_REPLY, "longest", LongestReplyPanel),
  renderer(CONVERSATIONS, OpenAIConversationsPanel),
  renderer(InvocationPanel, OPENAI_EXPERIMENT, "conversation", OpenAIConversationsPanel),
  renderer(InvocationPanel, OPENAI_EXPERIMENT, "compare", OpenAIRolloutComparisonPanel),
];
