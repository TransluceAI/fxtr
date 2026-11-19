import { useMemo, useState } from 'react'
import { Check } from 'lucide-react'
import { fetchJobReuse, type NodeReuse } from '../api/graphql'
import { readOverview } from '../api/live'
import { useQuery } from '../api/browse'
import { buildLevel } from '../model/level'
import { jobGlyph } from '../lib/progress'
import { formatTime } from '../lib/time'
import type { JobSummary, Level, StepDoc } from '../types'
import { cx } from '../lib/cx'
import { edgePath, layoutLevel } from './flowchartLayout'
import { failureText } from './JobRow'
import { StateGlyph } from './StateGlyph'
import { RetryLine } from './Status'
import styles from './JobPicker.module.css'

interface Props {
  job: JobSummary
  /** The commit it launched at, where the list shows it under another; else undefined. */
  launchedAt?: { hash: string; subject: string } | null
  /** Open it at one of its root workflow's steps, by name. */
  onOpenStep: (id: string, step: string) => void
}

/**
 * The job under the pointer (or the arrow keys), before opening it: what it is, the steps it
 * failed on, its root workflow as a small flowchart saying, step by step, what the job ran itself
 * and what it reused from an earlier job, and the inputs changed since the workflow's preceding
 * job. Opening the job, starring it, and naming it are the list's (`JobRow`); a click on a step
 * opens the job there.
 */
export function JobPreview({ job, launchedAt, onOpenStep }: Props) {
  return (
    <div className={styles.preview}>
      <div className={styles.previewHead}>
        <span className={styles.previewTitle}>
          <span className={styles.previewName}>{breakable(job.workflowName)}</span>
        </span>
      </div>
      <div className={cx(styles.previewMeta, styles.mono)}>{job.id}</div>
      <div className={styles.previewMeta}>{formatTime(job.createdAt)}</div>
      {launchedAt && (
        <div className={styles.previewMeta} title={launchedAt.hash}>
          Launched at <span className={styles.mono}>{launchedAt.hash.slice(0, 7)}</span> · {launchedAt.subject}
        </div>
      )}
      <div className={cx(styles.status, job.failed && styles.isFailed)}>
        <span className={styles.statusGlyph}>
          <JobGlyph job={job} />
        </span>
        {job.failed ? failureText(job) : capitalized(job.state)}
      </div>
      {/* The flowchart before the changed inputs, which not every job has, so it never moves. */}
      <JobFlow job={job} onOpenStep={(step) => onOpenStep(job.id, step)} />
      <ChangedInputs job={job} />
    </div>
  )
}

/** Under a title saying so, a chip for each input given a different array than in the workflow's
 *  preceding job, with its value when that is a short scalar. */
function ChangedInputs({ job }: { job: JobSummary }) {
  const changed = job.inputs.filter((input) => job.changedInputs.includes(input.name))
  if (changed.length === 0) return null
  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>Inputs changed since the previous job</div>
      <div className={styles.inputs}>
        {changed.map((input) => (
          <InputChip key={input.name} name={input.name} value={input.text !== null ? `=${input.text}` : undefined} title={`Changed since the previous ${job.workflowName} job`} />
        ))}
      </div>
    </div>
  )
}

/** One input as a chip: its name, then `value` (`=16`) when it has one. When room runs out the
 *  name gives way first. */
function InputChip({ name, value, title }: { name: string; value?: string; title: string }) {
  return (
    <span className={styles.input} title={title}>
      <span className={styles.inputName}>{name}</span>
      {value && <span className={styles.inputValue}>{value}</span>}
    </span>
  )
}

/** A node's share of the preview: how many step results under it the job ran and reused. */
interface Share {
  ran: number
  reused: number
}

function JobFlow({ job, onOpenStep }: { job: JobSummary; onOpenStep: (step: string) => void }) {
  const loaded = useQuery(`job-preview:${job.id}:${job.state}`, async (signal) => {
    const [overview, reuse] = await Promise.all([readOverview(job.id, job.root), fetchJobReuse(job.id, signal)])
    return { level: buildLevel(job.id, overview), reuse }
  })
  if (loaded.error) return <RetryLine error={loaded.error} onRetry={loaded.reload} />
  if (!loaded.value) return <div className={styles.none}>Loading the workflow…</div>
  return <FlowPreview level={loaded.value.level} reuse={loaded.value.reuse} onOpenStep={onOpenStep} />
}

/** The preview's horizontal scale against the main flowchart, its cards' height, and the gap
 *  between its rows, which shrinks (to no less than the smallest) to keep it under MAX_HEIGHT. */
const SCALE_X = 0.3
const CARD_HEIGHT = 12
const ROW_GAP = 22
const MIN_ROW_GAP = 10
const MAX_HEIGHT = 240

/**
 * The main flowchart's layout of the level's run nodes (its inputs left out, so every card is in a
 * rank below the cards it reads), with every card at the smallest size (no names or dims), redrawn
 * small: its horizontal positions scaled down, and each row given a short fixed height, the
 * routes' corners moved with their rows. Every row keeps its order and every route its corners,
 * so the routes still pass between the cards rather than through them.
 */
export function compactLayout(steps: readonly StepDoc[]) {
  const ran = steps.filter((step) => step.progress.counts !== null)
  const full = layoutLevel(ran, { sideInputs: false, smallest: true })
  const rows = full.ranks.length
  const gap = rows < 2 ? ROW_GAP : Math.max(MIN_ROW_GAP, Math.min(ROW_GAP, (MAX_HEIGHT - rows * CARD_HEIGHT) / (rows - 1)))
  const top = (rank: number) => rank * (CARD_HEIGHT + gap)
  const y = (value: number, rank: number) => {
    const band = full.ranks[rank]
    return top(rank) + ((value - band.top) / band.height) * CARD_HEIGHT
  }
  return {
    nodes: full.nodes.map((node) => ({ step: node.step, x: node.x * SCALE_X, y: top(node.rank), width: node.width * SCALE_X, height: CARD_HEIGHT })),
    edges: full.edges.map((edge) => ({
      id: `${edge.sourceId}->${edge.targetId}`,
      sourceId: edge.sourceId,
      targetId: edge.targetId,
      path: edgePath(edge.points.map((point, index) => ({ x: point.x * SCALE_X, y: y(point.y, edge.pointRanks[index]) })), edge.pointRanks),
    })),
    width: full.width * SCALE_X,
    height: rows === 0 ? 1 : top(rows - 1) + CARD_HEIGHT,
  }
}

/** A step under the pointer, and where its card is in the preview, for the tooltip. */
interface Hover {
  step: StepDoc
  share: Share
  left: number
  right: number
  top: number
  bottom: number
}

/**
 * The root workflow's steps, small and without text (compactLayout). A step's card is red when
 * any of it failed, dark when the job ran some of it, and empty (an outline the colour of a faint edge) when the job ran none of it
 * (reused all of it from earlier jobs, or never reached it); an edge to or from an empty card is
 * faint, so the work the job did stands out. Hovering a card names the step and says what
 * happened to it; clicking it opens the job at the step.
 */
export function FlowPreview({ level, reuse, onOpenStep }: { level: Level; reuse: readonly NodeReuse[]; onOpenStep: (step: string) => void }) {
  const layout = useMemo(() => compactLayout(level.steps), [level.steps])
  const shares = useMemo(() => new Map<string, Share>(reuse.map((node) => [node.identity, node])), [reuse])
  const [hover, setHover] = useState<Hover | null>(null)
  const pad = 4
  const steps = new Map(level.steps.map((step) => [step.id, step]))
  const shareOf = (id: string): Share => shares.get(id) ?? { ran: 0, reused: 0 }
  /** Whether a step's card is blank. */
  const idle = (id: string) => {
    const step = steps.get(id)
    return step !== undefined && !cardFill(step, shareOf(id))
  }
  const point = (event: React.MouseEvent<SVGGElement>, step: StepDoc, share: Share) => {
    const box = event.currentTarget.getBoundingClientRect()
    const frame = event.currentTarget.ownerSVGElement!.parentElement!.getBoundingClientRect()
    setHover({ step, share, left: box.left - frame.left, right: frame.right - box.right, top: box.top - frame.top, bottom: box.bottom - frame.top })
  }
  return (
    <div className={styles.section}>
      <div className={styles.flowFrame}>
        <svg
          className={styles.flow}
          viewBox={`${-pad} ${-pad} ${layout.width + 2 * pad} ${layout.height + 2 * pad}`}
          width={layout.width + 2 * pad}
          height={layout.height + 2 * pad}
          role="img"
          aria-label="The job's steps"
        >
          {layout.edges.map((edge) => (
            <path key={edge.id} d={edge.path} className={cx(styles.flowEdge, (idle(edge.sourceId) || idle(edge.targetId)) && styles.isIdle)} />
          ))}
          {layout.nodes.map(({ step: laid, x, y, width, height }) => {
            const step = steps.get(laid.id) ?? laid
            const share = shareOf(step.id)
            const hovered = hover?.step.id === step.id
            return (
              <g key={step.id} transform={`translate(${x} ${y})`} onMouseEnter={(event) => point(event, step, share)} onMouseLeave={() => setHover(null)} onClick={() => onOpenStep(step.name)}>
                <rect width={width} height={height} rx={3} className={cx(styles.flowNode, cardFill(step, share), hovered && styles.isHovered)} />
              </g>
            )
          })}
        </svg>
        {hover && layout.nodes.some((node) => node.step.id === hover.step.id) && <StepTip hover={hover} />}
      </div>
    </div>
  )
}

/** A card's fill: failed, run (some of it) by the job, or neither. */
function cardFill(step: StepDoc, share: Share): string | false {
  if (step.progress.glyph.failed) return styles.isFailed
  return share.ran > 0 && styles.isRan
}

/** The hovered step's name and what happened to it, beside its card: above it unless that would
 *  leave the preview, and from whichever of its edges keeps the tip inside. */
function StepTip({ hover }: { hover: Hover }) {
  const above = hover.top > 40
  const style: React.CSSProperties = {
    ...(hover.left < hover.right ? { left: hover.left } : { right: hover.right }),
    ...(above ? { bottom: `calc(100% - ${hover.top - 4}px)` } : { top: hover.bottom + 4 }),
  }
  return (
    <div className={styles.stepTip} style={style} role="tooltip">
      <div className={styles.stepTipName}>{hover.step.name}</div>
      <div>{stepSummary(hover.step, hover.share)}</div>
    </div>
  )
}

/** What happened to a step in the job, in a few words: how many of its results the job ran and
 *  reused, and how many invocations failed, are still going, or were stopped. A workflow's results
 *  are those of the steps inside it. */
export function stepSummary(step: StepDoc, share: Share): string {
  const counts = step.progress.counts
  if (counts === null) return 'An input'
  const results = step.kind === 'workflow' ? 'its steps ' : ''
  const parts: string[] = []
  if (share.ran > 0 && share.reused > 0) parts.push(`${results}ran ${share.ran}, reused ${share.reused}`)
  else if (share.ran > 0) parts.push(`${results}ran ${share.ran}`)
  else if (share.reused > 0) parts.push(`${results}reused all ${share.reused} from earlier jobs`)
  if (counts.failed > 0) parts.push(`${counts.failed} failed`)
  if (counts.running > 0) parts.push(`${counts.running} running`)
  if (counts.queued > 0) parts.push(`${counts.queued} queued`)
  if (counts.stopped > 0) parts.push(`${counts.stopped} stopped`)
  return capitalized(parts.join(' · ') || 'did not run')
}

/** A job's state as a glyph, beside its state in words: a check once it succeeded, else its state
 *  glyph (a spinner while it runs). The list's rows show only the spinner (`JobRow`). */
function JobGlyph({ job }: { job: JobSummary }) {
  if (job.state === 'succeeded') return <Check size={11} strokeWidth={2.2} className={styles.succeeded} aria-hidden />
  return <StateGlyph glyph={jobGlyph(job.state, job.failed)} spin />
}

/** A dotted name that wraps, when it must, after a dot rather than inside a word. */
function breakable(name: string): React.ReactNode[] {
  return name.split('.').flatMap((part, index) => (index === 0 ? [part] : ['.', <wbr key={index} />, part]))
}

function capitalized(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}
