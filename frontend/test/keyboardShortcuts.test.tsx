// @vitest-environment jsdom
import { useRef, useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as Dialog from '@radix-ui/react-dialog'
import { dispatchRegistry } from 'fxtr-view-shell-util'
import { App } from '../src/App'
import { KeyboardShortcuts } from '../src/components/KeyboardShortcuts'
import { KeyValueList } from '../src/components/KeyValueList'
import { useDismiss } from '../src/lib/dismiss'

beforeEach(() => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue('MacIntel')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function openHelp(target: Element | Window = window, modifiers: KeyboardEventInit = { metaKey: true }) {
  return fireEvent.keyDown(target, { key: ',', code: 'Comma', ...modifiers })
}

it.each(['input', 'textarea', 'editable', 'button'] as const)('opens from a focused %s before its own key handler and restores focus', async (kind) => {
  const consume = vi.fn((event: React.KeyboardEvent) => event.stopPropagation())
  render(<><KeyboardShortcuts />{
    kind === 'input' ? <input aria-label="Editor" onKeyDown={consume} /> :
    kind === 'textarea' ? <textarea aria-label="Editor" onKeyDown={consume} /> :
    kind === 'editable' ? <div aria-label="Editor" contentEditable onKeyDown={consume} /> :
    <button aria-label="Editor" onKeyDown={consume}>Editor</button>
  }</>)
  const editor = screen.getByLabelText('Editor')
  editor.focus()
  expect(openHelp(editor)).toBe(false) // Suppress the browser's default action.
  expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeTruthy()
  expect(consume).not.toHaveBeenCalled()
  const search = screen.getByRole('textbox', { name: 'Search keyboard shortcuts' })
  expect(document.activeElement).toBe(search)
  fireEvent.keyDown(search, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(() => expect(document.activeElement).toBe(editor))
})

it.each([
  { platform: 'MacIntel', modifier: { metaKey: true }, wrong: { ctrlKey: true }, label: '⌘', otherLabel: 'Ctrl' },
  { platform: 'Win32', modifier: { ctrlKey: true }, wrong: { metaKey: true }, label: 'Ctrl', otherLabel: '⌘' },
  { platform: 'Linux x86_64', modifier: { ctrlKey: true }, wrong: { metaKey: true }, label: 'Ctrl', otherLabel: '⌘' },
])('uses only the platform modifier for help and its labels on $platform', ({ platform, modifier, wrong, label, otherLabel }) => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform)
  render(<KeyboardShortcuts />)
  for (const modifiers of [{}, wrong, { metaKey: true, ctrlKey: true }, { altKey: true },
    { ...modifier, shiftKey: true }, { ...modifier, altKey: true }, { ...modifier, isComposing: true }]) {
    expect(openHelp(window, modifiers)).toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
  }
  expect(openHelp(window, modifier)).toBe(false)
  const keys = [...screen.getByRole('dialog').querySelectorAll('kbd')].map((key) => key.textContent)
  expect(keys).toContain(label)
  expect(keys).not.toContain(otherLabel)
  const search = screen.getByRole('textbox', { name: 'Search keyboard shortcuts' })
  openHelp(search, { ...modifier, repeat: true })
  openHelp(search, modifier)
  expect(screen.getAllByRole('dialog')).toHaveLength(1)
})

it('searches actions and contexts and explains where bindings apply', () => {
  render(<KeyboardShortcuts />)
  openHelp()
  const search = screen.getByRole('textbox', { name: 'Search keyboard shortcuts' })
  fireEvent.change(search, { target: { value: 'typing' } })
  expect(screen.getByText('Choose the next / previous dimension value')).toBeTruthy()
  expect(screen.getByText(/Hover a value list/).textContent).toContain('Only while not typing')
  expect(screen.queryByText('Go to a row')).toBeNull()
  fireEvent.change(search, { target: { value: 'not-a-shortcut' } })
  expect(screen.getByRole('status').textContent).toContain('No shortcuts match')
  fireEvent.click(screen.getByRole('button', { name: 'Close keyboard shortcuts' }))
  openHelp()
  expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Search keyboard shortcuts' }).value).toBe('')
  expect(screen.getByText('Go to a row')).toBeTruthy()
})

it('keeps help keystrokes and Escape out of a hovered key list and its open panel', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const choose = vi.fn()
  const dismiss = vi.fn()
  function Panel() {
    const ref = useRef<HTMLDivElement>(null)
    useDismiss(ref, true, dismiss)
    return <div ref={ref}>
      <KeyValueList dim="seed" scope="seed" against="all" enabled picked={undefined}
        onChoose={choose} onClear={() => {}} load={async () => ({
          items: [{ value: 'first', compatible: true, glyph: null }, { value: 'second', compatible: true, glyph: null }], hasMore: false,
        })} />
    </div>
  }
  render(<><KeyboardShortcuts /><Panel /></>)
  await screen.findByRole('button', { name: 'first' })
  fireEvent.mouseEnter(screen.getByRole('region', { name: 'seed keys' }))
  fireEvent.keyDown(document.body, { key: 'j' })
  expect(choose).toHaveBeenCalledTimes(1)
  openHelp()
  const close = screen.getByRole('button', { name: 'Close keyboard shortcuts' })
  close.focus()
  fireEvent.keyDown(close, { key: 'j' })
  expect(choose).toHaveBeenCalledTimes(1)
  fireEvent.keyDown(close, { key: 'Escape' })
  expect(dismiss).not.toHaveBeenCalled()
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('opens above another dialog and closes only help, restoring its input focus', async () => {
  function Example() {
    const [open, setOpen] = useState(true)
    return <><KeyboardShortcuts /><Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal><Dialog.Content aria-describedby={undefined}>
        <Dialog.Title>Row detail</Dialog.Title>
        <input aria-label="Row input" />
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root></>
  }
  render(<Example />)
  const input = screen.getByRole('textbox', { name: 'Row input' })
  input.focus()
  openHelp(input)
  expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeTruthy()
  fireEvent.keyDown(screen.getByRole('textbox', { name: 'Search keyboard shortcuts' }), { key: 'Escape' })
  expect(screen.getByRole('dialog', { name: 'Row detail' })).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(input))
})

it('is available in the app while jobs are loading, empty, or fail to load', async () => {
  let finish!: (response: unknown) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise((resolve) => { finish = resolve })))
  render(<App views={{ registry: dispatchRegistry([]), problem: null }} />)
  openHelp()
  expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Close keyboard shortcuts' }))
  await act(async () => finish({ ok: true, json: async () => ({ data: { jobSummaries: { items: [], next: null } } }) }))
  expect(screen.getByText('no jobs recorded')).toBeTruthy()
  openHelp()
  expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeTruthy()
  cleanup()
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline') }))
  render(<App views={{ registry: dispatchRegistry([]), problem: null }} />)
  await screen.findByText('offline')
  openHelp()
  expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeTruthy()
})
