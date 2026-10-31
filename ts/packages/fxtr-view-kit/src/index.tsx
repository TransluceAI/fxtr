// Building blocks for renderer authors: components, shared layout, and helpers renderers use, not
// renderers. The kind label every inline link and preview starts with, a labelled field list, a
// chat-style message list and its turns, the small helpers every invocation renderer wants (its
// head, its input arrays, and what to show instead of an output it has not got), the adapters from
// a view of one invocation record to a panel of any count (`eachInvocation`; `singleInvocation`,
// deprecated), `LoadEach` for loading many entities at once, `DataTable` for rows the reader sorts,
// filters, and resizes, and `layout`, the classes of the looks renderers share (a panel's parts,
// prose, a dim line, a table, a chart's axes and pressable marks). For charts and the views around
// them: the frame that measures a chart and hosts its tooltip, marks
// pressable from the keyboard, legends (chart.tsx); the chart colours (colors.ts); number
// formatting (format.ts); headline numbers, segmented and select controls, and the box showing a
// pick (controls.tsx); and UI state kept in the panel's storage (stored_state.ts).
//
// A component when the kit owns structure or logic: several elements in a fixed arrangement, or
// markup derived from data, so a caller passes data and never sees the inside. A `layout` class
// when it is a look applied to one element the caller writes anyway, of whatever tag and content
// it chooses. Either way a caller never names a class by string: the kit's styles are CSS Modules,
// its components' own classes private, `layout` the one shared map. A renderer package that uses the
// kit bundles its own copy (it is not one of the modules a shell shares with a bundle), so the kit
// can change without a new API version; a piece that settles into something every renderer wants
// can move into fxtr-view.

import type { ReactNode } from "react";
import type { ArrayData, EntityID, InvocationRecord } from "fxtr-view";
import styles from "./kit.module.css";
import layout from "./layout.module.css";

export { layout };
export {
  eachInvocation,
  singleInvocation,
  spawnKeyLabel,
  type EachInvocationOptions,
  type SingleInvocationProps,
} from "./single_invocation";
export { LoadEach } from "./load_each";
export { DataTable, type DataTableProps, type TableColumn, type TableRowProps, type TableSort } from "./table";
export { ChartFrame, Legend, ScaleLegend, pressable, useWidth, type ChartFrameProps, type ChartTooltip, type LegendItem } from "./chart";
export { CHART_OTHER, CHART_SERIES, divergingFill, divergingInk, sequentialFill, sequentialInk, seriesColor } from "./colors";
export { DetailBox, Segmented, Select, Stat, Stats, type Choice } from "./controls";
export { MISSING, countLabel, formatNumber, formatPercent, formatSigned } from "./format";
export { useStoredState } from "./stored_state";

/** One turn of a chat transcript, as `MessageList` and `FirstTurn` show it: any data with a role
 *  and a content. */
export interface ChatMessage {
  readonly role: string;
  readonly content: string;
}

/** The style rule for custom renderers: a panel, a hover preview, and an inline link all start
 *  with the kind of thing they show, the type's short name written out (a renderer knows its own
 *  type; the generic cbor-tree views derive it), so wherever a reader lands on a value they can
 *  tell what it is before reading anything else. */
export function Kind({ children }: { children: ReactNode }) {
  return <span className={styles.kind}>{children}</span>;
}

/** A labelled field list. */
export function FieldList({ fields }: { fields: readonly (readonly [label: string, value: ReactNode])[] }) {
  return (
    <dl className={styles.fields}>
      {fields.map(([label, value]) => (
        <div key={label} className={styles.field}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One turn's inside: the role as a label, then the content as prose. A renderer that lays out
 *  turns its own way (with highlighting, say) puts these in its own container. */
export function MessageTurn({ role, children }: { role: string; children: ReactNode }) {
  return (
    <>
      <span className={styles.role}>{role}</span>
      <div className={styles.content}>{children}</div>
    </>
  );
}

/** Transcript turns, chat style: each a `MessageTurn`, marked by its role. */
export function MessageList({ messages }: { messages: readonly ChatMessage[] }) {
  if (messages.length === 0) return <p className={layout.empty}>no turns</p>;
  return (
    <ol className={styles.turns}>
      {messages.map((m, i) => (
        <li key={i} className={styles.turn} data-role={m.role}>
          <MessageTurn role={m.role}>{m.content}</MessageTurn>
        </li>
      ))}
    </ol>
  );
}

/** The first turn of a transcript, for previews. */
export function FirstTurn({ messages }: { messages: readonly ChatMessage[] }) {
  const first = messages[0];
  if (!first) return <p className={layout.empty}>no turns</p>;
  return (
    <div className={styles.previewTurn}>
      <span className={styles.role}>{first.role}</span> <span>{excerpt(first.content)}</span>
      {messages.length > 1 && <span className={styles.more}> · {messages.length - 1} more</span>}
    </div>
  );
}

/** A prose excerpt: the first `limit` characters, cut at a word. */
export function excerpt(text: string, limit = 160): string {
  if (text.length <= limit) return text;
  const cut = text.lastIndexOf(" ", limit);
  return `${text.slice(0, cut > limit / 2 ? cut : limit)}…`;
}

export function turnsLabel(n: number): string {
  return `${n} turn${n === 1 ? "" : "s"}`;
}

/** A named input of an invocation record, which must be there: a renderer keyed on a function's
 *  name knows that function's arguments. */
export function invocationInput(invocation: InvocationRecord, name: string): EntityID<ArrayData> {
  const found = invocation.inputs[name];
  if (found === undefined) throw new Error(`${invocation.name} has no input "${name}"`);
  return found;
}

/** The state of an invocation without an output yet: running, or failed with the error. */
export function InvocationOutcome({ invocation }: { invocation: InvocationRecord }) {
  return (
    <p className={styles.outcome}>
      {invocation.error ? `${invocation.error.typeName}: ${invocation.error.message}` : `${invocation.state} · no output yet`}
    </p>
  );
}

/** The head of a card or panel: the kind, then the registered name, then whatever the renderer
 *  puts beside it. */
export function InvocationHead({ kind, name, children }: { kind: "step" | "workflow"; name: string; children?: ReactNode }) {
  return (
    <div className={layout.header}>
      <Kind>{kind}</Kind> <b>{name}</b>
      {children}
    </div>
  );
}
