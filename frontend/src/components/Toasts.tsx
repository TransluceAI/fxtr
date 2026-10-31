import { X } from 'lucide-react'
import { Toaster } from 'sonner'
import styles from './Toasts.module.css'

/**
 * Where the viewer's toasts appear (sonner's `toast()`; `lib/launches.tsx` raises them): the top
 * right corner, each drawn like the viewer's other floating cards. sonner positions, stacks, and
 * animates them, and pauses a toast's countdown while it is hovered or the page is hidden, so one
 * raised while the user is in another window waits for them. The classes here are all of their look.
 */
export function Toasts() {
  return (
    <Toaster
      position="top-right"
      // sonner's own theme colours only its styled toasts. These are unstyled, in the tokens'
      // colours, which follow the page's theme; light leaves them nothing to override.
      theme="light"
      closeButton
      icons={{ close: <X size={10} aria-hidden /> }}
      toastOptions={{
        unstyled: true,
        classNames: {
          toast: styles.toast,
          icon: styles.icon,
          content: styles.content,
          title: styles.title,
          description: styles.description,
          actionButton: styles.action,
          closeButton: styles.close,
        },
      }}
    />
  )
}
