import { describe, expect, it } from 'vitest'
import { formatType, isReference, returnType, schemaAt, typeLabel } from '../src/model/schema'
import type { ArrayTypeJson, SchemaJson } from '../src/types'

describe('returnType', () => {
  const type: ArrayTypeJson = {
    dims: [['model', 'str'], ['seed', 'int'], ['sample', 'int']],
    schema: { struct: { answer: 'str', correct: 'bool' } },
  }

  it('drops the mapped and replica dims, keeping the declared ones', () => {
    expect(formatType(returnType(type, ['model', 'seed']))).toBe('[sample: int]: struct{answer: str, correct: bool}')
    expect(formatType(returnType(type, ['model', 'seed', 'sample']))).toBe('struct{answer: str, correct: bool}')
  })

  it('rejects run dims the output type lacks', () => {
    expect(() => returnType(type, ['prompt'])).toThrow(/prompt/)
  })
})

describe('typeLabel', () => {
  const value = (schema: SchemaJson): ArrayTypeJson => ({ dims: [], schema })
  const array = (schema: SchemaJson): ArrayTypeJson => ({ dims: [['sample', 'int']], schema })

  it('names structs, entities, and arrays of them without their details', () => {
    expect(typeLabel(value({ struct: { answer: 'str' } }))).toBe('struct')
    expect(typeLabel(value({ id: 'org.example.Report.v2' }))).toBe('Report.v2')
    expect(typeLabel(array({ id: 'org.example.Report' }))).toBe('Array[Report]')
    expect(typeLabel(array({ struct: { answer: 'str' } }))).toBe('Array[struct]')
  })

  it('keeps other schemas short', () => {
    expect(typeLabel(value('str'))).toBe('str')
    expect(typeLabel(value('id'))).toBe('entity')
    expect(typeLabel(value({ list: { nullable: { id: 'org.example.Report' } } }))).toBe('list[Report?]')
  })
})

describe('schemaAt', () => {
  const schema: SchemaJson = { struct: { call: { id: 'org.example.Call' }, scores: { list: { nullable: 'float' } }, ref: 'id' } }
  it('walks struct fields and list items to the value at a path', () => {
    expect(schemaAt(schema, [])).toEqual(schema)
    expect(schemaAt(schema, ['call'])).toEqual({ id: 'org.example.Call' })
    expect(schemaAt(schema, ['scores', '2'])).toEqual({ nullable: 'float' })
  })
  it('says a path through a reference goes where the schema does not, and a missing one is not there', () => {
    expect(schemaAt(schema, ['call', 'messages'])).toBeNull()
    expect(schemaAt(schema, ['nope'])).toBeUndefined()
    expect(schemaAt(schema, ['scores', 'x'])).toBeUndefined()
  })
  it('tells references, typed or not, nullable or not', () => {
    expect(isReference({ id: 'org.example.Call' })).toBe(true)
    expect(isReference({ nullable: 'id' })).toBe(true)
    expect(isReference('str')).toBe(false)
  })
})
