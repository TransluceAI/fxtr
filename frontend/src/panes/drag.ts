/**
 * Dragging into the detail view's columns. The payload is either a link (what it opens, and the
 * view it was dragged from) or an open panel being moved, as JSON under a private MIME type, so a
 * link stays a plain `draggable` element and the drop targets are plain handlers.
 */

import { formatRel, type Params, type Rel } from './address'

const DRAG_MIME = 'application/x-fxtr-frame'

/** What a link opens: an address relative to the view it is in (`rel`), and the params it is opened
 *  at (a row's or an invocation's full keys: following it keeps what they add to the view's). */
export interface LinkTarget {
  rel: Rel
  params: Params
}

export type DragPayload =
  | { kind: 'link'; target: LinkTarget; originId: string }
  | { kind: 'move'; panelId: string }

export function setLinkDrag(dataTransfer: DataTransfer, target: LinkTarget, originId: string): void {
  const payload: DragPayload = { kind: 'link', target, originId }
  dataTransfer.setData(DRAG_MIME, JSON.stringify(payload))
  dataTransfer.setData('text/plain', formatRel(target.rel, target.params))
  dataTransfer.effectAllowed = 'copyMove'
}

export function setPanelDrag(dataTransfer: DataTransfer, panelId: string): void {
  const payload: DragPayload = { kind: 'move', panelId }
  dataTransfer.setData(DRAG_MIME, JSON.stringify(payload))
  dataTransfer.effectAllowed = 'move'
}

/** Whether a drag carries a payload, testable during `dragover` where `getData` is not yet legal. */
export function hasFrameDrag(dataTransfer: DataTransfer): boolean {
  return Array.from(dataTransfer.types).includes(DRAG_MIME)
}

export function getDragPayload(dataTransfer: DataTransfer): DragPayload | null {
  const json = dataTransfer.getData(DRAG_MIME)
  if (!json) return null
  try {
    const value = JSON.parse(json) as DragPayload
    if (value.kind === 'move' && typeof value.panelId === 'string') return value
    if (value.kind === 'link' && typeof value.target?.rel?.location?.kind === 'string' && typeof value.originId === 'string') return value
  } catch {
    // Not JSON: not a payload.
  }
  return null
}
