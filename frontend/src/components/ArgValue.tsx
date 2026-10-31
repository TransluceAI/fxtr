import { useEffect, useRef, useState } from 'react'
import type { StepArg, StepDoc } from '../types'
import { SourceDocs } from './SourceDocs'
import { KIND_ICONS, KIND_ICON_SIZE, KIND_ICON_STROKE } from '../lib/kindIcons'
import { describeCounts } from '../lib/progress'
import { StateGlyph, glyphLabel } from './StateGlyph'
import { KeyPill } from './KeyPill'
import { useCardSpotlight } from '../lib/cardSpotlight'
import { cx } from '../lib/cx'
import { HoverPreview } from './HoverPreview'
import styles from './ArgValue.module.css'

interface Props {
  arg: StepArg
  /** The consuming card's mapped dims, or null where no invocation reads the argument (an
   *  awaited or returned expression), which leaves the preview's slicing line out. */
  mapOver: string[] | null
  steps: ReadonlyMap<string, StepDoc>
  onSelectStep: (id: string) => void
}

/**
 * An argument as the expression that built it: chips for the steps it reads, with the operations
 * applied to them written out between. Each chip carries its step's kind icon; hovering one shows
 * its state (the glyph, a run's ring, and its counts), so a run waiting on its inputs can be asked
 * which of them are still outstanding. Hovering also lights the step's minimap row, and when its
 * card is fully on screen lights the card up in place of the preview (`CardSpotlight`), since it
 * says more. A chip's dims are the upstream step's, in neutral text: neither the consuming step's
 * mapping nor the level's slice is shown on it. The hover preview says which dims each invocation
 * reads only its own key's rows of.
 */
export function ArgValue({ arg, mapOver, steps, onSelectStep }: Props) {
  const mapped = mapOver && mapOver.filter((dim) => arg.dims.includes(dim))
  return (
    <div className={styles.expr}>
      {arg.expr.map((token, index) => {
        if (typeof token === 'string')
          return token ? (
            <span key={index} className={styles.literal}>
              {token}
            </span>
          ) : null
        const table = steps.get(token.ref)
        if (!table)
          return (
            <span key={index} className={styles.literal}>
              {`<${token.ref.slice(0, 8)}…>`}
            </span>
          )
        return (
          <ArgTableChip
            key={index}
            table={table}
            mapped={mapped}
            onSelectStep={onSelectStep}
          />
        )
      })}
    </div>
  )
}

function ArgTableChip({
  table,
  mapped,
  onSelectStep,
}: {
  table: StepDoc
  mapped: string[] | null
  onSelectStep: (id: string) => void
}) {
  const sliced = mapped && table.dims.filter((dim) => mapped.includes(dim))
  const KindIcon = KIND_ICONS[table.kind]
  const spotlight = useCardSpotlight()
  const [previewOpen, setPreviewOpen] = useState(false)
  /** Whether the chip is hovered, and so has the step lit up. */
  const hoveredRef = useRef(false)
  /** Whether hovering lit the step's card, which then stands in for the preview. */
  const litRef = useRef(false)
  const putOut = () => {
    if (hoveredRef.current) spotlight?.clear(table.id)
    hoveredRef.current = false
    litRef.current = false
  }
  useEffect(() => putOut, [spotlight, table.id])
  return (
    <HoverPreview open={previewOpen} onOpenChange={(open) => setPreviewOpen(open && !litRef.current)}
      className={styles.preview} side="top" align="start"
      trigger={
        <button
          className={cx('chip', styles.chip)}
          onClick={() => onSelectStep(table.id)}
          onMouseEnter={() => {
            hoveredRef.current = true
            litRef.current = spotlight?.light(table.id) ?? false
          }}
          onMouseLeave={putOut}
        >
          <KindIcon size={KIND_ICON_SIZE} strokeWidth={KIND_ICON_STROKE} className={styles.kindIcon} aria-label={table.kind} />
          <span className={styles.chipName}>{table.name}</span>
          {table.dims.length > 0 && (
            <span className={styles.keys} aria-label={`keys: ${table.dims.join(', ')}`}>
              {table.dims.map((dim, index) => (
                // The brackets and commas ride with a dim, so a wrapped line never starts with one.
                <span key={dim} className={styles.dim}>
                  {index === 0 && '['}
                  {dim}
                  {index === table.dims.length - 1 ? ']' : ','}
                </span>
              ))}
            </span>
          )}
        </button>
      }>
      <ArgTablePreview table={table} sliced={sliced} />
    </HoverPreview>
  )
}

function ArgTablePreview({ table, sliced }: { table: StepDoc; sliced: string[] | null }) {
  return (
    <>
      <div className={styles.previewHead}>
        <StateGlyph glyph={table.progress.glyph} counts={table.progress.counts} spin r={5.5} />
        <span className={styles.previewName}>
          {table.name}
          {table.dims.length > 0 && <span className={styles.previewShape}> [{table.dims.join(' × ')}]</span>}
        </span>
        {!(table.progress.glyph.shape === 'done' && !table.progress.glyph.failed) && (
          <span className={styles.previewState}>
            {table.progress.counts && table.progress.counts.total > 0
              ? describeCounts(table.progress.counts)
              : glyphLabel(table.progress.glyph)}
          </span>
        )}
      </div>
      <SourceDocs method={table.method} codeVersion={table.codeVersion} />
      {sliced?.length === 0 && <div className={styles.mapping}>Each invocation sees all rows</div>}
      {sliced && sliced.length > 0 && (
        <div className={styles.mapping}>
          Each invocation only sees rows matching its own {sliced.length === 1 ? 'key' : 'keys'} for
          {sliced.map((dim) => (
            <span key={dim}>
              {' '}
              <KeyPill dim={dim} />
            </span>
          ))}
        </div>
      )}
    </>
  )
}
