import { describe, expect, it } from 'vitest'
import { sequence, type Columns } from '../src/panes/columns'
import { columnsFromUrl, formatUrlState, parseUrlState, readSnapshot, snapshotState, urlPanelsOf, type UrlState } from '../src/lib/urlState'

const state: UrlState = {
  job: 'job-a',
  workflow: { address: { view: 'flowchart', location: { root: 'step', steps: ['outer'], path: [] } }, keys: { elicitor: 'sonnet' } },
  slice: { model: 'claude opus', prompt: 3 },
  card: 'judge',
  panels: [
    { stored: { rel: { view: 'table', location: { kind: 'absolute', location: { root: 'step', steps: ['judge'], path: [] } } }, own: { prompt: 3 } }, pinned: false, collapsed: false },
    { stored: { rel: { view: null, location: { kind: 'below', path: ['output', 'transcript'] } }, own: { selector: { message: 2 } } }, pinned: false, collapsed: true },
    { stored: { rel: { view: 'tree', location: { kind: 'absolute', location: { root: 'entity', cid: 'bafy1', path: ['messages', '2'] } } }, own: {} }, pinned: true, collapsed: false },
  ],
  layout: [{ panels: [0, 1], width: null }, { panels: [2], width: 480 }],
}

describe('the URL state', () => {
  it('reads back what it writes', () => {
    const hash = formatUrlState(state)
    expect(parseUrlState(hash)).toEqual({ state, problems: [] })
  })

  it('stays readable: only what a hash would mangle is encoded, and no spaces after commas', () => {
    expect(formatUrlState(state)).toBe(
      '#job=job-a&w=flowchart(/outer,elicitor=sonnet)&slice=(model=%22claude%20opus%22,prompt=3)&card=judge'
      + '&p=table(/judge,prompt=3)&p=(.output.transcript,selector={%22message%22:2})&pin=tree(cid:bafy1.messages[2])'
      + '&layout=0,1|2@480&collapsed=1',
    )
  })

  it('reads a link an agent writes by hand, spaced or encoded throughout', () => {
    const spaced = parseUrlState('#job=job-a&p=table(/judge, prompt=3)&p=.output')
    expect(parseUrlState('#w=/outer').state.workflow?.address.location).toEqual({ root: 'step', steps: ['outer'], path: [] })
    expect(spaced.problems).toEqual([])
    expect(spaced.state.panels.map((panel) => panel.stored)).toEqual([
      { rel: { view: 'table', location: { kind: 'absolute', location: { root: 'step', steps: ['judge'], path: [] } } }, own: { prompt: 3 } },
      { rel: { view: null, location: { kind: 'below', path: ['output'] } }, own: {} },
    ])
    expect(spaced.state.layout).toBeNull()
    const encoded = parseUrlState(`#job=job-a&p=${encodeURIComponent('table(/judge, prompt=3)')}`)
    expect(encoded.state.panels[0]!.stored).toEqual(spaced.state.panels[0]!.stored)
  })

  it('applies to the newest job without a job, and is empty for an empty hash', () => {
    expect(parseUrlState('#p=(/judge)').state.job).toBeNull()
    expect(parseUrlState('')).toEqual({ state: { job: null, workflow: null, slice: {}, card: null, panels: [], layout: null }, problems: [] })
    expect(formatUrlState({ job: null, workflow: null, slice: {}, card: null, panels: [], layout: null })).toBe('')
  })

  it('leaves out a part that does not read, and the browsing panels that would follow a panel left out', () => {
    const { state: read, problems } = parseUrlState('#job=a&w=(/x.output)&p=(/one)&p=table(/two&p=.output&pin=(/three)&p=.inputs&what=1&layout=0,0')
    expect(read.job).toBe('a')
    expect(read.workflow).toBeNull()
    expect(read.panels.map((panel) => formatUrlState({ ...read, job: null, panels: [panel], layout: null }))).toEqual([
      '#p=(/one)', '#pin=(/three)', '#p=(.inputs)',
    ])
    expect(read.layout).toBeNull()
    expect(problems).toHaveLength(5)
  })
})

describe('the panels in the URL', () => {
  it('open in the layout the URL gives, each following the one before it', () => {
    const columns = columnsFromUrl(state.panels, state.layout, 1000)
    expect(columns.map((column) => column.panels.length)).toEqual([2, 1])
    expect(columns[1]!.width).toBe(480)
    const ordered = sequence(columns)
    expect(ordered.map((panel) => panel.pinned)).toEqual([false, false, true])
    expect(ordered[1]!.collapsed).toBe(true)
    expect(urlPanelsOf(columns)).toEqual({ panels: state.panels, layout: state.layout })
  })

  it('without a layout, open where following links in sequence would put them', () => {
    const columns: Columns = columnsFromUrl(state.panels, null, 900)
    // 360 for a step and 480 for a relative location, with their gaps: two fit in a column 900px
    // tall, and the third goes right.
    expect(columns.map((column) => column.panels.length)).toEqual([2, 1])
  })

  it('keep the layout they are in through the URL: one column stays one, fractional widths are whole', () => {
    const stacked = columnsFromUrl(state.panels, [{ panels: [0, 1, 2], width: null }], 700)
    const roundTrip = (columns: Columns) => {
      const { panels, layout } = urlPanelsOf(columns)
      const read = parseUrlState(formatUrlState({ job: 'a', workflow: null, slice: {}, card: null, panels, layout }))
      expect(read.problems).toEqual([])
      return columnsFromUrl(read.state.panels, read.state.layout, 700)
    }
    expect(roundTrip(stacked).map((column) => column.panels.length)).toEqual([3])
    const dragged = columnsFromUrl(state.panels, state.layout, 700).map((column) => ({ ...column, width: 600.5 }))
    expect(roundTrip(dragged).map((column) => column.width)).toEqual([601, 601])
  })
})

describe('the history entry snapshot', () => {
  it('is read back for its own job only', () => {
    const entry = snapshotState('a', { columns: [], slice: {}, trail: [], depth: 0, pageView: null, flowchartMode: false, card: null })
    expect(readSnapshot(entry, 'a')?.job).toBe('a')
    expect(readSnapshot(entry, 'b')).toBeNull()
    expect(readSnapshot(null, 'a')).toBeNull()
    expect(readSnapshot({ fxtrViewer: { version: 2, job: 'a' } }, 'a')).toBeNull()
  })
})
