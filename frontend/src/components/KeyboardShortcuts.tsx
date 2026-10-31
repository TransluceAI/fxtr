import { useEffect, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Search, X } from 'lucide-react'
import { hasPrimaryModifier, primaryModifierLabel } from '../lib/keyboard'
import styles from './KeyboardShortcuts.module.css'

interface Shortcut {
  keys: string[][]
  action: string
  context: string
}

/** Keep each entry with the control that implements it in mind; these describe the viewer's
 *  bindings, not bindings a project's custom renderer may provide. */
const shortcutGroups = (modifier: string): { title: string; shortcuts: Shortcut[] }[] => [
  {
    title: 'General',
    shortcuts: [
      { keys: [[modifier, ',']], action: 'Open keyboard shortcuts', context: 'Anywhere in the IDE, including text fields and dialogs.' },
      { keys: [['Esc']], action: 'Dismiss a dialog or popover', context: 'While a dialog, menu, slice panel, or key-column list is open.' },
      { keys: [['Tab'], ['Shift', 'Tab']], action: 'Focus the next / previous control', context: 'Moves between buttons, links, and inputs. Stays inside an open dialog.' },
      { keys: [['Enter'], ['Space']], action: 'Activate a button', context: 'When a button has keyboard focus. Links use Enter.' },
    ],
  },
  {
    title: 'Slicing',
    shortcuts: [
      { keys: [['j'], ['k']], action: 'Choose the next / previous dimension value', context: 'Hover a value list in slice dims or a card’s key columns. Only while not typing; skips values that cannot be chosen and stops at either end.' },
      { keys: [[modifier, 'click']], action: 'Slice the level at a row’s keys', context: 'Click a row in a step card’s table. Applies that row’s dimension values across the level.' },
    ],
  },
  {
    title: 'Tables',
    shortcuts: [
      { keys: [['Enter']], action: 'Go to a row', context: 'In a table’s “Go to row” input, opens the page containing the entered row number.' },
      { keys: [['Esc']], action: 'Cancel a row-number edit', context: 'In the “Go to row” input, restores the current row number.' },
    ],
  },
]

/** Mounted outside job loading and error boundaries so help is always available. The capture
 *  listener reaches the shortcut before an input, renderer, or open menu can consume it. */
export function KeyboardShortcuts() {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  const previousFocus = useRef<HTMLElement | SVGElement | null>(null)
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== ',' || !hasPrimaryModifier(event) || event.altKey || event.shiftKey || event.isComposing) return
      event.preventDefault()
      event.stopPropagation()
      if (open || event.repeat) return
      const active = document.activeElement
      previousFocus.current = active instanceof HTMLElement || active instanceof SVGElement ? active : null
      setSearch('')
      setOpen(true)
    }
    window.addEventListener('keydown', onKeyDown, true)
    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [open])

  const modifier = primaryModifierLabel()
  const needle = search.trim().toLowerCase()
  const groups = shortcutGroups(modifier).map((group) => ({
    ...group,
    shortcuts: group.shortcuts.filter((shortcut) =>
      `${group.title} ${shortcut.action} ${shortcut.context} ${shortcut.keys.flat().join(' ')}`.toLowerCase().includes(needle)),
  })).filter((group) => group.shortcuts.length > 0)

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={styles.dialog}
          onOpenAutoFocus={(event) => {
            event.preventDefault()
            searchInput.current?.focus()
          }}
          onKeyDown={(event) => event.stopPropagation()}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            if (previousFocus.current?.isConnected) previousFocus.current.focus()
          }}
        >
          <div className={styles.header}>
            <Dialog.Title className={styles.title}>Keyboard shortcuts</Dialog.Title>
            <Dialog.Close className="icon-button" aria-label="Close keyboard shortcuts"><X size={13} /></Dialog.Close>
          </div>
          <label className={styles.search}>
            <Search size={12} aria-hidden="true" />
            <input ref={searchInput} aria-label="Search keyboard shortcuts" placeholder="Search shortcuts or contexts…" value={search} onChange={(event) => setSearch(event.target.value)} />
          </label>
          <div className={styles.list}>
            {groups.map((group) => (
              <section key={group.title} aria-label={group.title} className={styles.group}>
                <h3>{group.title}</h3>
                <dl>
                  {group.shortcuts.map((shortcut) => (
                    <div key={shortcut.action} className={styles.shortcut}>
                      <dt>{shortcut.action}<span className={styles.context}>{shortcut.context}</span></dt>
                      <dd>
                        {shortcut.keys.map((keys, index) => (
                          <span key={keys.join('+')} className={styles.binding}>
                            {index > 0 && <span className={styles.or}>or</span>}
                            {keys.map((key) => <kbd key={key}>{key}</kbd>)}
                          </span>
                        ))}
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>
            ))}
            {groups.length === 0 && <p role="status" className={styles.empty}>No shortcuts match “{search}”. Try an action or context, like “slice” or “table”.</p>}
          </div>
          <Dialog.Description className={styles.description}>
            {modifier === '⌘' ? '⌘ is Command.' : 'Ctrl is Control.'} Custom views may provide additional shortcuts.
          </Dialog.Description>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
