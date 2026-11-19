// @vitest-environment jsdom
import { Suspense } from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ARRAY_TYPE, EntityID, EntityInlineLink, renderer, ViewShellProvider, type AnyRenderer, type ViewShell } from 'fxtr-view'
import { dispatchRegistry, RegistryProvider } from 'fxtr-view-shell-util'
import { EntityLabel } from '../src/views/EntityLabel'

const typeTag = vi.hoisted(() => ({ current: 'test.Verdict' }))
vi.mock('../src/api/browse', () => ({
  useEntityMetadata: () => ({ value: { typeTag: typeTag.current } }),
}))

afterEach(cleanup)

const SHELL = {
  useEntity: () => ({ status: 'loaded', value: { $type: typeTag.current } }),
} as unknown as ViewShell

function show(type: string, renderers: readonly AnyRenderer[]) {
  typeTag.current = type
  render(
    <RegistryProvider registry={dispatchRegistry(renderers)}>
      <ViewShellProvider shell={SHELL}>
        <Suspense fallback="loading">
          <EntityLabel id={new EntityID('bafyexample')} />
        </Suspense>
      </ViewShellProvider>
    </RegistryProvider>,
  )
}

const Own = () => <span>drawn by its renderer</span>

it("draws an entity's label with its type's inline link renderer", () => {
  show('test.Verdict', [renderer(EntityInlineLink, 'test.Verdict', 'test.Inline', Own)])
  expect(screen.getByText('drawn by its renderer')).toBeTruthy()
})

it('labels an entity whose type has no inline link renderer with its type', () => {
  show('test.Other', [renderer(EntityInlineLink, 'test.Verdict', 'test.Inline', Own)])
  expect(screen.getByText(/Other · /)).toBeTruthy()
})

it('labels an array with its type, without loading its rows', () => {
  show(ARRAY_TYPE, [renderer(EntityInlineLink, ARRAY_TYPE, 'test.ArrayInline', Own)])
  expect(screen.queryByText('drawn by its renderer')).toBeNull()
})
