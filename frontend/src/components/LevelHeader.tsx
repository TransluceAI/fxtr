import { Crosshair } from 'lucide-react'
import { useNow } from '../lib/clock'
import { useLevelState } from '../lib/levelState'
import { formatElapsed, formatTime } from '../lib/time'
import type { Level, StepDoc } from '../types'
import { cx } from '../lib/cx'
import { ArgValue } from './ArgValue'
import { KindLabel } from './KindLabel'
import { SourceDocs } from './SourceDocs'
import { StateGlyph } from './StateGlyph'
import styles from './LevelHeader.module.css'

interface Props {
  level: Level
  steps: ReadonlyMap<string, StepDoc>
  onSelectStep: (id: string) => void
}

/**
 * The workflow invocation whose DAG is on screen: what it is, its docstring and code chip (as a
 * card has for its function), and how its run is going. While it
 * runs, the duration ticks, a summary says how many runs are in each state, `awaiting` names what
 * the body most likely waits on, and "jump to activity" scrolls to it (else to the first run still
 * going).
 */
export function LevelHeader({ level, steps, onSelectStep }: Props) {
  const { levelPath } = useLevelState()
  const { status } = level
  const now = useNow()
  const duration = formatElapsed(status.startedAt, status.finishedAt, now)
  const summary = runSummary(level.steps)
  const target = activityTarget(level, steps)
  return (
    <header className={styles.header}>
      <div className={styles.head}>
        <StateGlyph glyph={level.glyph} spin />
        <KindLabel kind="workflow" iconSize={14} />
        <span className={styles.title}>{level.workflowName}</span>
        <span className={styles.pathname} title={level.pathname}>
          {level.pathname}
        </span>
        {target !== null && (
          <button className={styles.jump} onClick={() => onSelectStep(target)} title="scroll to the run in progress">
            <Crosshair size={12} strokeWidth={1.8} />
            jump to activity
          </button>
        )}
      </div>
      <div className={styles.docs}>
        <SourceDocs method={level.workflowName} codeVersion={level.codeVersion} codeAt={levelPath} />
      </div>
      <dl className={styles.facts}>
        <Fact label="state" value={status.state} failed={status.error !== null} />
        {duration && <Fact label={status.finishedAt || !level.live ? 'duration' : 'running for'} value={duration} />}
        <Fact label="ran at" value={formatTime(status.startedAt)} />
        <Fact label="attempts" value={String(level.attemptCount)} />
        <Fact label="nodes" value={String(level.steps.length)} />
        {summary && <Fact label="runs" value={summary} />}
        {status.error && <Fact label="error" value={`${status.error.typeName}: ${status.error.message}`} failed />}
      </dl>
      {level.awaiting && (
        <div className={styles.awaiting} title="the last thing the running body asked for; it may already have its value">
          <span className={styles.awaitingLabel}>awaiting</span>
          <ArgValue
            arg={{ name: 'awaiting', expr: level.awaiting.expr, dims: [] }}
            mapOver={null}
            steps={steps}
            onSelectStep={onSelectStep}
          />
        </div>
      )}
    </header>
  )
}

/**
 * The workflow's docstring, as the working tree has it. Unlike a card's docs it says nothing while
 * loading, or when there is no docstring or none can be found, so the header stays as it was.
 */
function Fact({ label, value, failed = false }: { label: string; value: string; failed?: boolean }) {
  return (
    <div className={styles.fact}>
      <dt>{label}</dt>
      <dd className={cx(failed && styles.isFailed)}>{value}</dd>
    </div>
  )
}

/** `2 running · 1 waiting · 1 with failures` over the level's runs, or null when all are done. */
function runSummary(steps: StepDoc[]): string | null {
  const runs = steps.filter((step) => step.progress.counts !== null)
  const count = (predicate: (step: StepDoc) => boolean) => runs.filter(predicate).length
  const parts: [number, string][] = [
    [count((step) => step.progress.glyph.shape === 'running'), 'running'],
    [count((step) => step.progress.glyph.shape === 'queued'), 'queued'],
    [count((step) => step.progress.glyph.shape === 'waiting'), 'waiting'],
    [count((step) => step.progress.glyph.failed), 'with failures'],
  ]
  const shown = parts.filter(([n]) => n > 0)
  return shown.length === 0 ? null : shown.map(([n, label]) => `${n} ${label}`).join(' · ')
}

/**
 * The card "jump to activity" scrolls to: the awaited node when it is a card, else the first card
 * its expression reads, else the first run still going; null when nothing is going on.
 */
function activityTarget(level: Level, steps: ReadonlyMap<string, StepDoc>): string | null {
  if (level.awaiting) {
    if (steps.has(level.awaiting.id)) return level.awaiting.id
    const ref = level.awaiting.expr.find((token) => typeof token !== 'string')
    if (ref && typeof ref !== 'string' && steps.has(ref.ref)) return ref.ref
  }
  const order = ['running', 'queued', 'waiting'] as const
  for (const shape of order) {
    const step = level.steps.find((candidate) => candidate.progress.glyph.shape === shape)
    if (step) return step.id
  }
  return null
}
