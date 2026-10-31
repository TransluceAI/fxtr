// @vitest-environment jsdom
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {ViewsButton} from '../src/components/PanelButton'
import {OpenEntityContext} from '../src/lib/openEntity'
import {viewTarget} from '../src/lib/linkBase'
import {buildLevel} from '../src/model/level'
import {followed, inProgress, overviewOf, recordingOpener} from './fixtures'

const roll = buildLevel('job', overviewOf(inProgress())).steps.find((step) => step.name === 'roll')!
const view = (id: string) => ({id, target: viewTarget([], roll, id)})

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

async function renderButton(ids: string[], shown: string[]) {
  const recording = recordingOpener()
  await act(async () => root.render(
    <OpenEntityContext.Provider value={recording.opener}>
      <ViewsButton views={ids.map(view)} shown={shown} className="button" selectedClassName="lit" />
    </OpenEntityContext.Provider>,
  ))
  return recording
}

it('names and opens a step\'s one view of the project\'s own, lit only while a panel shows that view', async () => {
  const {follows} = await renderButton(['test.Rolls'], [])
  const button = host.querySelector('button')!
  expect(button.textContent).toBe('Rolls')
  expect(button.classList.contains('lit')).toBe(false)
  await act(async () => button.click())
  expect(followed(follows[0])).toBe('test.Rolls(/roll)')
  await renderButton(['test.Rolls'], ['test.Rolls'])
  expect(host.querySelector('button')!.classList.contains('lit')).toBe(true)
})

it('opens the first of several views from its own segment, and lists them all from a counted one', async () => {
  const {follows} = await renderButton(['test.Rolls', 'test.Grid'], ['test.Grid'])
  const [main, more] = Array.from(host.querySelectorAll('button')) as HTMLButtonElement[]
  expect(main!.textContent).toBe('Rolls')
  expect(more!.textContent).toBe('2')
  // A panel shows Grid, which the menu's segment stands for, not the current view's.
  expect(main!.classList.contains('lit')).toBe(false)
  expect(more!.classList.contains('lit')).toBe(true)
  await act(async () => main!.click())
  expect(followed(follows.at(-1))).toBe('test.Rolls(/roll)')
  await act(async () => more!.click())
  const items = Array.from(document.querySelectorAll('[role="dialog"] button')) as HTMLButtonElement[]
  expect(items.map((item) => item.textContent)).toEqual(['Rolls', 'Grid'])
  await act(async () => items[1]!.click())
  expect(followed(follows.at(-1))).toBe('test.Grid(/roll)')
  // The view chosen from the menu is the current view's segment now.
  expect(host.querySelector('button')!.textContent).toBe('Grid')
})
