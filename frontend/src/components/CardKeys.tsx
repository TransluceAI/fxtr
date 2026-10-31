import { useRef, useState } from 'react'
import { fetchRunPage } from '../api/browse'
import { useCardTableState } from '../lib/cardTable'
import { dimKind, invocationDims, invocationSliceFor, type DimKind } from '../lib/invocations'
import { againstKey, invocationKeyLoader, useDimCount, withoutDim } from '../lib/keyLists'
import { useLevelState } from '../lib/levelState'
import { nodeTarget } from '../lib/linkBase'
import { useEntityOpener } from '../lib/openEntity'
import { invocationOf } from '../model/level'
import type { KeyValue, StepDoc, StepInvocation } from '../types'
import { KeyValueList } from './KeyValueList'
import { RetryLine } from './Status'
import { cx } from '../lib/cx'
import styles from './CardKeys.module.css'

const DIM_CLASS: Record<DimKind, string> = {
  mapped: styles.dimMapped,
  replica: styles.dimReplica,
  other: styles.dimOther,
}

/**
 * A run's card in `keys` mode: a column per invocation dim (mapped, then replica), each its
 * values in the level's slice (`KeyValueList`, from the spawn table), headed by the dim's name and
 * how many values it has.
 * The columns pick one invocation together (`CardTableState.keysPick`): choosing a value in one column picks a value in every column, keeping the others' picks where
 * an invocation has them all, and otherwise taking the first invocation with the chosen value
 * (so the first value of each other column), then opens the node's panel at that invocation's
 * keys. With the table hidden (`CardTableState.rowsShown`), only the columns' heads show. The
 * picked values are drawn in selection blue, as what the panel shows. Values the other
 * picks rule out are dimmed but can be chosen. Clearing a column clears the pick.
 */
export function CardKeys({ step, onPick }: { step: StepDoc; onPick?: () => void }) {
  const source = step.browse
  if (!source) throw new Error(`${step.name} was not run, so it has no invocation keys`)
  const { slice: levelSlice, levelPath } = useLevelState()
  const { state, update } = useCardTableState(step)
  const { follow } = useEntityOpener()
  const [error, setError] = useState<{ error: unknown; retry: () => void } | null>(null)
  /** The latest choice: an earlier one answered after it is dropped. */
  const latest = useRef(0)
  const dims = invocationDims(step)
  const invocationSlice = invocationSliceFor(step, levelSlice)
  const current = { ...invocationSlice, ...state.keysPick }

  const choose = (dim: string, value: KeyValue) => {
    const wanted = { ...current, [dim]: value }
    update({ keysPick: wanted })
    setError(null)
    const request = ++latest.current
    // The other picks kept if some invocation has them all; else the level's slice; else nothing.
    firstInvocation(step, [wanted, { ...invocationSlice, [dim]: value }, { [dim]: value }]).then((invocation) => {
      if (request !== latest.current) return
      if (!invocation) throw new Error(`no invocation of ${step.name} has ${dim} = ${String(value)}`)
      update({ keysPick: invocation.key })
      follow(nodeTarget(levelPath, step, invocation.key))
      onPick?.()
    }).catch((reason: unknown) => {
      if (request === latest.current) setError({ error: reason, retry: () => choose(dim, value) })
    })
  }

  if (dims.length === 0) return <div className={styles.none}>one invocation, with no keys to pick</div>
  return (
    <div className={styles.keys}>
      {error && <RetryLine error={error.error} onRetry={error.retry} className={styles.error} />}
      <div className={styles.columns}>
        {dims.map((dim) => {
          const others = withoutDim(current, dim)
          return (
            <section key={dim} className={styles.column}>
              <ColumnHead step={step} dim={dim} />
              {/* Hidden rather than unmounted with the table, so each list keeps its search. */}
              <div hidden={!state.rowsShown}>
              <KeyValueList dim={dim} scope={JSON.stringify(['keys', source, dim])} against={againstKey(others)}
                load={invocationKeyLoader(source, dim, others)} version={step.version} enabled={state.rowsShown}
                picked={current[dim]} selection={state.keysPick !== null} chooseUnavailable onChoose={(value) => choose(dim, value)}
                onClear={() => update({ keysPick: null })} />
              </div>
            </section>
          )
        })}
      </div>
    </div>
  )
}

function ColumnHead({ step, dim }: { step: StepDoc; dim: string }) {
  const count = useDimCount(step, dim)
  return (
    <div className={cx(styles.head, DIM_CLASS[dimKind(step, dim)])}>
      <span>{dim}</span>
      <span className={styles.count}>{count ?? '…'}</span>
    </div>
  )
}

/** The first invocation (in key order) the first of `selections` to pick out any picks out; null
 *  when none does. */
async function firstInvocation(step: StepDoc, selections: readonly Record<string, KeyValue>[]): Promise<StepInvocation | null> {
  for (const selection of selections) {
    const page = await fetchRunPage(step.browse!, selection, null, 1)
    const spawn = page.items[0]
    if (spawn) return invocationOf(spawn, invocationDims(step))
  }
  return null
}
