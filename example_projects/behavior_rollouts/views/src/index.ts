import { InvocationPanel, renderer } from "fxtr-view";
import { behaviorsRenderers } from "fxtr-view-behaviors";
import { OpenAIConversationsPanel } from "./invocations";
import { OpenAIRolloutComparisonPanel } from "./comparison";
import { CONVERSATIONS, OPENAI_EXPERIMENT } from "./views";

export { OPENAI_CONVERSATION, OPENAI_EXPERIMENT } from "./views";

export const rolloutRenderers = [
  ...behaviorsRenderers,
  renderer(CONVERSATIONS, OpenAIConversationsPanel),
  renderer(InvocationPanel, OPENAI_EXPERIMENT, "conversation", OpenAIConversationsPanel),
  renderer(InvocationPanel, OPENAI_EXPERIMENT, "compare", OpenAIRolloutComparisonPanel),
];
