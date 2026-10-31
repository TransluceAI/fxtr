import { InvocationPanel } from 'fxtr-view'
import { useRenderers } from 'fxtr-view-shell-util'
import { setLinkDrag, type LinkTarget } from '../panes/drag'
import { workflowViewTarget } from '../lib/linkBase'
import { RAIL } from '../panes/columns'
import { useLevelState } from '../lib/levelState'
import { cx } from '../lib/cx'
import { isDetailView } from '../views/registry'
import styles from './LevelViews.module.css'

interface Props {
  /** The registered name of the workflow on screen. */
  workflowName: string
  /** The renderer showing the level, or null for the trace. */
  active: string | null
  onChange: (renderer: string | null) => void
}

/**
 * Under the minimap's rows: the workflow's custom views.
 *
 * A **custom view** is an `InvocationPanel` renderer registered for the workflow on screen. Choosing
 * one shows the level through it in place of the trace; choosing the active one again returns to the
 * trace. A row can also be dragged into the detail view to open the workflow there through it.
 *
 * Pages are not listed: they are reached through the links that open them.
 */
export function LevelViews({ workflowName, active, onChange }: Props) {
  const renderers = useRenderers(InvocationPanel, workflowName).filter((renderer) => !isDetailView(renderer))
  const { levelPath } = useLevelState()
  const viewTarget = (id: string): LinkTarget => workflowViewTarget(levelPath, id)
  if (renderers.length === 0) return null
  return (
    <nav className={styles.views} aria-label="Custom views">
      <h2 className={styles.heading}>custom views</h2>
      {renderers.map((renderer) => (
        <button
          key={renderer.id}
          className={cx(renderer.id === active && styles.isActive)}
          aria-current={renderer.id === active ? 'page' : undefined}
          draggable
          title="drag into the detail view to open it there"
          onClick={() => onChange(renderer.id === active ? null : renderer.id)}
          onDragStart={(event) => setLinkDrag(event.dataTransfer, viewTarget(renderer.id), RAIL)}
        >
          <span>{renderer.id}</span>
        </button>
      ))}
    </nav>
  )
}
