// Which blocks of a judged transcript or agent run were not sent to the judge, and the warning
// that names them. A block is one message under the label the judge is sent it with (`B3`, `T0B3`,
// as behaviors/judging/agent_run_evidence.py writes them): a `LabelledBlock`, not a message's
// content block. A block is unsent when its label is not in the request: cut off, or dropped by
// the function that wrote the entity.

import shared from "../behaviors.module.css";
import styles from "./judging.module.css";
import { useEntity, type EntityData, type EntityID } from "fxtr-view";
import { useMemo } from "react";
import { AGENT_RUN, TRANSCRIPT, type MessageData, type TranscriptData, type TranscriptGroupData } from "../types";
import type { AttemptData, JudgmentData } from "./types";

function textOf(messages: readonly MessageData[]): string {
  return messages
    .map((message) => message.content.map((part) => (part.type === "text" ? part.text : "")).join(""))
    .join("\n\n");
}

/** One block of a judged entity: a message, under the label the judge is sent it with. */
export interface LabelledBlock {
  readonly address: string;
  readonly message: MessageData;
  /** The transcript the block belongs to, for an agent run. */
  readonly transcript: string | null;
}

/** The blocks of a transcript or an agent run, in the order and under the labels the judge's
 *  prompt text has them (behaviors/judging/agent_run_evidence.py). Null for any other entity, which has no addresses this view knows. */
export function blocksOf(data: EntityData): LabelledBlock[] | null {
  if (data.$type === TRANSCRIPT) {
    return (data as TranscriptData).messages.map((message, index) => ({ address: `B${index}`, message, transcript: null }));
  }
  if (data.$type === AGENT_RUN) {
    const run = data as TranscriptGroupData;
    const transcripts = [...run.transcripts];
    const visit = (group: TranscriptGroupData) => {
      transcripts.push(...group.transcripts);
      group.groups.forEach(visit);
    };
    run.groups.forEach(visit);
    return transcripts.flatMap((transcript, t) =>
      transcript.messages.map((message, index) => ({
        address: `T${t}B${index}`,
        message,
        transcript: transcript.name ?? `Transcript ${t}`,
      })),
    );
  }
  return null;
}

/** Everything an attempt's request said, as one text. */
export function sentText(attempt: AttemptData): string {
  return textOf(attempt.request.context);
}

/** The blocks whose label does not appear in what was sent: cut off, or dropped by a renderer. */
export function unsentBlocks(blocks: readonly LabelledBlock[], sent: string): LabelledBlock[] {
  return blocks.filter((block) => !sent.includes(`<|${block.address}; role:`));
}

/** Block addresses with each run of consecutive blocks of one transcript written as a range:
 *  `T0B2, T0B6–T0B23`. The blocks are in the order `blocksOf` gives them. */
export function addressRanges(blocks: readonly LabelledBlock[], all: readonly LabelledBlock[]): string {
  const position = new Map(all.map((block, index) => [block.address, index]));
  const runs: LabelledBlock[][] = [];
  for (const block of blocks) {
    const run = runs.at(-1);
    const previous = run?.at(-1);
    const adjacent = previous !== undefined && position.get(block.address) === position.get(previous.address)! + 1 && block.transcript === previous.transcript;
    if (run !== undefined && adjacent) run.push(block);
    else runs.push([block]);
  }
  return runs.map((run) => (run.length === 1 ? run[0]!.address : `${run[0]!.address}–${run.at(-1)!.address}`)).join(", ");
}

/** The blocks of one entity that were not sent to the judge, named, or nothing when all were. */
function EntityUnsentBlocks({ name, id, sent }: { name: string; id: EntityID; sent: string }) {
  const result = useEntity(id);
  const blocks = useMemo(() => (result.status === "loaded" ? blocksOf(result.value) : null), [result]);
  if (blocks === null) return null;
  const unsent = unsentBlocks(blocks, sent);
  if (unsent.length === 0) return null;
  return (
    <p className={`${shared["error"]} ${styles["warning"]}`}>
      Not sent to the judge: {addressRanges(unsent, blocks)} ({unsent.length} of {blocks.length} blocks of {name}).
    </p>
  );
}

/** For each transcript or agent run the judge was given, the blocks not sent in its first
 *  request. Nothing when every block was sent, or for an entity without blocks. */
export function UnsentBlocksWarning({ judgment }: { judgment: JudgmentData }) {
  const attempt = judgment.attempts[0];
  const sent = useMemo(() => (attempt === undefined ? "" : sentText(attempt)), [attempt]);
  if (attempt === undefined) return null;
  return (
    <>
      {Object.entries(judgment.entity_ids).map(([name, id]) => <EntityUnsentBlocks key={name} name={name} id={id} sent={sent} />)}
    </>
  );
}
