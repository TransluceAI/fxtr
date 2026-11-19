import { EntityInlineLink, EntityPanel, InvocationPanel, renderer } from "fxtr-view";
import { LONGEST_REPLY, LongestReplyPanel } from "./echo";
import { behaviorsRenderers } from "fxtr-view-behaviors";
import { InstructionsInForceConversationsPanel } from "./instructions_in_force/conversations";
import { InstructionsInForceComparisonPanel } from "./instructions_in_force/comparison";
import { InstructionsInForceOverviewPanel } from "./instructions_in_force/overview";
import {
  BELIEF_REINFORCEMENT_CONVERSATION_STEP, BELIEF_REINFORCEMENT_WORKFLOW, CONVERSATIONS, INSTRUCTIONS_IN_FORCE_WORKFLOW,
} from "./views";
import { SAMPLING_PARAMS_ENTITY, SamplingParamsInline, SamplingParamsPanel } from "./sampling";
import {
  BELIEF_REINFORCEMENT_REPORT, BeliefReinforcementConversations, BeliefReinforcementOverview,
  BeliefReinforcementReportInline, BeliefReinforcementReportPanel,
} from "./belief_reinforcement";

export {
  BELIEF_REINFORCEMENT_CONVERSATION_STEP, BELIEF_REINFORCEMENT_WORKFLOW, INSTRUCTIONS_IN_FORCE_CONVERSATION_STEP,
  INSTRUCTIONS_IN_FORCE_WORKFLOW,
} from "./views";

export const rolloutRenderers = [
  ...behaviorsRenderers,
  renderer(EntityPanel, LONGEST_REPLY, "longest", LongestReplyPanel),
  renderer(CONVERSATIONS, InstructionsInForceConversationsPanel),
  renderer(InvocationPanel, INSTRUCTIONS_IN_FORCE_WORKFLOW, "overview", InstructionsInForceOverviewPanel),
  renderer(InvocationPanel, INSTRUCTIONS_IN_FORCE_WORKFLOW, "conversation", InstructionsInForceConversationsPanel),
  renderer(InvocationPanel, INSTRUCTIONS_IN_FORCE_WORKFLOW, "compare", InstructionsInForceComparisonPanel),
  renderer(EntityPanel, SAMPLING_PARAMS_ENTITY, "sampling", SamplingParamsPanel),
  renderer(EntityInlineLink, SAMPLING_PARAMS_ENTITY, "sampling", SamplingParamsInline),
  renderer(EntityPanel, BELIEF_REINFORCEMENT_REPORT, "belief_reinforcement.report", BeliefReinforcementReportPanel),
  renderer(EntityInlineLink, BELIEF_REINFORCEMENT_REPORT, "belief_reinforcement.report", BeliefReinforcementReportInline),
  renderer(InvocationPanel, BELIEF_REINFORCEMENT_WORKFLOW, "belief_reinforcement.overview", BeliefReinforcementOverview),
  renderer(
    InvocationPanel, BELIEF_REINFORCEMENT_CONVERSATION_STEP, "belief_reinforcement.conversation",
    BeliefReinforcementConversations,
  ),
];
