/**
 * This shell's `EntityLinkImpl`: a metadata-labelled entity chip. Clicking follows the link through
 * the nearest `OpenEntityContext` (a link with a place opens the value at its path in what its view
 * shows, `lib/linkBase.ts`; any other its entity, fixed), hovering lights where it would open and,
 * after a moment, shows the entity's `EntityHoverPreview` view in a card, followed by the full cid,
 * and dragging carries the link into the columns. Alt-clicking opens a pinned panel. The link is lit
 * in selection blue while the browsing panel after its view shows what it names, and faintly while
 * a pinned panel does; the lit one is marked for `j`/`k` in a table (`lib/tableKeys.ts`).
 */

import { useMemo, useState } from 'react'
import { CidText, EntityHoverPreview, type EntityLinkImplProps } from 'fxtr-view'
import { RenderEntity } from 'fxtr-view-shell-util'
import { dragLink, useEntityOpener } from '../lib/openEntity'
import { linkTarget } from '../lib/linkBase'
import type { LinkTarget } from '../panes/drag'
import { specTarget } from './viewLink'
import { cx } from '../lib/cx'
import { HoverPreview } from '../components/HoverPreview'
import { CHIP_ATTRIBUTE, CHIP_SELECTED } from '../lib/tableKeys'
import styles from './EntityLinkImpl.module.css'
import { EntityLabel } from './EntityLabel'

export function EntityLinkImpl({ id, place, to }: EntityLinkImplProps) {
  const opener = useEntityOpener()
  const { follow, hover, linkState } = opener
  const [previewOpen, setPreviewOpen] = useState(false)
  // What the view named opens in place of the entity's own place, where it can be followed from
  // here (a relative location needs a view with an address).
  const named = useMemo(() => (to === undefined ? null : specTarget(to.spec, to.params, opener)),
    [to?.spec.view, to?.spec.within, to?.spec.to, JSON.stringify(to?.params), opener])
  const target: LinkTarget = named ?? linkTarget(id.cid, place)
  const state = linkState(target)
  const className = cx('chip', styles.link, state.selected ? styles.isOpen : state.open && styles.isPinned)
  // A link inside a selectable row opens the link, not the row.
  const onClick = (event: React.MouseEvent) => {
    event.stopPropagation()
    follow(target, { pin: event.altKey })
  }
  // The pointer is captured for the length of a drag, so the preview has to be dismissed by hand.
  const onDragStart = (event: React.DragEvent) => {
    event.stopPropagation()
    setPreviewOpen(false)
    dragLink(opener, event, target)
  }
  return (
    <HoverPreview open={previewOpen} onOpenChange={setPreviewOpen} className={cx('popover', styles.preview)} side="top" align="start"
      trigger={
        <button
          className={className}
          {...{ [CHIP_ATTRIBUTE]: state.selected ? CHIP_SELECTED : '' }}
          title={id.cid}
          onClick={onClick}
          onMouseEnter={() => hover(target)}
          onMouseLeave={() => hover(null)}
          draggable
          onDragStart={onDragStart}
        >
          <EntityLabel cid={id.cid} />
        </button>
      }>
      {previewOpen && <RenderEntity slot={EntityHoverPreview} id={id} />}
      <div className={styles.previewCid}>
        <CidText cid={id.cid} />
      </div>
    </HoverPreview>
  )
}
