import { Box, Component, Import, ReceiptText, SquareFunction, Workflow, type LucideIcon } from 'lucide-react'
import type { NodeKind } from '../types'

/**
 * The icon of each node kind, the one map every place showing a kind reads. Icons are always
 * monochrome (the muted text colour): colour already means dim kinds and run state. An icon is
 * shown with its kind word, or in a pane's head before the part of its path naming the node, never
 * alone.
 *
 * A step is one function call; a workflow is steps wired into a graph, a card you can open and go
 * inside; a source is one value the level receives (an `input` or `source_array` node), drawn as
 * something coming in rather than as a circle, which would read as a run-state glyph, or as code,
 * which is the "implemented" chip.
 */
export const KIND_ICONS: Record<NodeKind, LucideIcon> = {
  step: SquareFunction,
  workflow: Workflow,
  source: Import,
}

/** The icon of an entity (an isometric cube: a stored thing, not a node of the level), in a
 *  pane's head before the part of its path naming one. */
export const ENTITY_ICON: LucideIcon = Box

/** The icon of one invocation of a step or workflow (a receipt: the record of one call, when it ran,
 *  on what code, what went in and came out), on the button opening it from a row and in a pane's
 *  head before the step whose invocation it shows. */
export const INVOCATION_ICON: LucideIcon = ReceiptText

/** The mark of a view of the project's own (a renderer or page its view bundle adds): on a step
 *  card that has one, heading the "from this project" part of a view dropdown, which teaches what
 *  the card's mark means, and in a pane's head before the last part of its path when it shows
 *  one. */
export const CUSTOM_VIEW_ICON: LucideIcon = Component

/** Size and stroke matching the app's other Lucide icons. */
export const KIND_ICON_SIZE = 12
export const KIND_ICON_STROKE = 1.8
