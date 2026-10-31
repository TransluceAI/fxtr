import { useFinishLog } from '../api/finishes'
import { useNow } from '../lib/clock'
import { finishRate, timeLeft, type Counts } from '../lib/progress'
import { formatClock, formatEstimate, formatSeconds } from '../lib/time'
import { cx } from '../lib/cx'
import type { FailureGroup, RunActivity, RunSource, StepDoc } from '../types'
import { Heartbeat } from './Heartbeat'
import styles from './RunStatus.module.css'

/**
 * The hover card of a run's status (`RunStatus`). While the run is going: a heartbeat of its last
 * minute of finishes (`Heartbeat`, from what this page has seen, `api/finishes.ts`), then the colour
 * legend with each state's count beside the facts (rate,
 * time left, last finish, longest running), then a row per error type. Once nothing is left to do,
 * or for a run of one: when it finished and how long it took, the legend and the errors. No
 * percentage or headline: the card is opened from them.
 */
export function RunProgressCard({ step }: { step: StepDoc }) {
  const { counts, activity, failures } = step.progress
  if (counts === null || activity === null || step.browse === null) throw new Error(`${step.name} is not a run`)
  return <RunProgressBody source={step.browse} counts={counts} activity={activity} failures={failures} />
}

function RunProgressBody({ source, counts, activity, failures }: {
  source: RunSource
  counts: Counts
  activity: RunActivity
  failures: FailureGroup[]
}) {
  const log = useFinishLog(source.jobId, source.pathname, source.nodeId)
  const now = useNow()
  const live = counts.running + counts.queued > 0

  if (live && counts.total === 1) {
    const started = activity.oldestRunningSince
    return (
      <div className={styles.body}>
        <div className={styles.heading}>{counts.running ? `Running${started !== null ? ` for ${formatSeconds((now - started) / 1000)}` : ''}` : 'Queued'}</div>
        {started !== null && <div className={styles.dim}>One invocation, started at {formatClock(started)}.</div>}
      </div>
    )
  }

  if (!live) {
    const { firstStartedAt, lastFinishedAt } = activity
    const allFinished = counts.done + counts.failed === counts.total
    const took = firstStartedAt !== null && lastFinishedAt !== null ? lastFinishedAt - firstStartedAt : null
    return (
      <div className={styles.body}>
        <div className={styles.heading}>
          {allFinished ? 'Finished' : 'Stopped'}
          {lastFinishedAt !== null && `${allFinished ? ' at' : ', last finish at'} ${formatClock(lastFinishedAt)}`}
        </div>
        {took !== null && (
          <dl className={styles.facts}>
            <dt>took</dt>
            <dd>{formatSeconds(took / 1000)}</dd>
            {counts.total > 1 && took > 0 && (
              <>
                <dt>average rate</dt>
                <dd>{((activity.finished / took) * 1000).toFixed(1)} / s</dd>
              </>
            )}
          </dl>
        )}
        {counts.total > 1 && <div className={styles.section}><Legend counts={counts} /></div>}
        <Failures failures={failures} />
      </div>
    )
  }

  const rate = finishRate(log?.finishes ?? null, log?.since ?? null, activity, now)
  const left = timeLeft(counts, rate)
  return (
    <div className={styles.body}>
      <Heartbeat finishes={log?.finishes ?? []} />
      <div className={styles.pair}>
        <div className={styles.section}><Legend counts={counts} /></div>
        <dl className={cx(styles.section, styles.facts)}>
          <dt>rate</dt>
          <dd>{rate === null ? '—' : `${rate.toFixed(1)} / s`}</dd>
          <dt>time left</dt>
          <dd>{left === null ? '—' : formatEstimate(left)}</dd>
          <dt>last finish</dt>
          <dd>{activity.lastFinishedAt === null ? 'none yet' : ago(now - activity.lastFinishedAt)}</dd>
          {activity.oldestRunningSince !== null && (
            <>
              <dt>longest</dt>
              <dd>{formatSeconds((now - activity.oldestRunningSince) / 1000)}</dd>
            </>
          )}
        </dl>
      </div>
      <Failures failures={failures} />
    </div>
  )
}

/** The progress line's colours, each with its state's count; stopped only when there are any. */
function Legend({ counts }: { counts: Counts }) {
  const rows: [string, string, number][] = [
    ['done', styles.swatchDone, counts.done],
    ['running', styles.swatchRunning, counts.running],
    ['queued', styles.swatchQueued, counts.queued],
    ['failed', styles.swatchFailed, counts.failed],
  ]
  if (counts.stopped > 0) rows.push(['stopped', styles.swatchStopped, counts.stopped])
  return (
    <div className={styles.legend}>
      {rows.map(([label, swatch, count]) => (
        <div key={label} className={cx(styles.legendRow, count === 0 && styles.isZero, label === 'failed' && count > 0 && styles.isFailed)}>
          <i className={cx(styles.swatch, swatch)} />
          <span>{label}</span>
          <span className={styles.count}>{count.toLocaleString()}</span>
        </div>
      ))}
    </div>
  )
}

/** A row per error type, most frequent first, each with one of its messages. */
function Failures({ failures }: { failures: FailureGroup[] }) {
  if (failures.length === 0) return null
  return (
    <div className={cx(styles.section, styles.failures)}>
      {failures.map((failure) => (
        <div key={failure.typeName} className={styles.failure}>
          <span className={styles.failureCount}>{failure.count.toLocaleString()}</span>
          <span className={styles.times}>×</span>
          <span className={styles.failureType}>{failure.typeName}</span>
          <span className={styles.failureMessage} title={failure.message}>{failure.message}</span>
        </div>
      ))}
    </div>
  )
}

const ago = (ms: number) => (ms < 1500 ? 'just now' : `${formatSeconds(ms / 1000)} ago`)
