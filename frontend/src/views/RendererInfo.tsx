/**
 * "Which renderer is this?" — the info icon ending a renderer's row in a view dropdown
 * (`ViewSelect`), and the card it shows on hover naming the registry component behind that view.
 * A view is drawn by code its panel never mentions, so without this the only way to tell is to read
 * the registry.
 *
 * This is shell-side on purpose: dispatch knows nothing about chrome around its result. Each
 * dropdown hands its renderers' rows the renderer they choose, so the icon names exactly what that
 * row would show.
 */

import { Info } from 'lucide-react'
import { ANY_SUBJECT_TYPE, type AnyRenderer } from 'fxtr-view'
import { HoverPreview } from '../components/HoverPreview'
import { cx } from '../lib/cx'
import styles from './RendererInfo.module.css'

/** Stop a pointer event at the icon, so pointing at it or clicking it does not choose its row. */
const stop = (event: React.SyntheticEvent) => event.stopPropagation()

/** The icon naming `renderer` (its component, the type it is registered for, or that it takes any
 *  type) in a card while the pointer is on it. */
export function RendererInfo({ renderer }: { renderer: AnyRenderer }) {
  const name = renderer.Component.displayName || renderer.Component.name || '(anonymous)'
  const registered = renderer.subjectType !== ANY_SUBJECT_TYPE
  return (
    <HoverPreview side="right" align="start" className={cx('popover', styles.info)}
      trigger={
        <span className={styles.trigger} role="img" aria-label={`Renderer: ${name}`}
          onPointerDown={stop} onPointerUp={stop} onClick={stop}>
          <Info size={11} />
        </span>
      }>
      <div className="eyebrow">Renderer</div>
      <dl className={styles.fields}>
        <div className={styles.field}>
          <dt>name</dt>
          <dd className={styles.name}>{name}</dd>
        </div>
        <div className={styles.field}>
          <dt>type</dt>
          <dd>{registered ? renderer.subjectType : 'any'}</dd>
        </div>
        <div className={styles.field}>
          <dt>match</dt>
          <dd>{registered ? 'registered for this type' : 'a renderer for any type'}</dd>
        </div>
      </dl>
    </HoverPreview>
  )
}
