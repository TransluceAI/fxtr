// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach, expect, it, vi} from 'vitest'
import {ViewSelect, type ViewOption} from '../src/components/ViewSelect'

const options: ViewOption[] = [
  {key: 'output', label: 'output', fromProject: false},
  {key: 'output:entity_data', label: 'output entity_data', fromProject: false},
  {key: 'invocation:summary', label: 'invocation summary', fromProject: true},
  {key: 'output:card', label: 'output card', fromProject: true},
]

afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals(); vi.restoreAllMocks() })

async function mount(value: string, onChange = vi.fn()) {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.body.appendChild(document.createElement('div'))
  const root = createRoot(host)
  await act(async () => root.render(<ViewSelect options={options} value={value} onChange={onChange} />))
  return {host, root, onChange, trigger: host.querySelector('button')!}
}

it('counts the project views beside "views" over the view shown', async () => {
  const {trigger, root} = await mount('output')
  expect(trigger.textContent).toBe('views2output')
  await act(async () => root.unmount())
})

it('shows no count without project views', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.body.appendChild(document.createElement('div'))
  const root = createRoot(host)
  await act(async () => root.render(<ViewSelect options={options.slice(0, 2)} value="output" onChange={() => {}} />))
  expect(host.querySelector('button')!.textContent).toBe('viewsoutput')
  await act(async () => root.unmount())
})

it('refuses a value none of its options has', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  await expect(mount('gone')).rejects.toThrow('no view gone')
})
