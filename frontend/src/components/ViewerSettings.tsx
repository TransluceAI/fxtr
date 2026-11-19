import * as Popover from '@radix-ui/react-popover'
import { Settings } from 'lucide-react'
import { useRef } from 'react'
import { viewerConfig } from '../config'
import { ThemeToggle } from './ThemeToggle'
import styles from './ViewerSettings.module.css'

/** Viewer preferences, an optional notice, and a configured details action. */
export function ViewerSettings() {
  const { notice, detailsAction } = viewerConfig()
  // The details action hands the keyboard to the page that frames the viewer (a hub's details
  // dialog). Closing the menu normally returns focus to its trigger, which would take the
  // keyboard back into the frame; after the details action, the menu leaves focus alone.
  const handedOff = useRef(false)
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button className={styles.trigger} aria-label="Viewer settings" title="Settings">
          <Settings size={16} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className={`popover ${styles.menu}`}
          side="top"
          align="start"
          sideOffset={8}
          onCloseAutoFocus={(event) => {
            if (!handedOff.current) return
            handedOff.current = false
            event.preventDefault()
          }}
        >
          <h2 className={styles.heading}>Settings</h2>
          <ThemeToggle />
          {notice && <div className={styles.notice} role="note">{notice}</div>}
          {detailsAction && (
            <Popover.Close asChild>
              <button
                className={styles.details}
                onClick={() => {
                  handedOff.current = true
                  detailsAction.run()
                }}
              >
                {detailsAction.label}
              </button>
            </Popover.Close>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
