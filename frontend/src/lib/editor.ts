import { useSyncExternalStore } from 'react'

/**
 * The editor code opens in: the one each person last chose in the code pane's "Open" menu, kept
 * in localStorage so it holds across visits. Every editor here registers the same URL shape,
 * `<scheme>://file/<absolute path>:<line>` (see `editorHref` in `code.ts`).
 */
export interface Editor {
  id: string
  label: string
  scheme: string
}

export const EDITORS: readonly Editor[] = [
  { id: 'cursor', label: 'Cursor', scheme: 'cursor' },
  { id: 'vscode', label: 'VS Code', scheme: 'vscode' },
  { id: 'vscode-insiders', label: 'VS Code Insiders', scheme: 'vscode-insiders' },
  { id: 'windsurf', label: 'Windsurf', scheme: 'windsurf' },
  { id: 'positron', label: 'Positron', scheme: 'positron' },
]

const DEFAULT_EDITOR = EDITORS[0]!
const STORAGE_KEY = 'fxtr-editor'
const listeners = new Set<() => void>()

function readEditor(): Editor {
  try {
    const kept = localStorage.getItem(STORAGE_KEY)
    return EDITORS.find((editor) => editor.id === kept) ?? DEFAULT_EDITOR
  } catch {
    return DEFAULT_EDITOR
  }
}

let current = readEditor()

/** Pick the editor, for every code link on the page and for later visits. */
export function setEditor(id: string): void {
  const editor = EDITORS.find((candidate) => candidate.id === id)
  if (!editor) throw new Error(`no editor ${id}`)
  current = editor
  try {
    localStorage.setItem(STORAGE_KEY, id)
  } catch {
    // Storage can be unavailable (a private window, blocked site data); the pick then lasts for
    // this page only.
  }
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

/** The editor picked, re-rendering when it changes. */
export function useEditor(): Editor {
  return useSyncExternalStore(subscribe, () => current)
}
