import type { AnyRenderer } from 'fxtr-view'
import { chosenRenderer } from 'fxtr-view-shell-util'
import { isProjectRenderer } from '../views/registry'
import { ViewSelect } from './ViewSelect'

interface Props {
  /** The renderers `useRenderers` lists for the slot and subject type, shown by id. */
  choices: readonly AnyRenderer[]
  /** When set, a view of the host's own is offered first under this label, chosen as null. */
  hostDefaultLabel?: string
  /** The chosen renderer's id; null for the host's own view (or, without one, the first choice). */
  value: string | null
  onChange: (renderer: string | null) => void
}

const HOST_DEFAULT = 'host'
const rendererKey = (id: string) => `renderer:${id}`

/** A `ViewSelect` choosing how something is shown: the host's own view when it has one, then each
 *  renderer the registry offers for the subject type, the fallback included. A remembered id that
 *  is gone reads as the host's view or the first choice. */
export function RendererSelect({ choices, hostDefaultLabel: hostDefault, value, onChange }: Props) {
  const chosen = chosenRenderer(choices, value)
  const first = choices[0]
  const shown = chosen !== null ? rendererKey(chosen) : hostDefault === undefined && first ? rendererKey(first.id) : HOST_DEFAULT
  const options = [
    ...(hostDefault === undefined ? [] : [{ key: HOST_DEFAULT, label: hostDefault, fromProject: false }]),
    ...choices.map((choice) => ({ key: rendererKey(choice.id), label: choice.id, fromProject: isProjectRenderer(choice), renderer: choice })),
  ]
  return (
    <ViewSelect
      options={options}
      value={shown}
      onChange={(key) => {
        if (key === HOST_DEFAULT) return onChange(null)
        const choice = choices.find((candidate) => rendererKey(candidate.id) === key)
        if (!choice) throw new Error(`no renderer for ${key}`)
        onChange(choice.id)
      }}
    />
  )
}
