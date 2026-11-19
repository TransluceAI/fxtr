/**
 * What a step invocation that has not finished shows of its result: the latest value its body
 * published through `ctx.preview`, dispatched like an entity (the type's `PartialEntityPanel`
 * renderer, else the same data tree a finished entity's raw view shows).
 *
 * Every view that shows an unfinished step invocation (running, failed, or stopped) in place of
 * its output renders this module's components, and nothing else draws a preview. A running invocation's table row
 * shows `StepPreviewLine`, its preview in one line (the `PartialEntityInline` slot). Two views show
 * the whole preview today:
 * `components/PendingInvocation.tsx` (a node's default view sliced at one unfinished invocation)
 * and `views/DefaultInvocationPanel.tsx` (the `invocation` view, which clicking a running row
 * opens). A change to how a running step's result shows goes here, so both views get it; a view
 * added for unfinished steps renders these too and joins `test/unfinished-step-views.test.tsx`,
 * which checks every such view shows the preview.
 */
import { Suspense, useMemo, useRef, type ReactNode } from 'react'
import { atom } from 'jotai'
import { PartialEntityInline, PartialEntityPanel, type JsonValue } from 'fxtr-view'
import { RenderValue, type DispatchFallbacks } from 'fxtr-view-shell-util'
import { CborTreePartialInline, CborTreePartialPanel } from 'fxtr-view-cbor-tree'
import type { GqlPreview } from '../api/graphql'
import { useStepPin } from '../api/live'
import { useStepPreview, type Preview } from '../api/preview'
import { useFollowBottom } from '../lib/followBottom'
import { ErrorBoundary } from './ErrorBoundary'
import styles from './StepPreview.module.css'

/**
 * The latest preview a step published (`ctx.preview`), labelled live while the step runs; `fallback`
 * until it has one. `live` also goes to the renderer, so a partial value from a step that has
 * stopped does not read as in progress. The value is dispatched like an entity: the `PartialEntityPanel` renderer its
 * type registers (behaviors' conversation view for a rollout), else the slot's wildcard, the same
 * data tree a finished entity's raw view shows. Its storage lasts for the mount, so the renderer's
 * state (collapsed subtrees) survives each new preview. Its box opens scrolled to the bottom, where
 * the newest output is, and while the step runs it follows the bottom as the preview grows, unless
 * scrolled up (`lib/followBottom.ts`).
 */
export function StepPreview({
  preview,
  live,
  fallback = null,
}: {
  preview: GqlPreview | null
  live: boolean
  fallback?: ReactNode
}) {
  const shown = useStepPreview(preview)
  const storage = useMemo(() => atom<JsonValue>(null), [])
  if (shown === null) return <>{fallback}</>
  return (
    <section className={styles.preview} aria-label="preview">
      <div className={styles.previewHead}>{live ? 'live preview' : 'last preview'}</div>
      <PreviewBody shown={shown} live={live} storage={storage} />
    </section>
  )
}

/** The preview's scrolling box, mounted once there is a preview, so it opens at the bottom. */
function PreviewBody({ shown, live, storage }: { shown: Preview; live: boolean; storage: ReturnType<typeof atom<JsonValue>> }) {
  const scroller = useRef<HTMLDivElement>(null)
  const content = useRef<HTMLDivElement>(null)
  useFollowBottom(scroller, content, live)
  return (
    <div ref={scroller} className={styles.body}>
      <div ref={content}>
        <RenderValue
          slot={PartialEntityPanel}
          value={shown.value}
          partial={shown.partial}
          live={live}
          storage={storage}
          fallbacks={treeFallbacks(shown, live, storage)}
        />
      </div>
    </div>
  )
}

/** A renderer that fails on a preview (one not written for partial values, say) falls back to the
 *  data tree, with the error above it, so the preview still shows. */
function treeFallbacks(shown: Preview, live: boolean, storage: ReturnType<typeof atom<JsonValue>>): DispatchFallbacks {
  const tree = <CborTreePartialPanel value={shown.value} partial={shown.partial} live={live} storage={storage} />
  return {
    error: (error) => (
      <>
        <div className={styles.renderError}>this view could not show the preview: {String(error)}</div>
        {tree}
      </>
    ),
    noRenderer: () => tree,
  }
}

/** A step's latest preview, read from its pin (current while shown); `fallback` until it has
 *  one, or while the pin loads. */
export function StepPinPreview({
  jobId,
  cacheKey,
  live,
  fallback = null,
}: {
  jobId: string
  cacheKey: string
  live: boolean
  fallback?: ReactNode
}) {
  return (
    <ErrorBoundary fallback={() => <>{fallback}</>}>
      <Suspense fallback={fallback}>
        <PinPreview jobId={jobId} cacheKey={cacheKey} live={live} fallback={fallback} />
      </Suspense>
    </ErrorBoundary>
  )
}

function PinPreview({ jobId, cacheKey, live, fallback }: { jobId: string; cacheKey: string; live: boolean; fallback: ReactNode }) {
  const pin = useStepPin(jobId, cacheKey)
  return <StepPreview preview={pin.preview} live={live} fallback={fallback} />
}

/**
 * A step's latest preview in one line, read from its pin (current while shown): what a running
 * invocation's table row shows in place of its result, through the value's `PartialEntityInline`
 * renderer (a rollout says the turn it is on), else the slot's wildcard (its type, and that it is
 * partial). `fallback` until it has a preview, while the pin loads, or if it fails to.
 */
export function StepPreviewLine({ jobId, cacheKey, fallback = null }: { jobId: string; cacheKey: string; fallback?: ReactNode }) {
  return (
    <ErrorBoundary fallback={() => <>{fallback}</>}>
      <Suspense fallback={fallback}>
        <PinPreviewLine jobId={jobId} cacheKey={cacheKey} fallback={fallback} />
      </Suspense>
    </ErrorBoundary>
  )
}

function PinPreviewLine({ jobId, cacheKey, fallback }: { jobId: string; cacheKey: string; fallback: ReactNode }) {
  const pin = useStepPin(jobId, cacheKey)
  const shown = useStepPreview(pin.preview)
  if (shown === null) return <>{fallback}</>
  const live = pin.state === 'running'
  const line = <CborTreePartialInline value={shown.value} partial={shown.partial} live={live} />
  return (
    <RenderValue
      slot={PartialEntityInline}
      value={shown.value}
      partial={shown.partial}
      live={live}
      fallbacks={{ error: () => line, noRenderer: () => line }}
    />
  )
}
