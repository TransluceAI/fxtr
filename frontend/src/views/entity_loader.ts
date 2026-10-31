/**
 * The one loader of entity bytes this app has: `POST /api/entities`, batched and decoded as DRISL
 * by `fxtr-view-shell-util`, so a view loading thousands of entities sends a few dozen requests,
 * a handful at a time. Shared by the shell's `useEntity` and by the invocation source, which reads
 * a step's request entity through it, so both go through the same code path, the same batches,
 * and the same URL configuration.
 */

import { createBatchedDrislEntityLoader } from 'fxtr-view-shell-util'
import { ENTITIES_URL } from '../config'

export const loadEntity = createBatchedDrislEntityLoader(ENTITIES_URL)
