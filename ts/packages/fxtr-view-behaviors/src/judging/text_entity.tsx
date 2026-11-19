// A `TextEntity` (a rubric, a judge's instructions) as text to read: its markdown headings, rules,
// bold, italics and code set as such, and the lines of a paragraph wrapped by hand joined, so it
// reflows to the pane's width. A line that starts a line of its own in markdown (a list item, a
// quote, an indented line, a line after a hard break) keeps its line break, and so does every line
// of a fenced code block. Everything else stays as written.

import type { ReactNode } from "react";
import { unwrap, useEntity, type EntityID, type EntityPanelProps } from "fxtr-view";
import shared from "../behaviors.module.css";
import { countLabel } from "../entities";
import styles from "./judging.module.css";
import type { TextEntityData } from "./types";

const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*(-{3,}|\*{3,}|_{3,})\s*$/;
const INLINE = /\*\*([^*\n]+)\*\*|`([^`\n]+)`|\*([^*\s][^*\n]*)\*/g;
// A line that starts a line of its own: indented, a list item, or a quote.
const MARKDOWN_BLOCK_START = /^(?: {2,}|\t|\s*(?:[-*+]|\d+[.)])\s|\s*>)/;
const FENCE = /^\s*```/;

/** Text with `**bold**`, `*italics*` and `` `code` `` set as such. */
function inline(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let at = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > at) parts.push(text.slice(at, match.index));
    parts.push(
      match[1] !== undefined ? <strong key={match.index}>{match[1]}</strong>
        : match[2] !== undefined ? <code key={match.index}>{match[2]}</code>
        : <em key={match.index}>{match[3]}</em>,
    );
    at = match.index + match[0].length;
  }
  if (at < text.length) parts.push(text.slice(at));
  return parts;
}

/** The lines of a run with each line wrapped by hand joined to the one before it: a non-empty
 *  line that does not start a line of its own, after a non-empty line that does not end in a hard
 *  break (two spaces), outside a fenced code block. */
export function joinHardWrappedLines(lines: readonly string[]): string[] {
  const joined: string[] = [];
  let fenced = false;
  for (const line of lines) {
    const fence = FENCE.test(line);
    const previous = joined.at(-1);
    const wrapped = !fenced && !fence && previous !== undefined && previous.trim() !== "" && line.trim() !== ""
      && !MARKDOWN_BLOCK_START.test(line) && !FENCE.test(previous) && !/ {2}$/.test(previous);
    // The joined line keeps its own end, so a hard break at its end still counts.
    if (wrapped) joined[joined.length - 1] = `${previous.trimEnd()} ${line.trimStart()}`;
    else joined.push(line);
    if (fence) fenced = !fenced;
  }
  return joined;
}

/** Text as blocks to read: headings, rules, and the runs of lines between them. */
export function TextBody({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let run: string[] = [];
  const flush = () => {
    const body = joinHardWrappedLines(run).join("\n").replace(/^\n+|\n+$/g, "");
    if (body !== "") blocks.push(<p key={blocks.length}>{inline(body)}</p>);
    run = [];
  };
  for (const line of text.split("\n")) {
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push(<h3 key={blocks.length} data-level={heading[1]!.length}>{inline(heading[2]!)}</h3>);
    } else if (RULE.test(line)) {
      flush();
      blocks.push(<hr key={blocks.length} />);
    } else run.push(line);
  }
  flush();
  return <div className={styles["text"]}>{blocks}</div>;
}

/** A `TextEntity` in a panel, as the conversation views draw an entity: a header with its kind
 *  and how many lines it has, then its text, to read. */
export function TextPanel({ id }: EntityPanelProps) {
  const entity = unwrap(useEntity(id as EntityID<TextEntityData>));
  return (
    <section className={shared["panel"]} aria-label="Text">
      <header className={shared["header"]}>
        <span className={shared["kind"]}>TextEntity</span>
        <span className={shared["muted"]}>{countLabel(entity.text.split("\n").length, "line")}</span>
      </header>
      <TextBody text={entity.text} />
    </section>
  );
}
