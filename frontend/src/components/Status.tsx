import { GRAPHQL_HTTP } from '../config'
import styles from './Status.module.css'

export function Loading({ what = 'loading…' }: { what?: string }) {
  return <div className={styles.loading}>{what}</div>
}

/** A failed read in place of what it would have shown, and a button reading it again. */
export function RetryLine({ error, onRetry, className }: { error: unknown; onRetry: () => void; className?: string }) {
  return <div role="alert" className={className}>{String(error)} <button onClick={onRetry}>retry</button></div>
}

/** A failed load, with the backend it was talking to, so a dead server is obvious. */
export function Problem({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className={styles.problem}>
      <div className={styles.problemTitle}>could not load</div>
      <div className={styles.problemMessage}>{error.message}</div>
      <div className={styles.problemHint}>backend: {GRAPHQL_HTTP}</div>
      {onRetry && (
        <button className="chip" onClick={onRetry}>
          retry
        </button>
      )}
    </div>
  )
}
