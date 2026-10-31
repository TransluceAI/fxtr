import { useEffect, type RefObject } from 'react'
import { useStableCallback } from './store'

/** While `open`, call `dismiss` on a press outside `ref`'s element or on Escape. */
export function useDismiss(ref: RefObject<HTMLElement>, open: boolean, dismiss: () => void) {
  const onDismiss = useStableCallback(dismiss)
  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onDismiss()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onDismiss()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open, ref, onDismiss])
}
