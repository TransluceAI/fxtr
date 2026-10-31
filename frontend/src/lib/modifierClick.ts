/**
 * Holding the platform's primary modifier makes actions such as row slicing available through a
 * click (see `keyboard.ts`). While that modifier is held, the document's root
 * carries `data-modifier-held` (and `data-alt-held` while Alt is, for Alt-clicks), so what such a
 * click acts on can show itself clickable in CSS alone, with nothing re-rendering; `useModifierHeld` keeps the attribute current while anything
 * mounted uses it. Pointer movement corrects it too, should a key's release go unseen.
 */

import { useEffect } from 'react'
import { hasPrimaryModifier } from './keyboard'

let users = 0

function setFlag(name: 'modifierHeld' | 'altHeld', held: boolean) {
  const root = document.documentElement
  if (Object.hasOwn(root.dataset, name) === held) return
  if (held) root.dataset[name] = ''
  else delete root.dataset[name]
}

const sync = (event: KeyboardEvent | MouseEvent) => {
  setFlag('modifierHeld', hasPrimaryModifier(event))
  setFlag('altHeld', event.altKey)
}
const release = () => {
  setFlag('modifierHeld', false)
  setFlag('altHeld', false)
}

/** Keep the root's `data-modifier-held` and `data-alt-held` current while `enabled`. */
export function useModifierHeld(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return
    if (users++ === 0) {
      window.addEventListener('keydown', sync)
      window.addEventListener('keyup', sync)
      window.addEventListener('mousemove', sync, { passive: true })
      window.addEventListener('blur', release)
    }
    return () => {
      if (--users > 0) return
      window.removeEventListener('keydown', sync)
      window.removeEventListener('keyup', sync)
      window.removeEventListener('mousemove', sync)
      window.removeEventListener('blur', release)
      release()
    }
  }, [enabled])
}
