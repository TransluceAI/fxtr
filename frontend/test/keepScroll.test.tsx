// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { useRef } from 'react'
import { useKeepScroll } from '../src/lib/keepScroll'
import { viewAutoscrolls } from '../src/components/DetailPane'
import type { Resolution } from '../src/panes/resolve'

afterEach(cleanup)

function Panel({ showing, enabled }: { showing: string; enabled: boolean }) {
  const root = useRef<HTMLDivElement>(null)
  useKeepScroll(root, showing, { enabled })
  return <div ref={root}><div className="detail-scroll" data-testid="area" /></div>
}

function scrolledTo(area: HTMLElement, top: number) {
  area.scrollTop = top
  area.dispatchEvent(new Event('scroll'))
}

it('puts a panel\'s scroll back when what it shows changes', () => {
  const view = render(<Panel showing="a" enabled />)
  const area = view.getByTestId('area')
  scrolledTo(area, 700)
  area.scrollTop = 0 // the new contents start at the top
  view.rerender(<Panel showing="b" enabled />)
  expect(area.scrollTop).toBe(700)
})

it('puts nothing back while disabled, so a view\'s own scroll stands', () => {
  const view = render(<Panel showing="a" enabled={false} />)
  const area = view.getByTestId('area')
  scrolledTo(area, 700)
  area.scrollTop = 3000 // the view autoscrolls to what its new params name
  view.rerender(<Panel showing="b" enabled={false} />)
  expect(area.scrollTop).toBe(3000)
})

it('says a view autoscrolls for an entity opened at params, and for nothing else', () => {
  const entity = { kind: 'entity', cid: 'bafy', path: [], view: null }
  const resolution = (target: unknown, viewParams: Record<string, unknown>) => ({ target, viewParams }) as unknown as Resolution
  expect(viewAutoscrolls(resolution(entity, { selector: { message: 9 } }))).toBe(true)
  expect(viewAutoscrolls(resolution(entity, {}))).toBe(false)
  expect(viewAutoscrolls(resolution({ kind: 'node' }, { proposition: 'x' }))).toBe(false)
})
