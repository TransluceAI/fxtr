import styles from "./behaviors.module.css";
import {
  isDataMap,
  isPartial,
  markPartial,
  unwrap,
  useEntity,
  type DataValue,
  type EntityData,
  type EntityID,
  type EntityPanelProps,
  type EntityViewProps,
  type PartialEntityInlineProps,
  type PartialEntityPanelProps,
} from "fxtr-view";
import { isMessageSelector, selectedLines, selectedTranscript } from "./selector";
import { Messages, Metadata, MetadataList, rolloutUsageLine } from "./messages";
import {
  AGENT_RUN, CHAT_ROLLOUT, SET_SYSTEM_MESSAGE, TRANSCRIPT, TRANSCRIPT_GROUP,
  type BehaviorData, type ChatRolloutData, type ContentData, type MessageData, type PartialChatRolloutData,
  type PartialBehaviorData,
  type RolloutStatus, type TurnData,
  type SystemMessageData, type TranscriptData, type TranscriptGroupData,
} from "./types";

export function statusLabel(status: RolloutStatus): string {
  switch (status.kind) {
    case "rollout_completed": return "Completed";
    case "rollout_max_turns": return `Turn limit reached (${status.max_turns})`;
    case "rollout_failed": return `Failed · ${status.failure.category}`;
  }
}

/** A count and its noun: `1 message`, `3 messages`. */
export function countLabel(count: number, noun: string): string {
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

/** How far a rollout still being produced has got (a step's live preview of it). */
export interface RolloutProgress {
  /** Whether its step is still running; false once the step has failed or stopped, when the
   *  rollout will not be finished. */
  readonly live: boolean;
  /** Whether its last model turn is a reply the model is still generating, rather than a finished
   *  one. */
  readonly replyInFlight: boolean;
}

/** How far a transcript, or a group of them, still being produced has got (a step's live preview of
 *  it); which of its parts are still being written, `isPartial` says. */
export interface StreamProgress {
  /** Whether its step is still running; false once the step has failed or stopped. */
  readonly live: boolean;
}

function callsTools(turn: TurnData): boolean {
  return turn.messages.some((message) => message.content.some((block) => block.type === "tool_use"));
}

/** What the last turn of a rollout still being produced is waiting on, or how it was left: null for
 *  nothing to say, as once the rollout has `ended` (its status is known), when no turn is to come. */
function lastTurnNote(model: TurnData | undefined, progress: RolloutProgress, ended: boolean): string | null {
  if (ended) return null;
  if (model === undefined) return progress.live ? "waiting for the model…" : "no reply";
  if (progress.replyInFlight) return progress.live ? "streaming…" : "reply cut off";
  if (!progress.live) return null;
  return callsTools(model) ? "waiting for tools…" : "waiting for the user…";
}

/**
 * A rollout as a conversation: its initialization, then each turn's context and the model's reply.
 * `progress` is for a rollout still being produced (a step's live preview): its status reads "In
 * progress" (or "Stopped before finishing", once its step has stopped) until it is known, its last
 * turn says what it is waiting on (the model's reply streaming, the model, tools, or the user), and
 * the response count and usage cover finished replies only. Once the rollout has ended (its status
 * is known, though its step has yet to store it), it reads as the stored rollout does: its last
 * turn waits on nothing. `selected` is the index of the message a selector picks
 * (`MessageSelector`), marked.
 */
export function ChatRolloutView({ data, progress, selected = null }: {
  data: ChatRolloutData | PartialChatRolloutData;
  progress?: RolloutProgress;
  selected?: number | null;
}) {
  const finished = progress?.replyInFlight ? data.model_turns.slice(0, -1) : data.model_turns;
  const usage = rolloutUsageLine(finished);
  const turnCount = Math.max(data.context_turns.length, data.model_turns.length);
  const failed = data.status?.kind === "rollout_failed" ? data.status : null;
  const ended = data.status !== null;
  const status = data.status !== null ? statusLabel(data.status)
    : progress?.live === false ? "Stopped before finishing" : "In progress";
  // Where each turn's context and model messages start among the conversation's messages.
  const starts: { context: number; model: number }[] = [];
  let next = 0;
  for (let index = 0; index < turnCount; index++) {
    const context = next;
    next += data.context_turns[index]?.messages.length ?? 0;
    starts.push({ context, model: next });
    next += data.model_turns[index]?.messages.length ?? 0;
  }
  return (
    <section className={styles["panel"]} aria-label="Chat rollout">
      <header className={styles["header"]}>
        <span className={styles["kind"]}>ChatRollout</span>
        <span className={failed ? styles["error"] : styles["muted"]}>{status}</span>
        <h2>Conversation</h2>
        <div className={styles["muted"]}>{countLabel(finished.length, "response")}{usage && ` · ${usage}`}</div>
      </header>
      <Initialization data={data.initialization} />
      {turnCount === 0 && <p className={styles["muted"]}>No turns recorded.</p>}
      {Array.from({ length: turnCount }, (_, index) => {
        const context = data.context_turns[index];
        const model = data.model_turns[index];
        const note = progress && index === turnCount - 1 ? lastTurnNote(model, progress, ended) : null;
        return (
          <section className={styles["exchange"]} aria-label={`Turn ${index + 1}`} key={index}>
            <h3 className={styles["turn-number"]}>
              Turn {index + 1}
              {note && <span className={styles["muted"]}> · {note}</span>}
            </h3>
            {context && <Messages messages={context.messages} first={starts[index]!.context} selected={selected} />}
            {model ? <Messages messages={model.messages} first={starts[index]!.model} selected={selected} />
              : (!progress || ended) && <p className={styles["muted"]}>No model response recorded.</p>}
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
      {failed && <p className={styles["error"]}>{failed.failure.description}</p>}
      {data.end && <MetadataList entries={data.end.metadata} label="Completion metadata" />}
      <MetadataList entries={data.metadata} label="Metadata" />
    </section>
  );
}

/** The status a value still being produced shows in its header until it is finished. */
function inProgressLabel(live: boolean): string {
  return live ? "In progress" : "Stopped before finishing";
}

/** A transcript's messages, less the one still being written. */
function finishedMessages(data: TranscriptData): readonly MessageData[] {
  return isPartial(data.messages[data.messages.length - 1]) ? data.messages.slice(0, -1) : data.messages;
}

/** What a transcript still being produced is doing, after its messages: writing its last one (or
 *  cut off writing it), or nothing to say. */
function transcriptNote(data: TranscriptData, live: boolean): string | null {
  if (!isPartial(data.messages[data.messages.length - 1])) return null;
  return live ? "streaming…" : "message cut off";
}

/** How many of a group's transcripts, at any depth, have a message being written. */
function streamingCount(group: TranscriptGroupData): number {
  return group.transcripts.filter((transcript) => transcriptNote(transcript, true) !== null).length
    + group.groups.reduce((total, child) => total + streamingCount(child), 0);
}

/**
 * A transcript as its messages. `progress` is for one still being produced (a step's live preview):
 * its header reads "In progress" (or "Stopped before finishing", once its step has stopped), the
 * message count covers finished messages only, and a message still being written says so.
 * `selected` is the index of the message a selector picks (`MessageSelector`), marked, and `lines`
 * the lines it picks there, highlighted.
 */
function TranscriptView({ data, progress, selected = null, lines = null }: {
  data: TranscriptData;
  progress?: StreamProgress;
  selected?: number | null;
  lines?: readonly [number, number] | null;
}) {
  const note = progress ? transcriptNote(data, progress.live) : null;
  return (
    <section className={styles["panel"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>Transcript</span>
        {progress && <span className={styles["muted"]}>{inProgressLabel(progress.live)}</span>}
        <h2>{data.name || "Conversation"}</h2>
        {data.description && <p className={styles["prose"]}>{data.description}</p>}
        <div className={styles["muted"]}>{countLabel((progress ? finishedMessages(data) : data.messages).length, "message")}</div>
      </header>
      {data.messages.length ? <Messages messages={data.messages} selected={selected} lines={lines} /> : <p className={styles["muted"]}>No messages recorded.</p>}
      {note && <p className={styles["muted"]}>{note}</p>}
      <Metadata value={data.metadata} />
    </section>
  );
}

/** A message picked in one transcript of a group, by the transcript's place among the group's
 *  (`MessageSelector.transcript`). */
interface GroupPick {
  readonly transcript: number;
  readonly message: number;
  readonly lines: readonly [number, number] | null;
}

/**
 * A group of transcripts (an agent run's among them), each collapsed under its name, and the
 * groups nested in it. `first` is the place of its first transcript among the whole run's; the
 * transcript `pick` names, and every group around it, is opened, and its picked message marked.
 * `progress` is for one still being produced: its header reads "In progress" (or "Stopped before
 * finishing"), and each transcript still being written says so, and opens with its progress.
 */
function GroupView({ data, progress, first = 0, pick = null }: {
  data: TranscriptGroupData;
  progress?: StreamProgress;
  first?: number;
  pick?: GroupPick | null;
}) {
  const noteOf = (transcript: TranscriptData) => (progress ? transcriptNote(transcript, progress.live) : null);
  // The run's transcripts are counted as the judge's renderer labels them: this group's own, then
  // each nested group's, a group before the groups nested in it.
  let next = first + data.transcripts.length;
  const starts = data.groups.map((group) => {
    const start = next;
    next += groupCount(group);
    return start;
  });
  return (
    <section className={styles["panel"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>{kindOf(data)}</span>
        {progress && <span className={styles["muted"]}>{inProgressLabel(progress.live)}</span>}
        <h2>{data.name || "Conversations"}</h2>
        {data.description && <p className={styles["prose"]}>{data.description}</p>}
        <div className={styles["muted"]}>{countLabel(groupCount(data), "transcript")}</div>
      </header>
      {data.transcripts.length === 0 && data.groups.length === 0 && <p className={styles["muted"]}>No transcripts recorded.</p>}
      {data.transcripts.map((transcript, index) => {
        const picked = pick !== null && pick.transcript === first + index;
        return (
          <details className={styles["collection"]} key={index} {...(picked ? { open: true } : {})}>
            <summary>
              Transcript · {transcript.name || `Conversation ${index + 1}`} · {countLabel(finishedMessages(transcript).length, "message")}
              {noteOf(transcript) && ` · ${noteOf(transcript)}`}
            </summary>
            <TranscriptView
              data={transcript}
              selected={picked ? pick.message : null}
              lines={picked ? pick.lines : null}
              {...(progress && isPartial(transcript) ? { progress } : {})}
            />
          </details>
        );
      })}
      {data.groups.map((group, index) => {
        const holdsPick = pick !== null && starts[index]! <= pick.transcript && pick.transcript < starts[index]! + groupCount(group);
        return (
          <details className={styles["collection"]} key={index} {...(holdsPick ? { open: true } : {})}>
            <summary>TranscriptGroup · {group.name || `Group ${index + 1}`} · {countLabel(groupCount(group), "transcript")}</summary>
            <GroupView data={group} first={starts[index]!} pick={pick} {...(progress && isPartial(group) ? { progress } : {})} />
          </details>
        );
      })}
      <Metadata value={data.metadata} />
    </section>
  );
}

/** A conversation in full. For a `ChatRollout` or a `Transcript`, a `selector` param that is a
 *  `MessageSelector` marks its message (one of another shape, or past the last message, marks
 *  nothing), and for a `Transcript` highlights the lines it picks there. For an `AgentRun` the
 *  selector also names the transcript, which is opened with its message marked. */
export function BehaviorPanel({ id, params }: EntityPanelProps) {
  const data = unwrap(useEntity(id as EntityID<BehaviorData>));
  const selector = params?.["selector"];
  const selected = isMessageSelector(selector) ? selector.message : null;
  switch (data.$type) {
    case CHAT_ROLLOUT: return <ChatRolloutView data={data} selected={selected} />;
    case TRANSCRIPT:
      return <TranscriptView data={data} selected={selected} lines={isMessageSelector(selector) ? selectedLines(selector) : null} />;
    case TRANSCRIPT_GROUP: return <GroupView data={data} />;
    case AGENT_RUN:
      return <GroupView data={data} pick={isMessageSelector(selector)
        ? { transcript: selectedTranscript(selector), message: selector.message, lines: selectedLines(selector) } : null} />;
    case SET_SYSTEM_MESSAGE:
      return <section className={styles["panel"]}><span className={styles["kind"]}>SetSystemMessage</span><Messages messages={[data.message]} /></section>;
  }
}

/** A rollout, transcript, or group of transcripts where a shell links to it, as the shell writes a
 *  link of its own: its kind, then what it holds, in the link's own font (`Transcript · 9
 *  messages`). */
export function BehaviorInlineLink({ id }: EntityViewProps) {
  const data = unwrap(useEntity(id as EntityID<BehaviorData>));
  return <span>{kindOf(data)} · {summaryOf(data)}</span>;
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

/** A list field of a value still being produced: the list, or empty while it is not known. */
function listOf<T>(value: DataValue | undefined): readonly T[] {
  return Array.isArray(value) ? (value as readonly T[]) : [];
}

/** A content block still being produced, with its missing fields filled for display: text not yet
 *  known is empty, and a tool call's input stays null until it is known (`ContentBlock` says the
 *  arguments are not yet received). */
function partialBlock(block: ContentData): ContentData {
  if (block.type === "tool_use") return { ...block, input: isDataMap(block.input) ? block.input : null };
  return { ...block, text: typeof block.text === "string" ? block.text : "" };
}

/** A copy of a map a host handed over, marked still being produced if the original was (a copy is
 *  not marked otherwise, and the views read `isPartial` on what they are given). */
function copied<T extends DataValue>(original: DataValue | undefined, copy: T): T {
  return isPartial(original) && isDataMap(copy) ? (markPartial(copy) as T) : copy;
}

/** Messages still being produced: every one with its content so far. */
function partialMessages(value: DataValue | undefined): readonly MessageData[] {
  return listOf<MessageData>(value).map((message) =>
    copied(message, { ...message, content: listOf<ContentData>(message.content).map(partialBlock) }));
}

/** A transcript still being produced, as `TranscriptView` reads it: its messages so far, and empty
 *  metadata while unknown. Marked partial, as the value handed over was. */
export function partialTranscript(value: DataValue): TranscriptData {
  const data = value as unknown as TranscriptData;
  return copied(value, {
    ...data,
    messages: partialMessages(data.messages),
    metadata: isDataMap(data.metadata) ? data.metadata : {},
  });
}

/** A group of transcripts (or an agent run) still being produced, as `GroupView` reads it: every
 *  transcript and group so far, each read as one still being produced. */
export function partialGroup(value: DataValue): TranscriptGroupData {
  const data = value as unknown as TranscriptGroupData;
  return copied(value, {
    ...data,
    transcripts: listOf<DataValue>(data.transcripts).map(partialTranscript),
    groups: listOf<DataValue>(data.groups).map(partialGroup),
    metadata: isDataMap(data.metadata) ? data.metadata : {},
  });
}

/** A rollout still being produced, as `ChatRolloutView` reads it: every list it walks present (empty
 *  while unknown), every message with its content so far. */
export function partialChatRollout(value: EntityData): PartialChatRolloutData {
  const data = value as unknown as PartialChatRolloutData;
  const turn = (t: TurnData): TurnData => ({
    ...t,
    messages: partialMessages(t.messages),
    metadata: listOf<EntityData>(t.metadata),
  });
  return {
    ...data,
    context_turns: listOf<TurnData>(data.context_turns).map(turn),
    model_turns: listOf<TurnData>(data.model_turns).map(turn),
    metadata: listOf<EntityData>(data.metadata),
    initialization: isDataMap(data.initialization) ? data.initialization : null,
    status: isDataMap(data.status) ? data.status : null,
    end: isDataMap(data.end) ? data.end : null,
  };
}

/** How far a previewed rollout has got: undefined for a finished one (a step may publish its final
 *  rollout as a preview), which shows as it would stored. */
function progressOf(value: EntityData, partial: boolean, live: boolean): RolloutProgress | undefined {
  if (!partial) return undefined;
  const modelTurns = value["model_turns"];
  const last = Array.isArray(modelTurns) ? modelTurns[modelTurns.length - 1] : undefined;
  return { live, replyInFlight: isPartial(last) };
}

/**
 * A rollout, transcript, or group of transcripts (an agent run's among them) a step is still
 * producing (its live preview), as the conversation it is becoming: the view a stored one gets,
 * saying what is still being written. A finished one (a step may publish its final value as a
 * preview) shows as it would stored.
 */
export function BehaviorPartialPanel({ value, partial, live }: PartialEntityPanelProps) {
  const data = value as unknown as PartialBehaviorData;
  const stream = partial ? { progress: { live } } : {};
  switch (data.$type) {
    case CHAT_ROLLOUT: {
      const progress = progressOf(value, partial, live);
      return <ChatRolloutView data={partialChatRollout(value)} {...(progress ? { progress } : {})} />;
    }
    case TRANSCRIPT: return <TranscriptView data={partialTranscript(value)} {...stream} />;
    case TRANSCRIPT_GROUP:
    case AGENT_RUN: return <GroupView data={partialGroup(value)} {...stream} />;
  }
}

/**
 * A rollout, transcript, or group of transcripts a step is still producing, in one line: how far it
 * has got and what it is doing. A rollout says the turn it is on and what that turn waits on (as
 * the conversation view's last turn says: "turn 6 · streaming…"), a transcript its finished
 * messages and whether one is being written ("4 messages · streaming…"), a group its transcripts
 * and how many are being written ("3 transcripts · 1 streaming…"). A finished one reads as its
 * inline link does, as does a rollout that has ended (its status is known) while its step has yet
 * to store it.
 */
export function BehaviorPartialInline({ value, partial, live }: PartialEntityInlineProps) {
  const data = value as unknown as PartialBehaviorData;
  const kind = `${kindOf(data as BehaviorData)} ·`;
  if (!partial) return <span>{kind} {summaryOf(value as unknown as BehaviorData)}</span>;
  const stopped = live ? "in progress" : "stopped";
  switch (data.$type) {
    case CHAT_ROLLOUT: {
      const rollout = partialChatRollout(value);
      if (rollout.status !== null) {
        return <span>{kind} {countLabel(rollout.model_turns.length, "response")} · {statusLabel(rollout.status)}</span>;
      }
      const turnCount = Math.max(rollout.context_turns.length, rollout.model_turns.length);
      const note = lastTurnNote(rollout.model_turns[turnCount - 1], progressOf(value, partial, live)!, false) ?? "stopped";
      return <span>{kind} {turnCount === 0 ? note : `turn ${turnCount} · ${note}`}</span>;
    }
    case TRANSCRIPT: {
      const transcript = partialTranscript(value);
      const note = transcriptNote(transcript, live) ?? stopped;
      return <span>{kind} {countLabel(finishedMessages(transcript).length, "message")} · {note}</span>;
    }
    case TRANSCRIPT_GROUP:
    case AGENT_RUN: {
      const group = partialGroup(value);
      const streaming = streamingCount(group);
      const note = streaming === 0 ? stopped : live ? `${streaming} streaming…` : `${streaming} cut off`;
      return <span>{kind} {group.name ? `${group.name} · ` : ""}{countLabel(groupCount(group), "transcript")} · {note}</span>;
    }
  }
}
