import { afterEach } from 'vitest'
import { forgetRecentQueries } from '../src/api/useQuery'

// Tests reuse query keys over different fixture data, so no test starts from another's values.
afterEach(() => forgetRecentQueries())
