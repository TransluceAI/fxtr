import { Suspense } from 'react'
import { stepStatus, type GqlLogLine, type GqlPreview } from '../api/graphql'
import { useOverviewSnapshot, useStepPin } from '../api/live'
import { useNow } from '../lib/clock'
import { glyphOf, isLive, runStateOf } from '../lib/progress'
import { formatElapsed } from '../lib/time'
import type { InvocationStatus, StepInvocation } from '../types'
import { ErrorBoundary } from './ErrorBoundary'
import { LoadingContent } from '../lib/loading'
import { StateGlyph } from './StateGlyph'
import { StepPreview } from './StepPreview'
import styles from './PendingInvocation.module.css'

/** How many of a running step's latest `ctx.log` lines are shown. */
const LOG_TAIL = 8

/**
 * One invocation that has not finished, failed, or stopped, in place of its output: its state and
 * how long it has run, its error, for a step the latest preview it published (`ctx.preview`) and
 * the latest lines it logged, which update while it runs, and a line on what to expect of its
 * output. A child workflow is followed by opening it.
 */
export function PendingInvocation({ jobId, invocation }: { jobId: string; invocation: StepInvocation }) {
  return (
    <ErrorBoundary fallback={(error) => <span className="detail-none">state unavailable: {error.message}</span>}>
      <Suspense fallback={<LoadingContent><span className="detail-none">loading…</span></LoadingContent>}>
        {invocation.stepCacheKey !== null ? (
          <StepPending jobId={jobId} cacheKey={invocation.stepCacheKey} />
        ) : invocation.childPathname !== null ? (
          <WorkflowPending jobId={jobId} pathname={invocation.childPathname} />
        ) : null}
      </Suspense>
    </ErrorBoundary>
  )
}

function StepPending({ jobId, cacheKey }: { jobId: string; cacheKey: string }) {
  const pin = useStepPin(jobId, cacheKey)
  return <Pending status={stepStatus(pin)} logs={pin.logs} preview={pin.preview} hint={stepHint(pin.state)} />
}

/** What to expect of a step with no output, by its state as the backend spells it; none for a
 *  failed step, whose error says it. */
function stepHint(state: string): string | null {
  if (isLive(runStateOf(state))) return 'output appears when the step finishes'
  if (state === 'stopped') return 'no output: the job stopped before this step finished. Resuming the job runs it.'
  return null
}

function WorkflowPending({ jobId, pathname }: { jobId: string; pathname: string }) {
  const { invocation } = useOverviewSnapshot(jobId, pathname)
  const status = invocation
  return <Pending status={status} logs={[]} hint={isLive(runStateOf(status.state)) ? 'open it to follow its progress' : null} />
}

function Pending({
  status,
  logs,
  preview = null,
  hint,
}: {
  status: InvocationStatus
  logs: GqlLogLine[]
  preview?: GqlPreview | null
  hint: string | null
}) {
  const now = useNow()
  const state = runStateOf(status.state)
  const elapsed = formatElapsed(status.startedAt, status.finishedAt, now)
  const tail = logs.slice(-LOG_TAIL)
  return (
    <div className={styles.pending}>
      <div className={styles.head}>
        <StateGlyph glyph={glyphOf(state)} spin />
        <span className={styles.state}>{status.state}</span>
        {elapsed && (
          <span className={styles.elapsed}>
            {status.finishedAt ? `ran ${elapsed}` : `running ${elapsed}`}
          </span>
        )}
      </div>
      {status.error && (
        <div className={styles.error}>
          {status.error.typeName}: {status.error.message}
        </div>
      )}
      <StepPreview preview={preview} live={isLive(state)} />
      {tail.length > 0 && (
        <pre className={styles.logs}>
          {logs.length > tail.length && `… ${logs.length - tail.length} earlier lines\n`}
          {tail.map((line, index) => (
            <span key={line.seq} className={index === tail.length - 1 ? styles.latest : undefined}>
              {line.message}
              {'\n'}
            </span>
          ))}
        </pre>
      )}
      {hint && <span className={styles.hint}>{hint}</span>}
    </div>
  )
}
