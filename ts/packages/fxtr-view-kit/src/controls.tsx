// Controls and summaries a view puts around its charts, in the shell's look: a row of headline
// numbers, a segmented choice, a labelled select, and the box that shows what a reader picked.
// Keep what they control in the panel's storage (`useStoredState`), so it survives a remount.

import { useRef, type KeyboardEvent, type ReactNode } from "react";
import styles from "./controls.module.css";

/** One headline number: a short label over the value, with an optional note beside it. */
export function Stat({ label, value, note }: { label: ReactNode; value: ReactNode; note?: ReactNode }) {
  return (
    <div className={styles.stat}>
      <dt>{label}</dt>
      <dd>
        <span className={styles.statValue}>{value}</span>
        {note !== undefined && <span className={styles.statNote}>{note}</span>}
      </dd>
    </div>
  );
}

/** A row of `Stat`s, wrapping on a narrow pane. */
export function Stats({ children }: { children: ReactNode }) {
  return <dl className={styles.stats}>{children}</dl>;
}

export interface Choice<T extends string> {
  readonly value: T;
  readonly label: ReactNode;
}

/** A choice among a few options, all in view: a radio group drawn as joined buttons. Arrow keys
 *  move the choice. */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly Choice<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  const group = useRef<HTMLDivElement>(null);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
    if (step === 0 || options.length === 0) return;
    event.preventDefault();
    const at = options.findIndex((option) => option.value === value);
    const next = (at + step + options.length) % options.length;
    onChange((options[next] as Choice<T>).value);
    group.current?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  };
  return (
    <div ref={group} className={styles.segmented} role="radiogroup" aria-label={label} onKeyDown={onKeyDown}>
      {options.map((option) => {
        const checked = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/** A choice among many options: a native select in the shell's look, after its label. */
export function Select<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: ReactNode;
  options: readonly Choice<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <label className={styles.select}>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value as T)}>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** What a reader picked in a chart, shown under it: a head line naming it, a close button, and
 *  the detail, which links the entities behind the pick. */
export function DetailBox({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  return (
    <section className={styles.detail}>
      <div className={styles.detailHead}>
        <span className={styles.detailTitle}>{title}</span>
        <button type="button" className={styles.close} aria-label="close" onClick={onClose}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <path d="M1 1 9 9M9 1 1 9" />
          </svg>
        </button>
      </div>
      {children}
    </section>
  );
}
