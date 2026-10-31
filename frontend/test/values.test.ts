import { expect, it } from 'vitest'
import cases from './fixtures/table-values.json'
import { decodeBase64Cbor } from '../src/api/cbor'
import { previewValue, summarize } from '../src/lib/values'

it.each(cases)('matches the shared table-text contract: $name', fixture => {
  const value = decodeBase64Cbor(fixture.cbor)
  expect(summarize(value)).toBe(fixture.summary)
  expect(previewValue(value)).toBe(fixture.preview)
})
