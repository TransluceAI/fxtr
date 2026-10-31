import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import * as Popover from '@radix-ui/react-popover'
import { shortName } from 'fxtr-view-cbor-tree'
import { dragLink, useEntityOpener } from '../lib/openEntity'
import { CUSTOM_VIEW_ICON, KIND_ICON_STROKE } from '../lib/kindIcons'
import type { LinkTarget } from '../panes/drag'
import { cx } from '../lib/cx'
import styles from './PanelButton.module.css'

interface Props {
  /** The node's panel. */
  target: LinkTarget
  /** A pane is showing the node, which lights the button. */
  shown: boolean
  className: string
  selectedClassName: string
  /** Leads with the custom-view icon: the panel is a view of the project's own. */
  custom?: boolean
  /** What the button says. */
  label?: string
  title?: string
  /** Also run on a click, before the panel opens. */
  onOpen?: () => void
  /** Ends in a right chevron; not when it is a segment beside another that ends in one. */
  chevron?: boolean
  /** Leaves the label out, the custom-view icon standing for it (with `custom`). */
  compact?: boolean
}

/** The props making an element a link to `target` from the trace: a click opens it as the trace's
 *  links open (lit while hovered), a drag carries it into the columns, and an Alt-click opens a
 *  pinned panel. */
function useLinkProps(target: LinkTarget, onOpen?: () => void) {
  const opener = useEntityOpener()
  const { follow, hover } = opener
  return {
    draggable: true,
    onClick: (event: React.MouseEvent) => {
      onOpen?.()
      follow(target, { pin: event.altKey })
    },
    onMouseEnter: () => hover(target),
    onMouseLeave: () => hover(null),
    onDragStart: (event: React.DragEvent) => dragLink(opener, event, target),
  }
}

function CustomIcon({ label }: { label: string }) {
  const Icon = CUSTOM_VIEW_ICON
  return (
    <span role="img" aria-label={label} title={label} className={styles.custom}>
      <Icon size={12} strokeWidth={KIND_ICON_STROKE} />
    </span>
  )
}

/** The button in a card's head that opens a panel in the detail view (`open` unless labelled), as
 *  a link from the trace (`useLinkProps`). */
export function PanelButton({ target, shown, className, selectedClassName, custom = false, label = 'open', title, onOpen, chevron = true, compact = false }: Props) {
  return (
    <button className={cx(className, shown && selectedClassName)} title={title ?? 'click or drag to open in the detail view'}
      {...useLinkProps(target, onOpen)}>
      {custom && <CustomIcon label="a view from this project" />}
      {!compact && <span>{label}</span>}
      {chevron && <ChevronRight size={12} />}
    </button>
  )
}

/** One view a `ViewsButton` opens. */
export interface ViewLink {
  id: string
  target: LinkTarget
}

/**
 * The buttons in a step card's head opening the step's views of the project's own, named by the
 * last part of their ids (`shortName`), each a link from the trace. For one, a button named for
 * it, opening it (`PanelButton`). For several, two segments: the current view's button (the first
 * view until another is chosen), and beside it a button with the views' count and a chevron, whose
 * menu lists them all; choosing one there opens it and makes it the current view. A segment is lit
 * while a panel shows a view it stands for (`shown`, by view id): the first, its view; the menu's,
 * any other, and each item while a panel shows it. The caller shows none for a step without views.
 * `compact` (a card head short of room) leaves out the view's name and the count, so the first
 * segment is the custom-view icon and the menu's the chevron alone.
 */
export function ViewsButton({ views, shown, className, selectedClassName, compact = false, onOpen }: {
  views: readonly ViewLink[]
  shown: readonly string[]
  className: string
  selectedClassName: string
  compact?: boolean
  onOpen?: () => void
}) {
  const [chosen, setChosen] = useState<string | null>(null)
  if (views.length === 0) throw new Error('a views button needs a view to open')
  const current = views.find((view) => view.id === chosen) ?? views[0]!
  const main = (
    <PanelButton target={current.target} shown={shown.includes(current.id)} custom label={shortName(current.id)} className={className}
      selectedClassName={selectedClassName} title={`click or drag to open ${current.id} in the detail view`} onOpen={onOpen}
      chevron={views.length === 1} compact={compact} />
  )
  if (views.length === 1) return main
  return (
    <>
      {main}
      <Popover.Root>
        <Popover.Trigger asChild>
          <button className={cx(className, styles.more, views.some((view) => view.id !== current.id && shown.includes(view.id)) && selectedClassName)}
            title={`this step's ${views.length} views from the project`} aria-label={`${views.length} views`}>
            {!compact && <span className={styles.count}>{views.length}</span>}
            <ChevronDown size={12} />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content className={cx('popover', styles.menu)} side="bottom" align="end" sideOffset={4}>
            {views.map((view) => (
              <ViewItem key={view.id} view={view} shown={shown.includes(view.id)}
                onOpen={() => {
                  setChosen(view.id)
                  onOpen?.()
                }} />
            ))}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </>
  )
}

function ViewItem({ view, shown, onOpen }: { view: ViewLink; shown: boolean; onOpen: () => void }) {
  return (
    <Popover.Close asChild>
      <button className={cx(styles.item, shown && styles.isShown)} title={`click or drag to open ${view.id} in the detail view`}
        {...useLinkProps(view.target, onOpen)}>
        {shortName(view.id)}
      </button>
    </Popover.Close>
  )
}
