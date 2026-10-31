// The list `eachInvocation` renders for several invocations: a collapsible section per subject,
// headed by its label and an optional one-line summary, rendering its body only once opened. Each
// summary and body loads on its own: its own loading marker, and a failure shown in its place
// rather than taking the whole list down.

import { Component, Suspense, useState, type ReactNode } from "react";
import styles from "./kit.module.css";

class SectionErrorBoundary extends Component<{ children: ReactNode }, { error: unknown; failed: boolean }> {
  override state = { error: undefined as unknown, failed: false };

  static getDerivedStateFromError(error: unknown) {
    return { error, failed: true };
  }

  override render() {
    if (!this.state.failed) return this.props.children;
    const { error } = this.state;
    return (
      <span className={styles.sectionError} role="alert">
        {error instanceof Error ? error.message : String(error)}
      </span>
    );
  }
}

/** Render `children` under their own error and Suspense boundaries. */
function OwnLoad({ children }: { children: ReactNode }) {
  return (
    <SectionErrorBoundary>
      <Suspense fallback={<span className={styles.sectionLoading}>…</span>}>{children}</Suspense>
    </SectionErrorBoundary>
  );
}

/** One subject's section: closed at first, its body rendered while open. */
export function LazySection({ label, summary, body }: { label: string; summary?: ReactNode; body: () => ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <details className={styles.section} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <span className={styles.sectionLabel}>{label}</span>
        {summary !== undefined && <OwnLoad>{summary}</OwnLoad>}
      </summary>
      {open && <OwnLoad>{body()}</OwnLoad>}
    </details>
  );
}

/** The sections, in order. */
export function Sections({ children }: { children: ReactNode }) {
  return <div className={styles.sections}>{children}</div>;
}
