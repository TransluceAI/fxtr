import { EntityHoverPreview, EntityInlineLink, EntityPanel, PartialEntityInline, PartialEntityPanel, declareView, renderer } from "fxtr-view";
import { BehaviorInlineLink, BehaviorPanel, BehaviorPartialInline, BehaviorPartialPanel, BehaviorPreview } from "./entities";
import { JudgmentInlineLink, JudgmentPanel, JudgmentPreview } from "./judging/judgment";
import { JUDGMENT_PART } from "./judging/judgment_pane_params";
import { TextPanel } from "./judging/text_entity";
import { JUDGMENT, TEXT_ENTITY } from "./judging/types";
import { AGENT_RUN_CONVERSATION, CHAT_ROLLOUT_CONVERSATION, TRANSCRIPT_CONVERSATION } from "./selector";
import { AGENT_RUN, CHAT_ROLLOUT, SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP } from "./types";

export * from "./types";
export {
  AGENT_RUN_CONVERSATION, CHAT_ROLLOUT_CONVERSATION, TRANSCRIPT_CONVERSATION,
  isMessageSelector, selectedLines, selectedTranscript, type MessageSelector,
} from "./selector";
export {
  BehaviorInlineLink,
  BehaviorPanel,
  BehaviorPartialInline,
  BehaviorPartialPanel,
  BehaviorPreview,
  ChatRolloutView,
  partialChatRollout,
  partialGroup,
  partialTranscript,
  type RolloutProgress,
  type StreamProgress,
} from "./entities";
export { ComparedRollouts } from "./comparison";
export * from "./judging/types";
export { JudgmentDetail, JudgmentInlineLink, JudgmentPanel } from "./judging/judgment";
export { JUDGE_CONVERSATION_PARAMS, JUDGMENT_PART, SCHEMA_PARAMS } from "./judging/judgment_pane_params";
export { TextBody, TextPanel } from "./judging/text_entity";

/** Entity renderers shared by projects that store behaviors conversations or judgments: the
 *  conversation view and one-line summary of a rollout, transcript, or group of transcripts (also
 *  for one a step is still producing, its live preview), a `Judgment`'s panel, link and hover
 *  preview, and a `TextEntity`'s panel (a rubric, a judge's instructions) as text to read. A
 *  `ChatRollout`'s, a `Transcript`'s and an `AgentRun`'s conversation views read a `selector`
 *  (`MessageSelector`); a judgment's panel reads `judgment_part` to show its conversation or its output
 *  schema alone (judging/judgment_pane_params.ts). A project registers these in its own view bundle. */
export const behaviorsRenderers = [
  renderer(CHAT_ROLLOUT_CONVERSATION, BehaviorPanel),
  renderer(TRANSCRIPT_CONVERSATION, BehaviorPanel),
  renderer(AGENT_RUN_CONVERSATION, BehaviorPanel),
  ...[TRANSCRIPT_GROUP, SET_SYSTEM_MESSAGE].map((type) => renderer(EntityPanel, type, "conversation", BehaviorPanel)),
  ...[CHAT_ROLLOUT, TRANSCRIPT, TRANSCRIPT_GROUP, AGENT_RUN, SET_SYSTEM_MESSAGE].flatMap((type) => [
    renderer(EntityInlineLink, type, "summary", BehaviorInlineLink),
    renderer(EntityHoverPreview, type, "summary", BehaviorPreview),
  ]),
  // A system prompt is never produced bit by bit, so its previews show as the generic data tree.
  ...[CHAT_ROLLOUT, TRANSCRIPT, TRANSCRIPT_GROUP, AGENT_RUN].flatMap((type) => [
    renderer(PartialEntityPanel, type, "conversation", BehaviorPartialPanel),
    renderer(PartialEntityInline, type, "summary", BehaviorPartialInline),
  ]),
  renderer(declareView(EntityPanel, JUDGMENT, "judgment", { params: [JUDGMENT_PART] }), JudgmentPanel),
  renderer(EntityInlineLink, JUDGMENT, "summary", JudgmentInlineLink),
  renderer(EntityHoverPreview, JUDGMENT, "summary", JudgmentPreview),
  renderer(EntityPanel, TEXT_ENTITY, "text", TextPanel),
];
