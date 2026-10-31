import { createContext, useContext } from 'react'
import { setLinkDrag, type LinkTarget } from '../panes/drag'
import type { LinkState } from '../panes/useColumns'

/**
 * How a link opens what it names, from where it is: inside a panel, or a root of the viewer (the
 * trace, the rail), whose id is `originId`. Clicking follows the link (`follow`; Alt-clicking opens
 * a pinned panel), hovering lights where it would open (`hover`, null when the pointer leaves), and
 * `linkState` says whether what it names is open, and in the browsing panel after this view, which
 * lights the link. Provided by the nearest enclosing panel, or around the trace.
 */
export interface EntityOpener {
  originId: string
  /** The view the links are in (null: none, or the default), which alone links to the views
   *  declared within it. */
  viewId: string | null
  /** Relative links have an address to start from here: in a panel. */
  relative: boolean
  follow: (target: LinkTarget, options?: { pin?: boolean }) => void
  hover: (target: LinkTarget | null) => void
  linkState: (target: LinkTarget) => LinkState
}

export const OpenEntityContext = createContext<EntityOpener | null>(null)

export function useEntityOpener(): EntityOpener {
  const opener = useContext(OpenEntityContext)
  if (!opener) throw new Error('a link was rendered outside an OpenEntityContext provider')
  return opener
}

/** Start dragging a link to `target` from where `opener` is: it carries the link into the detail
 *  view's columns, which open it where it is dropped, and lights nothing while it is dragged. */
export function dragLink(opener: EntityOpener, event: React.DragEvent, target: LinkTarget): void {
  opener.hover(null)
  setLinkDrag(event.dataTransfer, target, opener.originId)
}
