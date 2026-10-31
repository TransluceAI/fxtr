// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { useQuery } from '../src/api/useQuery'

afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals() })

it('starts a remounted query from the value its key last read, and revalidates it', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  let answer = 'first'
  const load = vi.fn(async () => answer)
  const seen: (string | undefined)[] = []
  function Reader() {
    const query = useQuery('reader', load)
    seen.push(query.value)
    return <span>{query.loading ? 'loading' : query.value}</span>
  }
  const host = document.body.appendChild(document.createElement('div'))
  let root = createRoot(host)
  await act(async () => root.render(<Reader />))
  expect(host.textContent).toBe('first')
  await act(async () => root.unmount())

  answer = 'second'
  seen.length = 0
  root = createRoot(host)
  await act(async () => root.render(<Reader />))
  // The first render already has the kept value; the read it makes replaces it.
  expect(seen[0]).toBe('first')
  expect(host.textContent).toBe('second')
  expect(load).toHaveBeenCalledTimes(2)
  await act(async () => root.unmount())
})

it('starts a disabled query empty, whatever its key read before', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.body.appendChild(document.createElement('div'))
  function Reader({ enabled }: { enabled: boolean }) {
    return <span>{useQuery('kept', async () => 'value', { enabled }).value ?? 'none'}</span>
  }
  let root = createRoot(host)
  await act(async () => root.render(<Reader enabled />))
  await act(async () => root.unmount())
  root = createRoot(host)
  await act(async () => root.render(<Reader enabled={false} />))
  expect(host.textContent).toBe('none')
  await act(async () => root.unmount())
})
