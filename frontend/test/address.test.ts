import { describe, expect, it } from 'vitest'
import {
  AddressError, addressParts, applyRelLocation, formatAddress, formatRel, parseAddress, parseRel, parseRelLocation,
  relLocationFor, sharedParts, type Location,
} from '../src/panes/address'

describe('addresses', () => {
  it.each([
    '(/)',
    'trace(/)',
    '(/generate_prompts)',
    'tree(/generate_prompts.output.attempts[0])',
    '(/judge.inputs.prompt)',
    '(/score_all/judge, target_model=gpt-5)',
    'pander-score.ScoreFit/point(/score_fit, proposition=p042, type=conversational)',
    '(cid:bafyabc.messages[2])',
    'pander-score.ReportPanel(cid:bafyabc)',
    'limerick-contest.poet(poet=nash)',
    'experiment_readme()',
    '(/"$return".output."a.b")',
  ])('round-trips %s', (text) => {
    const { address, params } = parseAddress(text)
    expect(formatAddress(address, params)).toBe(text)
  })

  it('writes params sorted, quoting strings that look like numbers, and reads a bare number as one', () => {
    const { address } = parseAddress('x.Fit(/score_fit)')
    expect(formatAddress(address, { type: 'conversational', prompt: 3, a: '3', b: 'a b' }))
      .toBe('x.Fit(/score_fit, a="3", b="a b", prompt=3, type=conversational)')
    expect(parseAddress('(/a, n=3, s="3")').params).toEqual({ n: 3, s: '3' })
  })

  it('reads the parts of a location', () => {
    expect(parseAddress('tree(/a/b.output.x[2])').address).toEqual({
      view: 'tree', location: { root: 'step', steps: ['a', 'b'], path: ['output', 'x', '2'] },
    })
    expect(parseAddress('(/.output)').address.location).toEqual({ root: 'step', steps: [], path: ['output'] })
  })

  it('rejects malformed addresses', () => {
    expect(() => parseAddress('/prompts')).toThrow(AddressError)
    expect(() => parseAddress('(/a.)')).toThrow(AddressError)
    expect(() => parseAddress('(/a, b)')).toThrow(AddressError)
    expect(() => parseAddress('(/a) x')).toThrow(AddressError)
  })
})

describe('relative addresses', () => {
  const base: Location = { root: 'step', steps: ['score_all', 'judge'], path: ['output'] }
  const at = (text: string) => applyRelLocation(parseRelLocation(text), base)

  it.each([
    ['.call[0]', '/score_all/judge.output.call[0]'],
    ['../respond.output', '/score_all/respond.output'],
    ['/prompts.output', '/prompts.output'],
    ['', '/score_all/judge.output'],
  ])('%s from /score_all/judge.output is %s', (rel, expected) => {
    expect(formatAddress({ view: null, location: at(rel) })).toBe(`(${expected})`)
  })

  it('round-trips a relative address with its own params', () => {
    for (const text of ['x.Fit/point(proposition=p042)', '(.call)', 'tree(../reply.output, prompt=3)', '(/prompts)']) {
      const { rel, params } = parseRel(text)
      expect(formatRel(rel, params)).toBe(text)
    }
  })

  it('puts a location relative to the one before it as near as it can', () => {
    const rel = (location: Location) => relLocationFor(location, base)
    expect(rel(base)).toEqual({ kind: 'same' })
    expect(rel({ ...base, path: ['output', 'call'] })).toEqual({ kind: 'below', path: ['call'] })
    expect(rel({ root: 'step', steps: ['score_all', 'reply'], path: [] })).toEqual({ kind: 'sibling', steps: ['reply'], path: [] })
    expect(rel({ root: 'step', steps: ['prompts'], path: [] }).kind).toBe('absolute')
  })

  it('refuses a field path or sibling with nothing before it', () => {
    expect(() => applyRelLocation(parseRelLocation('.a'), null)).toThrow(AddressError)
    expect(() => applyRelLocation(parseRelLocation('../a'), { root: 'step', steps: [], path: [] })).toThrow(AddressError)
  })
})

describe('the parts a panel head draws', () => {
  const parts = (text: string, isPage = false) => addressParts(parseAddress(text).address, isPage).map((part) => `${part.kind}:${part.name}`)

  it('draws every step, the field path and a nested view\'s parts, leaving out a view of its own', () => {
    expect(parts('tree(/generate_prompts/elicit.output.attempts[0])')).toEqual(
      ['step:generate_prompts', 'step:elicit', 'field:output', 'field:attempts', 'field:0'])
    expect(parts('x.Fit/point(/score_fit)')).toEqual(['step:score_fit', 'view:point'])
  })

  it('draws a root of its own: the run, an entity, a page, the experiment', () => {
    expect(parts('invocation(/)')).toEqual(['run:run'])
    expect(parts('(cid:bafyabc.messages[2])')).toEqual(['entity:bafyabc', 'field:messages', 'field:2'])
    expect(parts('x.dashboard/proposition(proposition=p)', true)).toEqual(['page:x.dashboard', 'view:proposition'])
    expect(parts('experiment_readme()')).toEqual(['experiment_readme:experiment_readme'])
  })

  it('counts the leading parts two addresses share', () => {
    const of = (text: string) => addressParts(parseAddress(text).address, false)
    expect(sharedParts(of('(/judge.output.call)'), of('table(/judge)'))).toBe(1)
    expect(sharedParts(of('(/grade)'), of('(/judge)'))).toBe(0)
  })
})
