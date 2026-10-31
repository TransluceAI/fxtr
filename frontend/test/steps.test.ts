import { describe, expect, it } from 'vitest'
import { levelTrail, mappedKeyOf } from '../src/lib/steps'

describe('levels from pathnames', () => {
  it('lists the levels from the root down to a pathname, a mapped key\'s slashes kept in it', () => {
    expect(levelTrail('root', 'root')).toEqual([])
    expect(levelTrail('root', 'root/round[topic:"a/b"]/entry[poet:"nash"]')).toEqual([
      { pathname: 'root/round[topic:"a/b"]', label: 'round[topic:"a/b"]' },
      { pathname: 'root/round[topic:"a/b"]/entry[poet:"nash"]', label: 'entry[poet:"nash"]' },
    ])
    expect(() => levelTrail('root', 'other/x')).toThrow()
  })

  it('reads a segment\'s mapped key, values JSON-encoded', () => {
    expect(mappedKeyOf('round')).toEqual({})
    expect(mappedKeyOf('sample[prompt:3,split:"a, b"]')).toEqual({ prompt: 3, split: 'a, b' })
  })
})
