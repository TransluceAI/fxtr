import { expect, it } from 'vitest'
import { defineViewBundleConfig } from '../../ts/packages/fxtr-view-bundle/src/index'

it('leaves no process.env.NODE_ENV in a bundle, since a browser has no process', () => {
  const config = defineViewBundleConfig({ entry: 'src/bundle.ts' })
  expect(config.define).toEqual({ 'process.env.NODE_ENV': '"production"' })
})
