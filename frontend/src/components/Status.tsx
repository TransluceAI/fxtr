import { useEffect } from 'react'
import { viewerConfig } from '../config'
import styles from './Status.module.css'

/** What is on its way, centered in the space it will fill. */
export function Loading({ what = 'loading…' }: { what?: string }) {
  return (
    <div className={styles.loading} role="status">
      <span className={styles.spinner} aria-hidden="true" />
      {what}
    </div>
  )
}

/** Tells a page that frames the viewer that it has something to show. */
export function Shown() {
  useEffect(() => viewerConfig().onShown?.(), [])
  return null
}

/** That there is nothing to show here. */
export function Nothing({ what }: { what: string }) {
  return (
    <div className={styles.nothing}>
      <Shown />
      {what}
    </div>
  )
}

/** A failed read in place of what it would have shown, and a button reading it again. */
export function RetryLine({ error, onRetry, className }: { error: unknown; onRetry: () => void; className?: string }) {
  return <div role="alert" className={className}>{String(error)} <button onClick={onRetry}>retry</button></div>
}

/** A failed load, with the backend it was talking to, so a dead server is obvious. */
export function Problem({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className={styles.problem}>
      <Shown />
      <div className={styles.problemTitle}>could not load</div>
      <div className={styles.problemMessage}>{error.message}</div>
      <div className={styles.problemHint}>backend: {viewerConfig().graphqlUrl}</div>
      {onRetry && (
        <button className="chip" onClick={onRetry}>
          retry
        </button>
      )}
    </div>
  )
}
