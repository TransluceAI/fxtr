import type { ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import styles from './ValueModal.module.css'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  subtitle?: string
  children: ReactNode
}

/** The full detail of something a cell only had room to summarize. */
export function ValueModal({ open, onOpenChange, title, subtitle, children }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.modal}>
          <div className={styles.head}>
            <Dialog.Title className={styles.title}>{title}</Dialog.Title>
            {subtitle && <span className={styles.sub}>{subtitle}</span>}
            <Dialog.Close className="icon-button detail-close" aria-label="Close">
              <X size={13} />
            </Dialog.Close>
          </div>
          <div className={styles.body}>{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
