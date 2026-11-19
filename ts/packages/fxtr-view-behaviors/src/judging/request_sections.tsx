// The sections `judge` writes into a request, and a message of a judgment's conversation shown with
// each section collapsed under its name. The citation instructions count as a section here: `judge`
// writes them as plain text in place of `{citation_instructions}`, not as a section, but they read
// as one.

import shared from "../behaviors.module.css";
import styles from "./judging.module.css";
import type { ReactNode } from "react";
import { ContentBlock } from "../messages";
import type { MessageData } from "../types";

/** One section `judge` wrote into a request: an entity, or the output schema, under its name. */
export interface Section {
  readonly name: string;
  readonly content: string;
}

// A section as behaviors/judging/judge.py writes it (`_wrap_in_section`): the boundary is computed
// from the content and does not occur in it, so the first closing tag with it ends the section.
export const SECTION = /<judge_section name="([^"]+)" boundary="([0-9a-f]+)">\n([\s\S]*?)\n<\/judge_section boundary="\2">/g;

/** The sections of a request's text, in order. */
export function sectionsOf(text: string): Section[] {
  return [...text.matchAll(SECTION)].map((match) => ({ name: match[1]!, content: match[3]! }));
}

// How the citation instructions begin: their heading and the first words under it, as
// `citation_instructions_text` in behaviors/judging/citations.py writes them, with entities or
// without. A template's own "## Citations" heading is not taken for them. They are the one text
// `judge` fills in that is not a section. Nothing marks where they end, so they are taken to run
// to the next section or the end of the message. Template text written after
// `{citation_instructions}` is therefore collapsed with them.
const CITATION_INSTRUCTIONS_START = /^## Citations\n\n(?:Each entity you are given|You are given no entities)/m;

/** Plain text longer than this many lines is collapsed as well. */
const LONG_TEXT_LINES = 8;

function lineCount(text: string): number {
  return text.split("\n").length;
}

/** One section, collapsed: its name (the entity's, or what `judge` filled in) and length, opening
 *  to the text that was sent. */
function CollapsedSection({ name, content }: { name: string; content: string }) {
  const lines = lineCount(content);
  return (
    <details className={styles["collapsedSection"]}>
      <summary>
        <span className={styles["placeholder"]}>{name}</span>
        <span className={styles["partLength"]}> · {lines} line{lines === 1 ? "" : "s"}</span>
      </summary>
      <pre className={styles["sent"]}>{content}</pre>
    </details>
  );
}

/** Text between sections: the citation instructions as their placeholder, other long text
 *  collapsed, short text as it is. */
function between(text: string, key: number): ReactNode {
  const start = text.search(CITATION_INSTRUCTIONS_START);
  if (start !== -1) {
    return (
      <span key={key}>
        {plainText(text.slice(0, start))}
        <CollapsedSection name="citation_instructions" content={text.slice(start).trim()} />
      </span>
    );
  }
  return <span key={key}>{plainText(text)}</span>;
}

function plainText(text: string): ReactNode {
  const trimmed = text.trim();
  if (lineCount(trimmed) <= LONG_TEXT_LINES) return text;
  return <CollapsedSection name="text" content={trimmed} />;
}

/** A message's text, each section `judge` wrote into it collapsed under its name, and other long
 *  text collapsed too. */
export function MessageWithSections({ text }: { text: string }) {
  const pieces: ReactNode[] = [];
  let at = 0;
  for (const match of text.matchAll(SECTION)) {
    if (match.index > at) pieces.push(between(text.slice(at, match.index), at));
    pieces.push(<CollapsedSection key={match.index} name={match[1]!} content={match[3]!} />);
    at = match.index + match[0].length;
  }
  if (at < text.length) pieces.push(between(text.slice(at), at));
  return <>{pieces}</>;
}

/** A message of a judgment's conversation, in its role's colours as a transcript's messages are.
 *  Its text has each section collapsed under its name (`MessageWithSections`); its other blocks
 *  (reasoning, say) are drawn as a transcript draws them. `note` follows the role in its heading,
 *  and `problems` (why an answer was invalid) go under the heading. */
export function ConversationMessage({ message, note, problems = [] }: { message: MessageData; note?: string; problems?: readonly string[] }) {
  return (
    <article className={shared["message"]} data-role={message.role}>
      <header className={shared["role"]}>
        {message.role}
        {note !== undefined && <span className={shared["muted"]}>{note}</span>}
      </header>
      {problems.map((problem, index) => <p className={`${shared["error"]} ${styles["warning"]}`} key={index}>{problem}</p>)}
      {message.content.map((block, index) => block.type === "text"
        ? <div className={shared["prose"]} key={index}><MessageWithSections text={block.text} /></div>
        : <ContentBlock block={block} key={index} />)}
    </article>
  );
}
