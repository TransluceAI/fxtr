import styles from "./behaviors.module.css";
import { useEffect, useRef } from "react";
import { isDataMap, type DataMap, type DataValue } from "fxtr-view";
import { MODEL_RESPONSE, type ContentData, type MessageData, type MetadataEntries, type TurnData } from "./types";

export function Metadata({ value, label = "Metadata" }: { value: DataMap; label?: string }) {
  if (Object.keys(value).length === 0) return null;
  return (
    <details className={styles["details"]}>
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

/** Tagged metadata entries, each shown under its type tag. */
export function MetadataList({ entries, label }: { entries: MetadataEntries; label: string }) {
  if (entries.length === 0) return null;
  return (
    <details className={styles["details"]}>
      <summary>{label}</summary>
      {entries.map(({ $type, ...fields }, index) => (
        <div key={index}>
          <div className={styles["muted"]}>{$type}</div>
          <pre>{JSON.stringify(fields, null, 2)}</pre>
        </div>
      ))}
    </details>
  );
}

/** The lines of a text, each with its number among the text's non-empty lines (null for an empty
 *  line), counting on from `start`. A message's lines are numbered through its text blocks in
 *  order; a judge cites a line by that number. */
export function textLines(text: string, start = 0): { readonly text: string; readonly line: number | null }[] {
  let next = start;
  return text.split("\n").map((line) => ({ text: line, line: /\S/.test(line) ? next++ : null }));
}

/** A text block with the lines `picked` (first and last, by number) highlighted. */
function PickedText({ text, start, picked }: { text: string; start: number; picked: readonly [number, number] }) {
  const lines = textLines(text, start);
  return (
    <div className={styles["prose"]}>
      {lines.map(({ text: line, line: number }, index) => {
        const end = index < lines.length - 1 ? "\n" : "";
        return number !== null && picked[0] <= number && number <= picked[1]
          ? <mark className={styles["picked"]} data-line={number} key={index}>{line}{end}</mark>
          : line + end;
      })}
    </div>
  );
}

/** One content block of a message: text as prose (its `picked` lines highlighted), reasoning and a
 *  tool call each collapsed under a summary. */
export function ContentBlock({ block, start = 0, picked = null }: {
  block: ContentData;
  /** The number of the block's first non-empty line among its message's. */
  start?: number;
  picked?: readonly [number, number] | null;
}) {
  switch (block.type) {
    case "text":
      if (picked !== null && block.text) return <PickedText text={block.text} start={start} picked={picked} />;
      return <div className={styles["prose"]}>{block.text || <em>Empty text</em>}</div>;
    case "reasoning":
      if (block.form === "redacted") {
        return <p className={styles["muted"]}>Reasoning redacted by provider.</p>;
      }
      return (
        <details className={styles["details"]}>
          <summary>{block.form === "summary" ? "Reasoning summary" : "Reasoning"}</summary>
          <div className={styles["prose"]}>
            {block.text || (block.form === "summary" ? "No summary text recorded." : "No reasoning text recorded.")}
          </div>
        </details>
      );
    case "tool_use": {
      // A tool call is one line of its message: picked, it is opened and highlighted.
      const isPicked = picked !== null && picked[0] <= start && start <= picked[1];
      return (
        <details className={isPicked ? `${styles["details"]} ${styles["picked"]}` : styles["details"]}
          {...(isPicked ? { "data-line": start, open: true } : {})}>
          <summary>Tool call · {block.name}</summary>
          <div className={styles["muted"]}>Call ID: {block.id}</div>
          {block.parse_error && <p className={styles["error"]}>{block.parse_error}</p>}
          {block.input === null ? <p className={styles["muted"]}>Arguments not yet received.</p>
            : <pre>{JSON.stringify(block.input, null, 2)}</pre>}
        </details>
      );
    }
  }
}

/** For each content block of a message, the number of its first line among the message's. The
 *  lines are the ones a judge can cite, counted as behaviors' `message_lines` counts them
 *  (judging/agent_run_evidence.py): each non-empty line of a piece of text, and each tool call,
 *  which is one line. */
function lineStarts(content: readonly ContentData[]): number[] {
  let next = 0;
  return content.map((block) => {
    const start = next;
    if (block.type === "text") next += textLines(block.text).filter(({ line }) => line !== null).length;
    else if (block.type === "tool_use") next += 1;
    return start;
  });
}

/** The nearest ancestor that scrolls an element up and down, or null if none does. */
function scrollerOf(element: HTMLElement): HTMLElement | null {
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    const overflow = getComputedStyle(parent).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && parent.scrollHeight > parent.clientHeight) return parent;
  }
  return null;
}

/** Scroll an element into view within the pane that scrolls it, and nothing else: one shorter than
 *  the pane is centred, a taller one is shown from its top. `scrollIntoView` is not used, since it
 *  scrolls every box around the element, sideways too, which moves the shell's panes. The pane
 *  glides there (`glide`). */
function scrollTo(element: HTMLElement): void {
  const scroller = scrollerOf(element);
  if (scroller === null) return;
  const top = element.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  const room = scroller.clientHeight - element.offsetHeight;
  glide(scroller, top - (room > 0 ? room / 2 : SCROLL_MARGIN));
}

/** The gap kept above an element scrolled to the top of its pane. */
const SCROLL_MARGIN = 6;

/** How long a glide takes, in ms, however far it goes. */
const GLIDE_MS = 500;

/** The glide each pane is making: its animation frame request, the timer that ends it, and
 *  where it ends. */
const gliding = new WeakMap<HTMLElement, { frame: number; timer: ReturnType<typeof setTimeout>; end: number }>();

/** Scroll `scroller` to `target`, easing out over `GLIDE_MS`. A glide to where the running one
 *  ends leaves it be; one elsewhere takes over from where the pane is. With reduced motion, the
 *  pane jumps there. A timer puts the pane at `target` once the glide should be over, in case the
 *  page draws no frames (a hidden tab), so the pane never stays short of it. */
function glide(scroller: HTMLElement, target: number): void {
  const end = Math.max(0, Math.min(target, scroller.scrollHeight - scroller.clientHeight));
  const start = scroller.scrollTop;
  const running = gliding.get(scroller);
  if (running !== undefined && Math.abs(running.end - end) < 1) return;
  if (running !== undefined) {
    cancelAnimationFrame(running.frame);
    clearTimeout(running.timer);
  }
  if (Math.abs(end - start) < 1) return;
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
    scroller.scrollTop = end;
    return;
  }
  const began = performance.now();
  const finish = () => {
    const current = gliding.get(scroller);
    if (current !== undefined) cancelAnimationFrame(current.frame);
    gliding.delete(scroller);
    scroller.scrollTop = end;
  };
  const timer = setTimeout(finish, GLIDE_MS + 50);
  const step = (now: number) => {
    const progress = Math.min(1, (now - began) / GLIDE_MS);
    scroller.scrollTop = start + (end - start) * (1 - (1 - progress) ** 3);
    if (progress < 1) gliding.set(scroller, { frame: requestAnimationFrame(step), timer, end });
    else {
      clearTimeout(timer);
      gliding.delete(scroller);
    }
  };
  gliding.set(scroller, { frame: requestAnimationFrame(step), timer, end });
}

/** `messages`, each in its role's colours. `first` is the index of the first of them among the
 *  messages of the whole conversation, and `selected` the index there of the one a selector picks
 *  (`MessageSelector`), which is marked, and scrolled into view when it is picked. `lines` are the
 *  lines the selector picks within it, which are highlighted, and scrolled to in its place. */
export function Messages({ messages, first = 0, selected = null, lines = null }: {
  messages: readonly MessageData[];
  first?: number;
  selected?: number | null;
  lines?: readonly [number, number] | null;
}) {
  const marked = useRef<HTMLElement>(null);
  const local = selected === null ? -1 : selected - first;
  useEffect(() => {
    const message = marked.current;
    if (!message) return;
    const element = message.querySelector<HTMLElement>("[data-line]") ?? message;
    // A pane that is just opening lays its content out over its first moments, and a scroll made
    // before then goes nowhere, so the scroll is made again until the pane has settled.
    const scroll = () => scrollTo(element);
    scroll();
    const timers = [50, 200, 500].map((delay) => setTimeout(scroll, delay));
    return () => timers.forEach(clearTimeout);
  }, [local, lines?.[0], lines?.[1]]);
  return (
    <div className={styles["messages"]}>
      {messages.map((message, index) => (
        <article className={styles["message"]} data-role={message.role} key={index}
          {...(index === local ? { "data-selected": "", ref: marked } : {})}>
          <header className={styles["role"]}>
            {message.role}
            {message.role === "tool" && (
              <>
                <span className={styles["muted"]}>{String(message["tool_call_id"] ?? "")}</span>
                {message["is_error"] === true && <span className={styles["error"]}>Tool error</span>}
              </>
            )}
          </header>
          {message.content.length === 0 && <p className={styles["muted"]}>No content recorded.</p>}
          {index === local && lines !== null
            ? message.content.map((block, blockIndex) =>
                <ContentBlock block={block} key={blockIndex} start={lineStarts(message.content)[blockIndex]!} picked={lines} />)
            : message.content.map((block, blockIndex) => <ContentBlock block={block} key={blockIndex} />)}
        </article>
      ))}
    </div>
  );
}

/** A token count a `Usage` record holds, or null when it is not a count (absent, null, negative,
 *  or not a finite number). */
function asTokenCount(value: DataValue | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/** The token counts of several responses' `Usage` records (behaviors/types/response.py), summed,
 *  in a line: `21,345 input (20,480 cached) · 1,234 output (900 reasoning) tokens`. Input counts
 *  every input token: those not cached, and those read from or written to the prompt cache; the
 *  ones read from it are noted when there are any. Output notes the part spent on reasoning when
 *  any is reported. A record without valid input and output counts is left out, and the line says
 *  how many of the responses it covers. Null when it covers none. */
export function usageLine(usages: readonly (DataValue | undefined)[]): string | null {
  let input = 0;
  let cached = 0;
  let output = 0;
  let reasoning = 0;
  let count = 0;
  for (const usage of usages) {
    if (!isDataMap(usage)) continue;
    const uncached = asTokenCount(usage["input_tokens"]);
    const out = asTokenCount(usage["output_tokens"]);
    if (uncached === null || out === null) continue;
    const read = asTokenCount(usage["cache_read_input_tokens"]) ?? 0;
    input += uncached + read + (asTokenCount(usage["cache_write_input_tokens"]) ?? 0);
    cached += read;
    output += out;
    reasoning += asTokenCount(usage["reasoning_output_tokens"]) ?? 0;
    count++;
  }
  if (count === 0) return null;
  const inputText = `${input.toLocaleString()} input${cached > 0 ? ` (${cached.toLocaleString()} cached)` : ""}`;
  const outputText = `${output.toLocaleString()} output${reasoning > 0 ? ` (${reasoning.toLocaleString()} reasoning)` : ""}`;
  const partial = count < usages.length ? ` · ${count}/${usages.length} responses reporting usage` : "";
  return `${inputText} · ${outputText} tokens${partial}`;
}

/** The token counts of a rollout's turns (`usageLine`), from the `ModelResponse` a model may
 *  record in its turn's metadata. */
export function rolloutUsageLine(turns: readonly TurnData[]): string | null {
  return usageLine(turns.map((turn) => turn.metadata.find((entry) => entry.$type === MODEL_RESPONSE)?.["usage"]));
}
