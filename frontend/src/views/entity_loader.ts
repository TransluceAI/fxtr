/**
 * The one loader of entity bytes this app has: `POST /api/entities`, batched and decoded as DRISL
 * by `fxtr-view-shell-util`, so a view loading thousands of entities sends a few dozen requests,
 * a handful at a time. Shared by the shell's `useEntity` and by the invocation source, which reads
 * a step's request entity through it, so both go through the same code path, the same batches,
 * and the same URL configuration.
 *
 * The loader is made on the first load rather than when this module is imported, so a hosted
 * viewer's configuration (config.ts), set before the first render, decides where it loads from;
 * each request carries the configured authorization.
 */

import { createBatchedDrislEntityLoader, type EntityLoader } from 'fxtr-view-shell-util'
import { viewerConfig } from '../config'

let loader: EntityLoader | null = null

export const loadEntity: EntityLoader = (id) => {
  loader ??= createBatchedDrislEntityLoader(viewerConfig().entitiesUrl, {
    headers: () => ({ ...viewerConfig().headers }),
  })
  return loader(id)
}
