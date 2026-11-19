import { expect, it } from 'vitest'
import { keepCommits, layoutGraph, type GraphCommit } from '../src/lib/commitGraph'
import { formatRelative } from '../src/lib/time'

const commit = (hash: string, parents: string[], branch: string | null = 'main'): GraphCommit => ({ hash, parents, branch })

it('draws one branch as a straight line on the first lane', () => {
  const layout = layoutGraph([commit('c', ['b']), commit('b', ['a']), commit('a', [])])
  expect(layout.lanes).toBe(1)
  expect(layout.rows.map((row) => row.lane)).toEqual([0, 0, 0])
  expect(layout.rows[1]).toEqual({
    lane: 0,
    into: [{ from: 0, to: 0, branch: 'main' }],
    out: [{ from: 0, to: 0, branch: 'main' }],
    through: [],
  })
  expect(layout.rows[2].out).toEqual([])
})

it('gives a side branch its own lane until it joins the commit it forked from', () => {
  // main: a <- b <- d; feature: b <- c. Newest first: d, c, b, a.
  const layout = layoutGraph([commit('d', ['b']), commit('c', ['b'], 'feature'), commit('b', ['a']), commit('a', [])])
  expect(layout.lanes).toBe(2)
  expect(layout.rows.map((row) => row.lane)).toEqual([0, 1, 0, 0])
  // The feature commit sits beside main's line, which passes it on the way down to b.
  expect(layout.rows[1].through).toEqual([{ from: 0, to: 0, branch: 'main' }])
  expect(layout.rows[1].out).toEqual([{ from: 1, to: 0, branch: 'feature' }])
  // Both children's lines arrive at b on main's lane.
  expect(layout.rows[2].into).toEqual([{ from: 0, to: 0, branch: 'main' }])
})

it('starts commits that share no shown history on the same free lane', () => {
  const layout = layoutGraph([commit('x', []), commit('y', [], 'feature')])
  expect(layout.rows.map((row) => [row.lane, row.into.length, row.out.length])).toEqual([[0, 0, 0], [0, 0, 0]])
})

it('hands a dropped commit\'s parents to the commits that named it', () => {
  const commits = [commit('d', ['c']), commit('c', ['b'], 'feature'), commit('b', ['a']), commit('a', [])]
  expect(keepCommits(commits, (each) => each.branch === 'main').map((each) => [each.hash, each.parents])).toEqual([
    ['d', ['b']],
    ['b', ['a']],
    ['a', []],
  ])
  // A dropped merge does not leave a second line past the commit on its side branch.
  const merged = [commit('top', ['merge']), commit('merge', ['base', 'side']), commit('side', ['base']), commit('base', [])]
  expect(keepCommits(merged, (each) => each.hash !== 'merge')[0].parents).toEqual(['side'])
})

it('says when something happened the way a person would', () => {
  const now = new Date(2026, 10, 13, 15, 0, 0).getTime()
  const at = (...when: [number, number, number, number?, number?]) => new Date(...when).toISOString()
  expect(formatRelative(at(2026, 10, 13, 14, 59), now)).toBe('1 min ago')
  expect(formatRelative(new Date(now - 20_000).toISOString(), now)).toBe('just now')
  expect(formatRelative(at(2026, 10, 13, 9, 30), now)).toBe('5 hr ago')
  expect(formatRelative(at(2026, 10, 12, 10, 0), now)).toBe('yesterday')
  expect(formatRelative(at(2026, 9, 14, 10, 0), now)).toBe('Oct 14')
  expect(formatRelative(at(2025, 9, 14, 10, 0), now)).toBe('Oct 14, 2025')
  expect(formatRelative(null, now)).toBe('—')
})
