/**
 * Wires fxtr-view into this app: the `ViewShell` the renderers see, and beside it the registry
 * (./registry.ts's `loadViews`, handed down from main.tsx) that this app's dispatch
 * (fxtr-view-shell-util's `RenderEntity` and friends) reads.
 * Entity loads go through the backend's bytes endpoint, cached with one load per key and created
 * once so the cache lives for the page, and invocation records through `invocation_source.ts`,
 * which reads the live store so they stay current while a job runs. Links are `EntityLinkImpl`,
 * which opens through the nearest `OpenEntityContext`, so a link inside a panel opens from that
 * panel and a link in the card column from none; `useEntityLinks`, for the marks a renderer draws
 * itself, opens each entity alone through the same context and says whether it is open, as a chip
 * does.
 */

import { useContext, useMemo, type ReactNode } from 'react'
import { ViewShellProvider, type EntityID, type EntityLinks, type InvocationRef, type ViewKeys, type ViewShell } from 'fxtr-view'
import { RegistryProvider, createCachedEntityHook, type DispatchRegistry } from 'fxtr-view-shell-util'
import { loadEntity } from './entity_loader'
import { useInvocationRecord } from './invocation_source'
import { EntityLinkImpl } from './EntityLinkImpl'
import { useLink } from './viewLink'
import { useLoading } from '../lib/loading'
import { LevelStateContext } from '../lib/levelState'
import { linkTarget } from '../lib/linkBase'
import { useEntityOpener } from '../lib/openEntity'

const useCachedEntity = createCachedEntityHook(loadEntity)

function useEntity(id: EntityID) {
  const result = useCachedEntity(id)
  useLoading(result.status === 'loading')
  return result
}

function useInvocation(ref: InvocationRef) {
  const result = useInvocationRecord(ref)
  useLoading(result.status === 'loading')
  return result
}

/** The keys known where a renderer is shown: the slice and picks of the view around it (the
 *  trace's, or a panel's), from the nearest `LevelStateContext`. */
function useKeys(): ViewKeys {
  const level = useContext(LevelStateContext)
  return level ? { slice: level.slice, selected: level.selected } : NO_KEYS
}

const NO_KEYS: ViewKeys = { slice: {}, selected: {} }

function useEntityLinks(): EntityLinks {
  const { follow, linkState } = useEntityOpener()
  return useMemo(() => ({
    open: (id: EntityID) => follow(linkTarget(id.cid, null)),
    isOpen: (id: EntityID) => linkState(linkTarget(id.cid, null)).open,
  }), [follow, linkState])
}

const SHELL: ViewShell = { useEntity, useInvocation, useKeys, useLink, EntityLinkImpl, useEntityLinks }

export function FrontendViewShellProvider({ registry, children }: { registry: DispatchRegistry; children: ReactNode }) {
  return (
    <RegistryProvider registry={registry}>
      <ViewShellProvider shell={SHELL}>{children}</ViewShellProvider>
    </RegistryProvider>
  )
}
