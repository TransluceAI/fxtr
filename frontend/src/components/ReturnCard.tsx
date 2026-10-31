import { isLive } from '../lib/progress'
import type { Level, StepDoc } from '../types'
import { ArgValue } from './ArgValue'
import { CardTable } from './CardTable'
import { PanelButton } from './PanelButton'
import { returnedTarget } from '../lib/linkBase'
import { useLevelState } from '../lib/levelState'
import { cx } from '../lib/cx'
import styles from './ReturnCard.module.css'

interface Props {
  level: Level
  steps: ReadonlyMap<string, StepDoc>
  /** A pane is showing the returned value. */
  valueShown: boolean
  onSelectStep: (id: string) => void
}

/**
 * The last card of a level: what the workflow returned, as the expression over its steps. A chip
 * names each step read, and selects it. When the returned node is derived, no other card holds its
 * value, so the head has a button opening its panel, its rows are the card's table
 * (`CardTable`). Until the level has a result the card says why: the body is still running, or it
 * has returned and the level waits on nodes still being computed.
 */
export function ReturnCard({ level, steps, valueShown, onSelectStep }: Props) {
  const { result } = level
  const table = result?.table ?? null
  const pending = pendingReason(level)
  return (
    <article className={styles.card}>
      <div className={styles.head}>
        <div className={styles.select}>
          <span className={cx('kind-label', styles.kind)}>returned</span>
          {result && (
            <ArgValue
              arg={{ name: 'value', expr: result.expr, dims: [] }}
              mapOver={null}
              steps={steps}
              onSelectStep={onSelectStep}
            />
          )}
          {pending && <span className={styles.pending}>{pending}</span>}
        </div>
        {table?.output && <ValueButton table={table} shown={valueShown} />}
      </div>
      {table?.output && (
        <div className={styles.table}>
          <CardTable step={table} matching={null} />
        </div>
      )}
    </article>
  )
}
/** The button opening the returned value's panel. */
function ValueButton({ shown }: { table: StepDoc; shown: boolean }) {
  const { levelPath } = useLevelState()
  return (
    <PanelButton
      target={returnedTarget(levelPath)}
      shown={shown}
      className={styles.panelButton}
      selectedClassName={styles.isSelected}
    />
  )
}

/** Why the level has no result yet, or null once it has one. */
function pendingReason(level: Level): string | null {
  if (level.resultCid !== null) return null
  if (level.result) {
    const unsettled = level.steps.filter((step) => isLive(step.progress.glyph.shape)).length
    return unsettled > 0 ? `waiting on ${unsettled} ${unsettled === 1 ? 'node' : 'nodes'}` : 'finishing'
  }
  return level.live ? 'running' : `not returned: the invocation is ${level.status.state}`
}
