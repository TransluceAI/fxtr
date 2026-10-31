import { Suspense, type ReactNode } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { Info } from 'lucide-react'
import { unwrap, useInvocation, type Spawn } from 'fxtr-view'
import { stepStatus, type GqlProvenance } from '../api/graphql'
import { useInvocationSnapshot, useOverviewSnapshot, useStepPin } from '../api/live'
import { useSource } from '../api/resources'
import { useNow } from '../lib/clock'
import { editorHref, codeLocation } from '../lib/code'
import { useEditor } from '../lib/editor'
import { glyphOf, runStateOf } from '../lib/progress'
import { formatElapsed, formatTime } from '../lib/time'
import type { InvocationStatus } from '../types'
import { cx } from '../lib/cx'
import { ErrorBoundary } from './ErrorBoundary'
import { StateGlyph } from './StateGlyph'
import styles from './InvocationInfo.module.css'

/** How many hex digits of a commit are shown; the whole hash is its title. */
const SHORT_COMMIT = 10

/** An info button whose popover holds one invocation's status line (with its error) and its
 *  provenance. They load when the popover opens, and stay current while it is open. */
export function InvocationInfo({ spawn }: { spawn: Spawn }) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button className="icon-button" aria-label="Invocation details" title="state, timing, code, cache key">
          <Info size={13} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className={cx('popover', styles.info)} side="bottom" align="end" sideOffset={4}>
          <div className="eyebrow">Invocation</div>
          <InvocationStatusLine spawn={spawn} withError />
          <InvocationProvenance spawn={spawn} />
          <Popover.Arrow className="popover-arrow" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

/**
 * One invocation's state and timing as one plain line, current while shown: `completed in 7s ·
 * <when it started>`, `running for 4s · started <when>`, `failed after 12s · <when>`, or just its
 * state before it starts. With `withError`, a failure's error follows on its own line.
 */
export function InvocationStatusLine({ spawn, withError = false }: { spawn: Spawn; withError?: boolean }) {
  const { ref } = spawn
  return (
    <ErrorBoundary fallback={(error) => <div className={styles.status}>state unavailable: {error.message}</div>}>
      <Suspense fallback={<div className={styles.status}>loading…</div>}>
        {ref.kind === 'step' ? (
          <StepStatus jobId={ref.jobId} cacheKey={ref.cacheKey} withError={withError} />
        ) : (
          <WorkflowStatus jobId={ref.jobId} pathname={ref.pathname} withError={withError} />
        )}
      </Suspense>
    </ErrorBoundary>
  )
}

function StepStatus({ jobId, cacheKey, withError }: { jobId: string; cacheKey: string; withError: boolean }) {
  return <StatusLine status={stepStatus(useStepPin(jobId, cacheKey))} withError={withError} />
}

function WorkflowStatus({ jobId, pathname, withError }: { jobId: string; pathname: string; withError: boolean }) {
  return <StatusLine status={useOverviewSnapshot(jobId, pathname).invocation} withError={withError} />
}

function StatusLine({ status, withError }: { status: InvocationStatus; withError: boolean }) {
  const now = useNow()
  const state = runStateOf(status.state)
  return (
    <>
      <div className={styles.status}>
        <StateGlyph glyph={glyphOf(state)} spin />
        <span>{statusText(status, now)}</span>
      </div>
      {withError && status.error && <div className={styles.error}>{status.error.typeName}: {status.error.message}</div>}
    </>
  )
}

function statusText(status: InvocationStatus, now: number): string {
  const elapsed = formatElapsed(status.startedAt, status.finishedAt, now)
  if (elapsed === null) return status.state
  if (status.finishedAt === null) return `${status.state} for ${elapsed} · started ${formatTime(status.startedAt)}`
  return `${status.state} ${status.state === 'completed' ? 'in' : 'after'} ${elapsed} · ${formatTime(status.startedAt)}`
}

/**
 * Where one invocation came from, as fine print: the commit it ran (and the job that ran it, for a
 * step result reused from another job's cache), where its function is implemented, and a step's
 * cache key or a workflow's pathname. Each part loads on its own, and stays current while shown.
 */
export function InvocationProvenance({ spawn }: { spawn: Spawn }) {
  const { ref } = spawn
  return (
    <dl className={styles.facts}>
      <ErrorBoundary fallback={(error) => <Fact label="commit" value={`unavailable: ${error.message}`} />}>
        <Suspense fallback={<Fact label="commit" value="loading…" />}>
          {ref.kind === 'step' ? (
            <StepCodeFacts jobId={ref.jobId} cacheKey={ref.cacheKey} />
          ) : (
            <WorkflowCodeFacts jobId={ref.jobId} pathname={ref.pathname} />
          )}
        </Suspense>
      </ErrorBoundary>
      <ErrorBoundary fallback={(error) => <Fact label="source" value={`unavailable: ${error.message}`} />}>
        <Suspense fallback={<Fact label="source" value="loading…" />}>
          <SourceFact spawn={spawn} />
        </Suspense>
      </ErrorBoundary>
      {ref.kind === 'step' && <Fact label="cache key" value={ref.cacheKey} mono />}
      {ref.kind === 'workflow' && <Fact label="pathname" value={ref.pathname} mono />}
    </dl>
  )
}

function Fact({ label, value, mono = false, title }: { label: string; value: ReactNode; mono?: boolean; title?: string }) {
  return (
    <div className={styles.fact}>
      <dt>{label}</dt>
      <dd className={cx(mono && styles.mono)} title={title}>{value}</dd>
    </div>
  )
}

/** A step's code: its bound result's, or while it has none its running attempt's. */
function StepCodeFacts({ jobId, cacheKey }: { jobId: string; cacheKey: string }) {
  const pin = useStepPin(jobId, cacheKey)
  return <CodeFacts jobId={jobId} provenance={pin.provenance ?? pin.attempt?.provenance ?? null} />
}

/** A workflow's code: its result's, or while it has none its last attempt's. */
function WorkflowCodeFacts({ jobId, pathname }: { jobId: string; pathname: string }) {
  const { invocation } = useInvocationSnapshot(jobId, pathname)
  return <CodeFacts jobId={jobId} provenance={invocation.provenance ?? invocation.attempts.at(-1)?.provenance ?? null} />
}

function CodeFacts({ jobId, provenance }: { jobId: string; provenance: GqlProvenance | null }) {
  if (!provenance) return <Fact label="commit" value="not run yet" />
  const commit = provenance.commit.slice(0, SHORT_COMMIT)
  return (
    <>
      <Fact label="commit" mono title={provenance.commit}
        value={provenance.ephemeral ? `${commit} (made from uncommitted changes)` : commit} />
      {provenance.jobId !== jobId && <Fact label="from job" value={`${provenance.jobId} (reused from its cache)`} mono />}
    </>
  )
}

/** Where the invocation's function is implemented, as a link opening its file and line in the
 *  editor last chosen (`lib/editor.ts`). */
function SourceFact({ spawn }: { spawn: Spawn }) {
  const source = useSource(unwrap(useInvocation(spawn.ref)).name, null)
  const editor = useEditor()
  if (!source) return <Fact label="source" value="not indexed" />
  const location = codeLocation(source)
  return (
    <Fact label="source" mono title={location}
      value={<a href={editorHref(editor, source)}>{`${source.file.split('/').at(-1)}:${source.startLine}`}</a>} />
  )
}
