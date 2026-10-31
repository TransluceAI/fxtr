import type { KeyValue, StepDoc } from '../types'

/** Every step of a level by id, for the panes that resolve ids rather than walk. */
export function indexSteps(steps: StepDoc[]): ReadonlyMap<string, StepDoc> {
  const out = new Map<string, StepDoc>()
  for (const step of steps) {
    if (out.has(step.id)) throw new Error(`duplicate step id ${step.id}`)
    out.set(step.id, step)
  }
  return out
}

/** The last segment of an invocation pathname: the run node's name with its mapped key. */
export function pathnameLabel(pathname: string): string {
  // A mapped key may itself contain "/" inside a quoted string, so split outside brackets only.
  let depth = 0
  let start = 0
  for (let i = 0; i < pathname.length; i++) {
    const c = pathname[i]
    if (c === '[') depth += 1
    else if (c === ']') depth -= 1
    else if (c === '/' && depth === 0) start = i + 1
  }
  return pathname.slice(start)
}

/**
 * The mapped key a pathname's last segment names (`name[dim:value,…]`, values JSON-encoded, as the
 * runner writes them: `append_mapped_key` in fxtr's `experiment/workflows.py`), by dim; empty for a
 * segment with none.
 */
export function mappedKeyOf(label: string): Record<string, KeyValue> {
  const open = label.indexOf('[')
  if (open < 0) return {}
  if (!label.endsWith(']')) throw new Error(`a mapped key in ${label} is not closed`)
  const inner = label.slice(open + 1, -1)
  const key: Record<string, KeyValue> = {}
  let at = 0
  while (at < inner.length) {
    const colon = inner.indexOf(':', at)
    if (colon < 0) throw new Error(`no value for a dim in ${label}`)
    const dim = inner.slice(at, colon)
    let end = colon + 1
    if (inner[end] === '"') {
      end += 1
      while (end < inner.length && inner[end] !== '"') end += inner[end] === '\\' ? 2 : 1
      end += 1
    } else while (end < inner.length && inner[end] !== ',') end += 1
    const value: unknown = JSON.parse(inner.slice(colon + 1, end))
    if (typeof value !== 'string' && typeof value !== 'number') throw new Error(`${dim} in ${label} is not a key value`)
    key[dim] = value
    at = end + 1
  }
  return key
}

/** The levels from the root down to the workflow invocation at `pathname`, outermost first, as the
 *  breadcrumbs hold them; none for the root itself. A pathname is the root's, then `/` and a
 *  segment per level (a node's name, with its mapped key). */
export function levelTrail(root: string, pathname: string): { pathname: string; label: string }[] {
  if (pathname === root) return []
  if (!pathname.startsWith(`${root}/`)) throw new Error(`${pathname} is not below the run ${root}`)
  const trail: { pathname: string; label: string }[] = []
  let depth = 0
  for (let i = root.length + 1; i <= pathname.length; i++) {
    const c = pathname[i]
    if (c === '[') depth += 1
    else if (c === ']') depth -= 1
    else if ((c === '/' && depth === 0) || i === pathname.length) {
      const prefix = pathname.slice(0, i)
      trail.push({ pathname: prefix, label: pathnameLabel(prefix) })
    }
  }
  return trail
}
