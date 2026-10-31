import { memo, useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { InvocationPanel } from 'fxtr-view'
import { useRenderers } from 'fxtr-view-shell-util'
import { formatType, returnType, typeLabel } from '../model/schema'
import { invocationSliceFor } from '../lib/invocations'
import { oneRowEach, useCardMode, useFailedOnly } from '../lib/cardTable'
import type { CardMode } from '../lib/levelState'
import { useCardSpotlit } from '../lib/cardSpotlight'
import { useLevelState } from '../lib/levelState'
import { hasOutput, useSelectedRun } from '../lib/output'
import type { StepDoc } from '../types'
import { ArgValue } from './ArgValue'
import { CardTable } from './CardTable'
import { KindLabel } from './KindLabel'
import { ViewsButton, type ViewLink } from './PanelButton'
import { viewTarget } from '../lib/linkBase'
import { isDetailView, isProjectRenderer } from '../views/registry'
import { SourceDocs } from './SourceDocs'
import { ProgressLine } from './ProgressLine'
import { RunStatus } from './RunStatus'
import { cx } from '../lib/cx'
import styles from './StepCard.module.css'

interface Props {
  step: StepDoc
  steps: ReadonlyMap<string, StepDoc>
  /** Which jump to this card to glow for: a new value (a chip naming the node was clicked, and the
   *  trace jumped here) plays the glow again; null for none. */
  jumpGlow?: number | null
  /** The jump glow on the card with this id has faded out. */
  onJumpGlowEnd?: (id: string) => void
  /** New since the level was last rendered: the card fades in once. */
  fresh?: boolean
  /** Remove the outer card chrome when the card itself is the whole pane. */
  edgeToEdge?: boolean
  /** The views of the project's own a pane shows this node through (`viewsShownOf`). */
  viewsShown: readonly string[]
  /** The card's element by its id, as it mounts and unmounts (null). */
  onCardElement?: (id: string, element: HTMLElement | null) => void
  /** Make the card with this id the selected one, without scrolling to it. */
  onActivate: (id: string) => void
  onSelectStep: (id: string) => void
}

/**
 * One node of the level: its docs, its arguments below them, and at its foot the table of its rows
 * in the level's slice (`CardTable`), whose key columns' headers slice the level. Its head has a
 * button opening the step's views of the project's own in the detail view (`ViewsButton`: the view's
 * name for one; for several, the current view's segment beside a counted one with a menu of them
 * all; none without any; its rows open the node's other
 * panels). For a run, the head also carries its status (`RunStatus`:
 * the percentage finished, and words for failures or stopped invocations, opening a hover
 * card of details) and, along its bottom edge while anything is left to finish, its progress line
 * (`ProgressLine`); the failed count lists the card's failed invocations and their errors alone,
 * and then every invocation again (its colours inverted while on, as a sliced key's are).
 * A segmented control floating just above a run's table, at its right, chooses what the foot lists
 * (`ModeSwitch`), for a run whose invocations produce several rows each; one whose invocations each
 * produce one row lists its invocations alone, with their rows' values.
 * Jumped to from a chip, the card glows blue and the glow fades out (`jumpGlow`); lit by a hovered
 * chip naming the node (`useCardSpotlit`), it glows too. Memoized: give it callbacks of fixed
 * identity, which take the card's id.
 */
export const StepCard = memo(function StepCard({
  step,
  steps,
  jumpGlow = null,
  onJumpGlowEnd,
  fresh = false,
  edgeToEdge = false,
  viewsShown,
  onCardElement,
  onActivate: onActivateId,
  onSelectStep,
}: Props) {
  const spotlit = useCardSpotlit(step.id)
  const onActivate = () => onActivateId(step.id)
  const cardRef = useCallback((element: HTMLElement | null) => onCardElement?.(step.id, element), [onCardElement, step.id])
  const { slice: levelSlice, levelPath } = useLevelState()
  const slice = useMemo(() => invocationSliceFor(step, levelSlice), [step, levelSlice])
  // A node with no function and no arguments has nothing to say in the info block.
  const hasInfo = step.method !== null || step.args.length > 0
  const returns = useMemo(
    () => returnType(step.type, [...step.mapOver, ...step.replicaDims]),
    [step.type, step.mapOver, step.replicaDims],
  )

  const run = useSelectedRun(step, slice)
  const views = useProjectViews(step)

  const { headRef, selectRef, tight } = useTightHead()
  const [failedOnly, toggleFailedOnly] = useFailedOnly(step)
  const counts = step.progress.counts
  const running = step.progress.glyph.shape === 'running'
  const unexpanded = counts !== null && counts.total === 0
  const showsLine = counts !== null && counts.total > 1 && counts.done + counts.failed < counts.total

  return (
    <article
      ref={cardRef}
      className={cx(
        styles.card,
        spotlit && styles.isSpotlit,
        edgeToEdge && styles.isEdgeToEdge,
        fresh && 'arrive',
      )}
    >
      {/* Keyed by the jump, so each jump replays the fade. */}
      {jumpGlow !== null && <span key={jumpGlow} className={styles.jumpGlow} aria-hidden onAnimationEnd={() => onJumpGlowEnd?.(step.id)} />}
      <div ref={headRef} className={cx(styles.head, running && styles.isRunning)} data-card-head>
        {running && <span className={cx('hatch', styles.headHatch)} aria-hidden />}
        <div ref={selectRef} className={styles.select}>
          <KindLabel kind={step.kind} iconSize={14} />
          <span className={styles.title}>{step.name}</span>
          <RunStatus step={step} failedOnly={failedOnly} onToggleFailed={toggleFailedOnly} />
        </div>
        {views.length > 0 && (
          <ViewsButton
            views={views}
            shown={viewsShown}
            className={styles.headButton}
            selectedClassName={styles.isSelected}
            compact={tight}
            onOpen={onActivate}
          />
        )}
        {showsLine && <ProgressLine counts={counts} className={styles.headLine} />}
      </div>

      {hasInfo && (
        <div>
          {step.method && (
            <section className={styles.section}>
              <div className={cx('code-method', styles.method)}>
                <span className={styles.methodName}>{step.method}</span>
                <span className={styles.returnType} title={formatType(returns)}>
                  -&gt; {typeLabel(returns)}
                </span>
              </div>
              <SourceDocs method={step.method} codeVersion={step.codeVersion} codeAt={[...levelPath, step.name]} />
            </section>
          )}

          <section className={styles.section}>
            {step.args.length > 0 ? (
              <dl className="kv kv-args">
                {step.args.map((arg) => (
                  <div key={arg.name} className="kv-pair">
                    <dt>{arg.name}</dt>
                    <dd>
                      <ArgValue
                        arg={arg}
                        mapOver={step.mapOver}
                        steps={steps}
                        onSelectStep={onSelectStep}
                      />
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <span className="detail-none">none</span>
            )}
          </section>
        </div>
      )}

      {unexpanded ? (
        <div className={cx(styles.table, styles.unexpanded)}>
          {step.progress.glyph.shape === 'waiting'
            ? 'waiting on inputs: runs once every argument above has a value (hover one to see its state)'
            : 'not run: the invocation stopped before its inputs had values'}
        </div>
      ) : (
        hasOutput(step) && (
          <div className={styles.table}>
            {step.browse && !oneRowEach(step) && <ModeSwitch step={step} />}
            <CardTable step={step} matching={step.browse ? run.total : null} onPick={onActivate} />
          </div>
        )
      )}
    </article>
  )
})

/** The modes the control offers. `keys` (`CardKeys`) is left out for now. */
const MODES: readonly { mode: CardMode; label: string; title: string }[] = [
  { mode: 'invocations', label: 'invocations', title: 'list the invocations, and what each produced' },
  { mode: 'data', label: 'data', title: 'list the rows the invocations produced' },
]

/** A run's segmented control: what its card lists at its foot (`CardTableState.mode`), a mode
 *  chosen exactly while the table is shown (`useCardMode`: choosing the one shown hides it). It
 *  floats just above the table's top edge at its right, over what is above it, taking no row of
 *  its own. */
function ModeSwitch({ step }: { step: StepDoc }) {
  const [mode, setMode] = useCardMode(step)
  return (
    <div className={styles.modes} role="radiogroup" aria-label={`What ${step.name} lists`}>
      {MODES.map((option) => (
        <button key={option.mode} role="radio" aria-checked={mode === option.mode} title={option.title}
          className={cx(styles.mode, mode === option.mode && styles.isOn)} onClick={() => setMode(option.mode)}>
          {option.label}
        </button>
      ))}
    </div>
  )
}

/** The renderers of the project's own for the step's function, each as a link to the node's panel
 *  through it, details (`isDetailView`) left out; none for a node that was not run (its
 *  invocations are what they render). */
function useProjectViews(step: StepDoc): readonly ViewLink[] {
  const { levelPath } = useLevelState()
  const renderers = useRenderers(InvocationPanel, step.browse ? step.method : null)
  return useMemo(
    () => renderers.filter((renderer) => isProjectRenderer(renderer) && !isDetailView(renderer)).map((renderer) => ({ id: renderer.id, target: viewTarget(levelPath, step, renderer.id) })),
    [renderers, levelPath, step],
  )
}

/**
 * Whether a card head's kind, name, and status would not fit beside its views button at full width,
 * so the button shows its icons alone (`ViewsButton`'s `compact`). The content's width is its
 * children's and gaps' and padding's, apart from how the head lays them out; the buttons' full
 * width is measured while they are full, and kept while they are compact.
 */
function useTightHead() {
  const headRef = useRef<HTMLDivElement>(null)
  const selectRef = useRef<HTMLDivElement>(null)
  const fullButtons = useRef(0)
  const [tight, setTight] = useState(false)
  useLayoutEffect(() => {
    const head = headRef.current
    const select = selectRef.current
    if (!head || !select) return
    const measure = () => {
      const style = getComputedStyle(select)
      const children = [...select.children] as HTMLElement[]
      const content =
        children.reduce((sum, child) => sum + child.offsetWidth, 0) +
        parseFloat(style.columnGap || '0') * Math.max(0, children.length - 1) +
        parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
      if (!tight) fullButtons.current = head.clientWidth - select.offsetWidth
      setTight(content + fullButtons.current > head.clientWidth)
    }
    // Every render measures and re-observes, so content that changed since (a status's words) counts.
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(head)
    for (const child of [...head.children, ...select.children]) observer.observe(child)
    return () => observer.disconnect()
  })
  return { headRef, selectRef, tight }
}
