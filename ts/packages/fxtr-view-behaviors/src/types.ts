import type { DataMap, EntityData } from "fxtr-view";

export const CHAT_ROLLOUT = "org.transluce.behaviors.ChatRollout.v2";
export const CONTEXT_TURN = "org.transluce.behaviors.ContextTurn.v2";
export const MODEL_TURN = "org.transluce.behaviors.ModelTurn.v2";
export const END_ROLLOUT = "org.transluce.behaviors.EndRollout.v2";
export const TRANSCRIPT = "org.transluce.behaviors.Transcript.v1";
export const TRANSCRIPT_GROUP = "org.transluce.behaviors.TranscriptGroup.v1";
export const AGENT_RUN = "org.transluce.behaviors.AgentRun.v1";
export const SET_SYSTEM_MESSAGE = "org.transluce.behaviors.SetSystemMessage.v1";
export const MODEL_RESPONSE = "org.transluce.behaviors.ModelResponse.v1";

/** Wire shapes of behaviors/types. Tagged inline records are maps, not entity references. */

/** Rollout and turn metadata: one tagged record per entry, its type chosen by the producer
 *  (e.g. a `ModelResponse` that a model recorded on its turn). */
export type MetadataEntries = readonly EntityData[];
export interface TextContent extends EntityData {
  readonly type: "text" | "reasoning" | "summarized_reasoning";
  readonly text: string;
}
export interface RedactedContent extends EntityData {
  readonly type: "redacted_reasoning";
  readonly data: string;
}
export interface ToolContent extends EntityData {
  readonly type: "tool_use";
  readonly id: string;
  readonly name: string;
  readonly input: DataMap;
  readonly parse_error: string | null;
}
export type ContentData = TextContent | RedactedContent | ToolContent;

export interface MessageData extends EntityData {
  readonly role: "system" | "user" | "assistant" | "tool";
  readonly content: readonly ContentData[];
}
export interface TurnData extends EntityData {
  readonly $type: typeof CONTEXT_TURN | typeof MODEL_TURN;
  readonly messages: readonly MessageData[];
  readonly metadata: MetadataEntries;
}
export type RolloutStatus =
  | (EntityData & { readonly kind: "rollout_completed" })
  | (EntityData & { readonly kind: "rollout_max_turns"; readonly max_turns: number })
  | (EntityData & {
      readonly kind: "rollout_failed";
      readonly failure: EntityData & { readonly category: string; readonly description: string };
    });

/** `initialization` is the initialization's own tagged record, or null if there was none.
 *  A reader names the initialization types it handles. */
export type ChatRolloutData = EntityData & {
  readonly $type: typeof CHAT_ROLLOUT;
  readonly initialization: EntityData | null;
  readonly context_turns: readonly TurnData[];
  readonly model_turns: readonly TurnData[];
  readonly status: RolloutStatus;
  readonly end: (EntityData & { readonly $type: typeof END_ROLLOUT; readonly metadata: MetadataEntries }) | null;
  readonly metadata: MetadataEntries;
};
export interface TranscriptData extends EntityData {
  readonly $type: typeof TRANSCRIPT;
  readonly name: string | null;
  readonly description: string | null;
  readonly messages: readonly MessageData[];
  readonly metadata: DataMap;
}
export interface TranscriptGroupData extends EntityData {
  readonly $type: typeof TRANSCRIPT_GROUP | typeof AGENT_RUN;
  readonly name: string | null;
  readonly description: string | null;
  readonly transcripts: readonly TranscriptData[];
  readonly groups: readonly TranscriptGroupData[];
  readonly metadata: DataMap;
}
export interface SystemMessageData extends EntityData {
  readonly $type: typeof SET_SYSTEM_MESSAGE;
  readonly message: MessageData;
}
export type BehaviorData = ChatRolloutData | TranscriptData | TranscriptGroupData | SystemMessageData;
