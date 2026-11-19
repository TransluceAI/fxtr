// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

beforeEach(() => vi.resetModules())
afterEach(() => vi.unstubAllGlobals())

it('loads recorded source, committed files and README from configured endpoints', async () => {
  const { configureViewer, LOCAL_CONFIG } = await import('../src/config')
  const { loadSource, loadFile, loadExperimentReadme } = await import('../src/api/resources')
  configureViewer({
    ...LOCAL_CONFIG,
    sourceBase: 'https://reader.test/source',
    fileUrl: 'https://reader.test/file',
    experimentReadmeUrl: 'https://reader.test/experiment_readme',
    headers: { authorization: 'Bearer reader-token' },
  })
  const source = { file: 'src/example.py', commit: 'abc', startLine: 3, endLine: 9 }
  const file = { path: source.file, commit: 'abc', text: 'def example(): pass' }
  const readme = { text: '# Example' }
  const fetch = vi.fn()
    .mockResolvedValueOnce(Response.json(source))
    .mockResolvedValueOnce(Response.json(file))
    .mockResolvedValueOnce(Response.json(readme))
  vi.stubGlobal('fetch', fetch)
  expect(await loadSource('my.example', 'version')).toEqual(source)
  expect(await loadFile(source.file, 'abc')).toEqual(file)
  expect(await loadExperimentReadme()).toEqual(readme)
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    'https://reader.test/source/my.example?codeVersion=version',
    'https://reader.test/file?path=src%2Fexample.py&commit=abc',
    'https://reader.test/experiment_readme',
  ])
  for (const [, options] of fetch.mock.calls) {
    expect(options.headers).toEqual({ authorization: 'Bearer reader-token' })
  }
})

it('treats missing source and README as unavailable but reports file and server failures', async () => {
  const { loadSource, loadFile, loadExperimentReadme } = await import('../src/api/resources')
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response('', { status: 404 }))
    .mockResolvedValueOnce(new Response('', { status: 404 }))
    .mockResolvedValueOnce(new Response('file missing', { status: 404 }))
    .mockResolvedValueOnce(new Response('', { status: 503 }))
  vi.stubGlobal('fetch', fetch)
  expect(await loadSource('missing', null)).toBeNull()
  expect(await loadExperimentReadme()).toBeNull()
  await expect(loadFile('missing.py', null)).rejects.toThrow('file missing')
  await expect(loadSource('unavailable', null)).rejects.toThrow('503')
})

it('does not request absent file endpoints or renderer assets', async () => {
  const { configureViewer, LOCAL_CONFIG } = await import('../src/config')
  const { loadSource, loadFile, loadExperimentReadme } = await import('../src/api/resources')
  const { loadViews } = await import('../src/views/registry')
  configureViewer({ ...LOCAL_CONFIG, sourceBase: null, fileUrl: null, experimentReadmeUrl: null, viewsBase: null })
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  expect(await loadSource('example', null)).toBeNull()
  expect(await loadExperimentReadme()).toBeNull()
  await expect(loadFile('example.py', null)).rejects.toThrow('source files are unavailable')
  expect((await loadViews()).views.problem).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

it('uses startup configuration for GraphQL even when the client module was imported first', async () => {
  const { configureViewer, LOCAL_CONFIG } = await import('../src/config')
  const { gql } = await import('../src/api/graphql')
  configureViewer({ ...LOCAL_CONFIG, graphqlUrl: 'https://reader.test/graphql/', headers: { authorization: 'Bearer reader-token' } })
  const fetch = vi.fn().mockResolvedValue(Response.json({ data: { value: 42 } }))
  vi.stubGlobal('fetch', fetch)
  expect(await gql('query { value }')).toEqual({ value: 42 })
  expect(fetch).toHaveBeenCalledWith('https://reader.test/graphql/', expect.objectContaining({
    headers: { 'content-type': 'application/json', authorization: 'Bearer reader-token' },
  }))
})

it.each([
  [{ error: { code: 'viewer_expired', message: 'Reload this job to renew access.' } }, 401, 'Reload this job to renew access.'],
  [{ errors: [{ message: 'Query is too complex.' }] }, 400, 'Query is too complex.'],
  [{}, 503, 'GraphQL request failed: 503'],
  [null, 502, 'GraphQL request failed: 502'],
])('reports GraphQL and hub failures to the viewer (%j)', async (body, status, message) => {
  const { gql } = await import('../src/api/graphql')
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(body, { status })))
  await expect(gql('query { value }')).rejects.toThrow(message)
})
