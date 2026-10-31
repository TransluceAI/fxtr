import type { AnyRenderer } from 'fxtr-view'
import { isProjectRenderer } from '../views/registry'
import { OUTPUT_VIEW, type PanelView } from './levelState'

/** A view as the string a dropdown option carries. */
export function panelViewKey(view: PanelView): string {
  switch (view.kind) {
    case 'output':
      return 'output'
    case 'outputRenderer':
      return `output:${view.renderer}`
    case 'invocation':
      return `invocation:${view.renderer}`
  }
}

/** Every view a node's panel offers, with its label and whether it is a renderer of the project's
 *  own, and the renderer a view is: the output with no renderer (`output (raw)`), then each renderer
 *  of the output's entity type, then each renderer of the step. */
export function panelViewOptions(
  outputRenderers: readonly AnyRenderer[],
  invocationRenderers: readonly AnyRenderer[],
): { view: PanelView; label: string; fromProject: boolean; renderer?: AnyRenderer }[] {
  return [
    { view: OUTPUT_VIEW, label: 'output (raw)', fromProject: false },
    ...outputRenderers.map((r) => ({ view: { kind: 'outputRenderer', renderer: r.id } as const, label: `output ${r.id}`, fromProject: isProjectRenderer(r), renderer: r })),
    ...invocationRenderers.map((r) => ({ view: { kind: 'invocation', renderer: r.id } as const, label: `invocation ${r.id}`, fromProject: isProjectRenderer(r), renderer: r })),
  ]
}

/** The view a node's panel shows until one is chosen: the first of the step's own invocation
 *  renderers, else the first of the output type's own renderers, else the output. Only renderers
 *  of the project's own count (`isProjectRenderer`), not the viewer's defaults. */
export function defaultPanelView(
  outputRenderers: readonly AnyRenderer[],
  invocationRenderers: readonly AnyRenderer[],
): PanelView {
  const invocation = invocationRenderers.find(isProjectRenderer)
  if (invocation) return { kind: 'invocation', renderer: invocation.id }
  const output = outputRenderers.find(isProjectRenderer)
  if (output) return { kind: 'outputRenderer', renderer: output.id }
  return OUTPUT_VIEW
}
