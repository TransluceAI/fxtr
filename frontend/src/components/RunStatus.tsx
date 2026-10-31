import type { ReactNode } from 'react'
import { useNow } from '../lib/clock'
import { finishedFraction, type Counts } from '../lib/progress'
import { formatSeconds } from '../lib/time'
import { cx } from '../lib/cx'
import type { RunActivity, StepDoc } from '../types'
import { RunProgressCard } from './RunProgressCard'
import { HoverPreview } from './HoverPreview'
import styles from './RunStatus.module.css'

/** Above this share of finished invocations failing, the failed count says out of how many. */
const FAILURE_SHARE_SHOWN = 0.25

/**
 * A run's status at the right of its card head: how much of it has finished as a percentage while
 * all is well, with words only for what is wrong: `14 failed` (or `261 of 381 failed` once failures
 * are common), `3 stopped`. A run of one says its state instead (`running · 2m14s`), and a run with
 * nothing left to do says `done`. The percentage, or the word in its place, opens a hover card with
 * the details (`RunProgressCard`); the failed
 * count is a button listing the card's failed invocations alone, and every invocation again
 * (`onToggleFailed`, pressed while `failedOnly`). Nothing for a node that was not run.
 */
export function RunStatus({ step, failedOnly, onToggleFailed }: { step: StepDoc; failedOnly: boolean; onToggleFailed: () => void }) {
  const { counts, activity } = step.progress
  if (counts === null || activity === null) return null
  if (counts.total === 0) return <span className={styles.status}>waiting</span>
  return counts.running + counts.queued > 0 ? (
    <LiveRunStatus step={step} counts={counts} activity={activity} failedOnly={failedOnly} onToggleFailed={onToggleFailed} />
  ) : (
    <RunStatusWords step={step} counts={counts} activity={activity} now={null} failedOnly={failedOnly} onToggleFailed={onToggleFailed} />
  )
}

interface WordsProps {
  step: StepDoc
  counts: Counts
  activity: RunActivity
  failedOnly: boolean
  onToggleFailed: () => void
}

/** A run still going re-renders each second, for a run of one's running time. */
function LiveRunStatus(props: WordsProps) {
  const now = useNow()
  return <RunStatusWords {...props} now={now} />
}

/** `now` is null for a run with nothing left to do, which has nothing that ticks. */
function RunStatusWords({ step, counts, activity, now, failedOnly, onToggleFailed }: WordsProps & { now: number | null }) {
  const failedButton = (text: string) => (
    <button
      className={cx(styles.failedCount, failedOnly && styles.isOn)}
      aria-pressed={failedOnly}
      onClick={(event) => {
        event.stopPropagation()
        onToggleFailed()
      }}
      title={failedOnly ? 'list every invocation again' : 'list the failed invocations and their errors'}
    >
      {text}
    </button>
  )

  let lead: ReactNode
  if (counts.total === 1) {
    if (counts.failed) return <span className={styles.status}>{failedButton('failed')}</span>
    lead = counts.running && now !== null && activity.oldestRunningSince !== null
      ? `running · ${formatSeconds((now - activity.oldestRunningSince) / 1000)}`
      : counts.running ? 'running' : counts.queued ? 'queued' : counts.stopped ? 'stopped' : 'done'
  } else if (counts.done + counts.failed === counts.total) {
    lead = 'done'
  } else {
    lead = <span className={styles.percent}>{Math.floor(finishedFraction(counts) * 100)}%</span>
  }

  const rest: ReactNode[] = []
  if (counts.total > 1 && counts.failed > 0) {
    const finished = counts.done + counts.failed
    rest.push(failedButton(
      counts.failed / finished > FAILURE_SHARE_SHOWN ? `${counts.failed} of ${finished} failed` : `${counts.failed} failed`,
    ))
  }
  if (counts.total > 1 && counts.stopped > 0) rest.push(`${counts.stopped} stopped`)

  return (
    <span className={styles.status}>
      <HoverPreview className={styles.card} side="bottom" align="end" collisionPadding={8}
        trigger={<span className={styles.hit} tabIndex={0}>{lead}</span>}>
        <RunProgressCard step={step} />
      </HoverPreview>
      {rest.map((part, index) => (
        <span key={index}>
          {' · '}
          {part}
        </span>
      ))}
    </span>
  )
}
