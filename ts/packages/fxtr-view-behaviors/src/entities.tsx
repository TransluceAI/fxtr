import styles from "./behaviors.module.css";
import { unwrap, useEntity, type EntityData, type EntityID, type EntityViewProps } from "fxtr-view";
import { Messages, Metadata, MetadataList, usageOf } from "./messages";
import {
  AGENT_RUN, CHAT_ROLLOUT, SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP,
  type BehaviorData, type ChatRolloutData, type MessageData, type RolloutStatus,
  type SystemMessageData, type TranscriptData, type TranscriptGroupData,
} from "./types";

export function statusLabel(status: RolloutStatus): string {
  switch (status.kind) {
    case "rollout_completed": return "Completed";
    case "rollout_max_turns": return `Turn limit reached (${status.max_turns})`;
    case "rollout_failed": return `Failed · ${status.failure.category}`;
  }
}

function countLabel(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function groupCount(group: TranscriptGroupData): number {
  return group.transcripts.length + group.groups.reduce((total, child) => total + groupCount(child), 0);
}

function kindOf(data: BehaviorData): string {
  switch (data.$type) {
    case CHAT_ROLLOUT: return "ChatRollout";
    case TRANSCRIPT: return "Transcript";
    case TRANSCRIPT_GROUP: return "TranscriptGroup";
    case AGENT_RUN: return "AgentRun";
    case SET_SYSTEM_MESSAGE: return "SetSystemMessage";
  }
}

function summaryOf(data: BehaviorData): string {
  switch (data.$type) {
    case CHAT_ROLLOUT: return `${countLabel(data.model_turns.length, "response")} · ${statusLabel(data.status)}`;
    case TRANSCRIPT: return data.name || countLabel(data.messages.length, "message");
    case TRANSCRIPT_GROUP:
    case AGENT_RUN: return `${data.name ? `${data.name} · ` : ""}${countLabel(groupCount(data), "transcript")}`;
    case SET_SYSTEM_MESSAGE: return "System prompt";
  }
}

function firstText(messages: readonly MessageData[]): string {
  for (const message of messages) {
    const block = message.content.find((part) => part.type === "text" && part.text);
    if (block?.type === "text") return block.text;
  }
  return "";
}

export function Initialization({ data }: { data: EntityData | null }) {
  return <section className={styles["exchange"]} aria-label="Session initialization">
    <h3 className={styles["turn-number"]}>{data?.$type === SET_SYSTEM_MESSAGE ? "System prompt" : "Session initialization"}</h3>
    {data === null ? <p className={styles["muted"]}>No session initialization.</p>
      : data.$type === SET_SYSTEM_MESSAGE ? <Messages messages={[(data as SystemMessageData).message]} />
      : <Metadata value={data} label="Initialization data" />}
  </section>;
}

export function ChatRolloutView({ data }: { data: ChatRolloutData }) {
  const usage = usageOf(data.model_turns);
  const turnCount = Math.max(data.context_turns.length, data.model_turns.length);
  return (
    <section className={styles["panel"]} aria-label="Chat rollout">
      <header className={styles["header"]}>
        <span className={styles["kind"]}>ChatRollout</span>
        <span className={data.status.kind === "rollout_failed" ? styles["error"] : styles["muted"]}>
          {statusLabel(data.status)}
        </span>
        <h2>Conversation</h2>
        <div className={styles["muted"]}>{countLabel(data.model_turns.length, "response")}{usage && ` · ${usage}`}</div>
      </header>
      <Initialization data={data.initialization} />
      {turnCount === 0 && <p className={styles["muted"]}>No turns recorded.</p>}
      {Array.from({ length: turnCount }, (_, index) => {
        const context = data.context_turns[index];
        const model = data.model_turns[index];
        return (
          <section className={styles["exchange"]} aria-label={`Turn ${index + 1}`} key={index}>
            <h3 className={styles["turn-number"]}>Turn {index + 1}</h3>
            {context && <Messages messages={context.messages} />}
            {model ? <Messages messages={model.messages} /> : <p className={styles["muted"]}>No model response recorded.</p>}
            {(context || model) && (
              <details className={styles["details"]}>
                <summary>Turn details</summary>
                {context && <MetadataList entries={context.metadata} label="Context metadata" />}
                {Array.isArray(context?.["available_tools"]) && (
                  <Metadata value={{ tools: context["available_tools"] }} label="Available tools" />
                )}
                {model && <MetadataList entries={model.metadata} label="Response metadata" />}
              </details>
            )}
          </section>
        );
      })}
      {data.status.kind === "rollout_failed" && <p className={styles["error"]}>{data.status.failure.description}</p>}
      {data.end && <MetadataList entries={data.end.metadata} label="Completion metadata" />}
      <MetadataList entries={data.metadata} label="Metadata" />
    </section>
  );
}

function TranscriptView({ data }: { data: TranscriptData }) {
  return (
    <section className={styles["panel"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>Transcript</span>
        <h2>{data.name || "Conversation"}</h2>
        {data.description && <p className={styles["prose"]}>{data.description}</p>}
        <div className={styles["muted"]}>{countLabel(data.messages.length, "message")}</div>
      </header>
      {data.messages.length ? <Messages messages={data.messages} /> : <p className={styles["muted"]}>No messages recorded.</p>}
      <Metadata value={data.metadata} />
    </section>
  );
}

function GroupView({ data }: { data: TranscriptGroupData }) {
  return (
    <section className={styles["panel"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>{kindOf(data)}</span>
        <h2>{data.name || "Conversations"}</h2>
        {data.description && <p className={styles["prose"]}>{data.description}</p>}
        <div className={styles["muted"]}>{countLabel(groupCount(data), "transcript")}</div>
      </header>
      {data.transcripts.length === 0 && data.groups.length === 0 && <p className={styles["muted"]}>No transcripts recorded.</p>}
      {data.transcripts.map((transcript, index) => (
        <details className={styles["collection"]} key={index}>
          <summary>Transcript · {transcript.name || `Conversation ${index + 1}`} · {countLabel(transcript.messages.length, "message")}</summary>
          <TranscriptView data={transcript} />
        </details>
      ))}
      {data.groups.map((group, index) => (
        <details className={styles["collection"]} key={index}>
          <summary>TranscriptGroup · {group.name || `Group ${index + 1}`} · {countLabel(groupCount(group), "transcript")}</summary>
          <GroupView data={group} />
        </details>
      ))}
      <Metadata value={data.metadata} />
    </section>
  );
}

export function BehaviorPanel({ id }: EntityViewProps) {
  const data = unwrap(useEntity(id as EntityID<BehaviorData>));
  switch (data.$type) {
    case CHAT_ROLLOUT: return <ChatRolloutView data={data} />;
    case TRANSCRIPT: return <TranscriptView data={data} />;
    case TRANSCRIPT_GROUP:
    case AGENT_RUN: return <GroupView data={data} />;
    case SET_SYSTEM_MESSAGE:
      return <section className={styles["panel"]}><span className={styles["kind"]}>SetSystemMessage</span><Messages messages={[data.message]} /></section>;
  }
}

export function BehaviorInlineLink({ id }: EntityViewProps) {
  const data = unwrap(useEntity(id as EntityID<BehaviorData>));
  return <span><span className={styles["kind"]}>{kindOf(data)}</span> {summaryOf(data)}</span>;
}

export function BehaviorPreview({ id }: EntityViewProps) {
  const data = unwrap(useEntity(id as EntityID<BehaviorData>));
  const text = data.$type === CHAT_ROLLOUT
    ? firstText(data.context_turns[0]?.messages ?? [])
    : data.$type === TRANSCRIPT ? firstText(data.messages)
    : data.$type === SET_SYSTEM_MESSAGE ? firstText([data.message]) : data.description ?? "";
  return (
    <div className={styles["preview"]}>
      <span className={styles["kind"]}>{kindOf(data)}</span>
      <div>{summaryOf(data)}</div>
      {text && <p className={styles["prose"]}>{text.length > 200 ? `${text.slice(0, 200)}…` : text}</p>}
      {data.$type === CHAT_ROLLOUT && data.status.kind === "rollout_failed" && <p className={styles["error"]}>{data.status.failure.description}</p>}
    </div>
  );
}
