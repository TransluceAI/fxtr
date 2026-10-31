/**
 * This shell's `useLink`: a link from a renderer to what a `ViewLinkSpec` names (a view, a location
 * relative to the view it is drawn in, `panes/address.ts`, or both), opened at `params` through the
 * nearest `OpenEntityContext` like any link: a click shows it from the view (in the browsing panel
 * after the view, or with Alt in a pinned panel), hovering lights where it would open, and dragging
 * carries it. A relative location needs a view with an address of its own: a panel, or the centre.
 * Anywhere else a relative link is null, and
 * the renderer shows its detail in place.
 */

import { useContext, useMemo } from 'react'
import type { ViewLink, ViewLinkSpec, ViewParams } from 'fxtr-view'
import { OpenEntityContext, dragLink, type EntityOpener } from '../lib/openEntity'
import { parseRelLocation } from '../panes/address'
import type { LinkTarget } from '../panes/drag'

/** What `spec` opens from `opener`'s view at `params`; null where a relative location has no address
 *  to start from. A view declared within another may be linked to only from that one: a link from
 *  anywhere else is the renderer's bug, and throws. A malformed location throws too. */
export function specTarget(spec: ViewLinkSpec, params: ViewParams, opener: EntityOpener): LinkTarget | null {
  if (spec.within !== null && opener.viewId !== spec.within)
    throw new Error(`${spec.view} is declared within ${spec.within}, and only that view links to it (this link is in ${opener.viewId ?? 'no view'})`)
  const location = spec.to === null ? { kind: 'same' as const } : parseRelLocation(spec.to)
  if (location.kind !== 'absolute' && !opener.relative) return null
  return { rel: { view: spec.view, location }, params }
}

export function useLink(spec: ViewLinkSpec | null, params: ViewParams): ViewLink | null {
  const opener = useContext(OpenEntityContext)
  // A malformed address is the renderer's bug: it throws, to the renderer's error boundary.
  const link = useMemo(() => (spec === null || !opener ? null : specTarget(spec, params, opener)),
    [spec?.view, spec?.within, spec?.to, JSON.stringify(params), opener])
  if (!link || !opener) return null
  const state = opener.linkState(link)
  return {
    props: {
      onClick: (event) => {
        event.stopPropagation()
        opener.follow(link, { pin: event.altKey })
      },
      onMouseEnter: () => opener.hover(link),
      onMouseLeave: () => opener.hover(null),
      draggable: true,
      onDragStart: (event) => {
        event.stopPropagation()
        dragLink(opener, event, link)
      },
    },
    open: state.open,
    selected: state.selected,
  }
}
