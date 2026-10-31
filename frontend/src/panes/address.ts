/**
 * Panel addresses: what a panel shows, as plain data with one canonical spelling. An address is a
 * **view** applied to a **location** and **params**, written like a call:
 *
 * ```
 * (/prompts.output, prompt=3)                          the default view of a value
 * table(/judge, evidence_judge=sonnet, prompt=3)        a built-in view of a step
 * pander-score.ScoreFit/point(/score_fit, proposition=p042, type=conversational)
 * limerick-contest.poet(poet=nash)                     a page: no location
 * tree(cid:bafy….messages[2])                           an entity, and a field path into it
 * code(/judge)                                          where a step's function is implemented
 * experiment_readme()
 * ```
 *
 * - The **view** is a view's id (a renderer, a page, or a built-in: `VIEWS`), or nothing for the
 *   default of what the location shows. A view declared within another is named below it
 *   (`…ScoreFit/point`).
 * - A **location** is a step path from the job's root workflow (`/` alone is the root, `/a/b` the
 *   step `b` inside workflow step `a`), or an entity's cid (`cid:…`), then a **field path**: field
 *   names after `.`, list indices in `[]`. A step's field path starts in each invocation's record:
 *   `.output…`, `.inputs.<name>…`.
 * - **Params** bind dims (a mapped workflow's keys among them) and the params views read, by name,
 *   sorted.
 *
 * A **token** (a step name, a field name, a param's name, a view id) is bare when made of letters,
 * digits, `_` and `-` (a view id may also hold `.` and `/`), and a JSON string otherwise. A param's
 * value is bare when made of letters, digits, `_`, `-` and `.`: a bare value that reads as a number
 * is that number, and a string that would read as one is quoted.
 *
 * A **relative address** (`Rel`) is what links and browsing panels store: a view, and a location
 * relative to the location of the view before it (`RelLocation`): none (the same), `.field…` (a
 * field path below it), `../step…` (a sibling step), or an absolute one.
 */

import { shortCid } from 'fxtr-view'
import type { KeyValue } from '../types'

export type Params = Readonly<Record<string, KeyValue>>

export type Location =
  | { root: 'step'; steps: readonly string[]; path: readonly string[] }
  | { root: 'entity'; cid: string; path: readonly string[] }

export interface Address {
  /** Null: the default view of what the location shows. */
  view: string | null
  /** Null for a view of no location (a page, the experiment). */
  location: Location | null
}

export type RelLocation =
  | { kind: 'same' }
  | { kind: 'below'; path: readonly string[] }
  /** The step path of the location before it with its last step replaced by `steps`, then `path`. */
  | { kind: 'sibling'; steps: readonly string[]; path: readonly string[] }
  | { kind: 'absolute'; location: Location | null }

export interface Rel {
  view: string | null
  location: RelLocation
}

/** The viewer's own views, beside the registry's. */
export const VIEWS = {
  /** A step's output rows. */
  table: 'table',
  /** One invocation's record. */
  invocation: 'invocation',
  /** Any value as a data tree. */
  tree: 'tree',
  /** A workflow's steps, as the centre shows them. */
  trace: 'trace',
  flowchart: 'flowchart',
  /** The file a step's or workflow's function is implemented in. */
  code: 'code',
  experimentReadme: 'experiment_readme',
} as const

export class AddressError extends Error {}

const NO_PARAMS: Params = Object.freeze({})
/** The characters of a bare token: a name, a view id, a param's value. */
const NAME = '[A-Za-z0-9_-]'
const VIEW = '[A-Za-z0-9_./-]'
const VALUE = '[A-Za-z0-9_.-]'
const bareIn = (chars: string) => new RegExp(`^${chars}+$`)
const BARE: Readonly<Record<string, RegExp>> = { [NAME]: bareIn(NAME), [VIEW]: bareIn(VIEW), [VALUE]: bareIn(VALUE) }
const NUMBER = /^-?\d+(\.\d+)?$/
const INDEX = /^\d+$/

// ── formatting ──

function token(text: string, chars: string = NAME): string {
  return BARE[chars]!.test(text) ? text : JSON.stringify(text)
}

export function formatValue(value: KeyValue): string {
  if (typeof value === 'number') return String(value)
  return BARE[VALUE]!.test(value) && !NUMBER.test(value) ? value : JSON.stringify(value)
}

/** A field path: `.name` for a field, `[n]` for a list index. */
export function formatPath(path: readonly string[]): string {
  return path.map((field) => (INDEX.test(field) ? `[${field}]` : `.${token(field)}`)).join('')
}

export function formatLocation(location: Location): string {
  if (location.root === 'entity') return `cid:${token(location.cid)}${formatPath(location.path)}`
  return `/${location.steps.map((step) => token(step)).join('/')}${formatPath(location.path)}`
}

function formatParamList(params: Params): string[] {
  return Object.keys(params).sort().map((name) => `${token(name)}=${formatValue(params[name]!)}`)
}

function call(view: string | null, args: readonly string[]): string {
  return `${view === null ? '' : token(view, VIEW)}(${args.join(', ')})`
}

/** An address with the params a panel shows it at. */
export function formatAddress(address: Address, params: Params = NO_PARAMS): string {
  return call(address.view, [...(address.location ? [formatLocation(address.location)] : []), ...formatParamList(params)])
}

function formatRelLocation(location: RelLocation): string | null {
  switch (location.kind) {
    case 'same':
      return null
    case 'below':
      return formatPath(location.path)
    case 'sibling':
      return `../${location.steps.map((step) => token(step)).join('/')}${formatPath(location.path)}`
    case 'absolute':
      return location.location === null ? null : formatLocation(location.location)
  }
}

/** A relative address, with the params a panel binds itself. */
export function formatRel(rel: Rel, own: Params = NO_PARAMS): string {
  const location = formatRelLocation(rel.location)
  return call(rel.view, [...(location === null ? [] : [location]), ...formatParamList(own)])
}

// ── parsing ──

class Scanner {
  pos = 0
  constructor(readonly text: string) {}

  done(): boolean {
    return this.pos >= this.text.length
  }

  peek(): string | undefined {
    return this.text[this.pos]
  }

  startsWith(prefix: string): boolean {
    return this.text.startsWith(prefix, this.pos)
  }

  eat(prefix: string): boolean {
    if (!this.startsWith(prefix)) return false
    this.pos += prefix.length
    return true
  }

  expect(prefix: string): void {
    if (!this.eat(prefix)) this.fail(`\`${prefix}\` expected`)
  }

  skipSpaces(): void {
    while (this.peek() === ' ') this.pos += 1
  }

  fail(message: string): never {
    throw new AddressError(`${message} at ${this.pos} in ${JSON.stringify(this.text)}`)
  }

  /** A bare token (of `chars`) or a JSON string; `quoted` says which. */
  token(chars: string = NAME): { text: string; quoted: boolean } {
    if (this.peek() === '"') {
      let end = this.pos + 1
      while (end < this.text.length && this.text[end] !== '"') end += this.text[end] === '\\' ? 2 : 1
      if (end >= this.text.length) this.fail('an unterminated string')
      const text = JSON.parse(this.text.slice(this.pos, end + 1)) as string
      this.pos = end + 1
      return { text, quoted: true }
    }
    const match = new RegExp(`^${chars}+`).exec(this.text.slice(this.pos))
    if (!match) this.fail('a name expected')
    this.pos += match[0].length
    return { text: match[0], quoted: false }
  }

  /** `.name` and `[n]` segments, to the first character that starts neither. */
  path(): string[] {
    const path: string[] = []
    for (;;) {
      if (this.eat('.')) path.push(this.token().text)
      else if (this.eat('[')) {
        const match = /^\d+/.exec(this.text.slice(this.pos))
        if (!match) this.fail('a list index expected')
        this.pos += match[0].length
        this.expect(']')
        path.push(match[0])
      } else return path
    }
  }

  /** Step names separated by `/`, after a leading `/` already eaten; none for the root. */
  steps(): string[] {
    const steps: string[] = []
    if (this.done() || !/[A-Za-z0-9_"-]/.test(this.peek()!)) return steps
    steps.push(this.token().text)
    while (this.eat('/')) steps.push(this.token().text)
    return steps
  }

  /** An absolute location, or null when none starts here. */
  location(): Location | null {
    if (this.eat('cid:')) {
      const cid = this.token().text
      return { root: 'entity', cid, path: this.path() }
    }
    if (this.eat('/')) {
      const steps = this.steps()
      return { root: 'step', steps, path: this.path() }
    }
    return null
  }

  relLocation(): RelLocation {
    if (this.startsWith('.') && !this.startsWith('..')) return { kind: 'below', path: this.path() }
    if (this.eat('../')) {
      const steps = this.steps()
      if (steps.length === 0) this.fail('`../` names a step')
      return { kind: 'sibling', steps, path: this.path() }
    }
    const location = this.location()
    return location === null ? { kind: 'same' } : { kind: 'absolute', location }
  }

  params(first: boolean): Params {
    const params: Record<string, KeyValue> = {}
    let needComma = !first
    for (;;) {
      this.skipSpaces()
      if (this.peek() === ')') return params
      if (needComma) {
        this.expect(',')
        this.skipSpaces()
      }
      const name = this.token().text
      this.expect('=')
      const value = this.token(VALUE)
      params[name] = !value.quoted && NUMBER.test(value.text) ? Number(value.text) : value.text
      needComma = true
    }
  }

  /** `view(` … `)`: the view, then `inner` reading what comes before the params. */
  call<T>(inner: () => T): { view: string | null; inner: T; params: Params } {
    const view = this.peek() === '(' ? null : this.token(VIEW).text
    this.expect('(')
    this.skipSpaces()
    const start = this.pos
    const value = inner()
    const params = this.params(this.pos === start)
    this.expect(')')
    if (!this.done()) this.fail('unexpected text')
    return { view, inner: value, params }
  }
}

/** An address and the params written with it. */
export function parseAddress(text: string): { address: Address; params: Params } {
  const scanner = new Scanner(text)
  const { view, inner: location, params } = scanner.call(() => scanner.location())
  return { address: { view, location }, params }
}

/** A relative address and the params written with it. */
export function parseRel(text: string): { rel: Rel; params: Params } {
  const scanner = new Scanner(text)
  const { view, inner: location, params } = scanner.call(() => scanner.relLocation())
  return { rel: { view, location }, params }
}

/** A location as a link names one (`useLink`'s `to`): relative or absolute, alone. */
export function parseRelLocation(text: string): RelLocation {
  const scanner = new Scanner(text)
  const location = scanner.relLocation()
  if (!scanner.done()) scanner.fail('unexpected text')
  return location
}

// ── structure ──

/** `rel`'s location from `base` (the location of the view before it; null for none): the location
 *  it names. Throws `AddressError` when `rel` does not apply to `base`. */
export function applyRelLocation(rel: RelLocation, base: Location | null): Location | null {
  switch (rel.kind) {
    case 'absolute':
      return rel.location
    case 'same':
      return base
    case 'below':
      if (base === null) throw new AddressError(`${formatPath(rel.path)} is relative, and there is no location before it`)
      return { ...base, path: [...base.path, ...rel.path] }
    case 'sibling':
      if (base === null || base.root !== 'step') throw new AddressError(`../${rel.steps.join('/')} names a step, and there is no step before it`)
      if (base.steps.length === 0) throw new AddressError('the root workflow has no siblings')
      return { root: 'step', steps: [...base.steps.slice(0, -1), ...rel.steps], path: rel.path }
  }
}

export function sameLocation(a: Location | null, b: Location | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** `location` relative to `base` (the location of the panel before it), as near as it can be put:
 *  the same, a field path below it, a sibling step; else as it is. */
export function relLocationFor(location: Location | null, base: Location | null): RelLocation {
  if (location === null || base === null) return { kind: 'absolute', location }
  if (sameLocation(location, base)) return { kind: 'same' }
  const sameRoot = location.root === 'entity' ? base.root === 'entity' && base.cid === location.cid
    : base.root === 'step' && JSON.stringify(base.steps) === JSON.stringify(location.steps)
  if (sameRoot && base.path.length < location.path.length && base.path.every((field, index) => location.path[index] === field))
    return { kind: 'below', path: location.path.slice(base.path.length) }
  if (location.root === 'step' && base.root === 'step' && location.steps.length > 0 && base.steps.length > 0
    && JSON.stringify(location.steps.slice(0, -1)) === JSON.stringify(base.steps.slice(0, -1)))
    return { kind: 'sibling', steps: [location.steps.at(-1)!], path: location.path }
  return { kind: 'absolute', location }
}

/** The last part of a view's id below the view it is declared within (`…ScoreFit/point`: `point`),
 *  each part of a nested name in order; none for a view declared within nothing. */
export function nestedNames(view: string | null): string[] {
  return view === null ? [] : view.split('/').slice(1)
}

/**
 * What an address shows, named in words (how one panel names another, in tooltips and the address
 * card; its own head draws `addressParts`): the parts of `addressParts`, by name.
 */
export function addressTitle(address: Address, isPage: boolean): string[] {
  return addressParts(address, isPage).map((part) => (part.kind === 'entity' ? `entity ${shortCid(part.name)}` : part.name))
}

/** One piece of an address as a panel's head draws it. */
export interface AddressPart {
  kind: 'run' | 'entity' | 'page' | 'experiment_readme' | 'step' | 'field' | 'view'
  /** The name it is drawn with: an entity's cid (its label is the shell's to draw), a step's,
   *  field's or page's own name, a nested view's own part. */
  name: string
}

/**
 * Every piece of an address a panel's head draws, in order: the root (the run for the root
 * workflow's own address, an entity, a page's first name, the experiment), each step, the fields
 * (`output`, `inputs` and its input's name, …), then the parts of a view declared within another
 * (`point` of `…ScoreFit/point`), or `code` for a function's code. Any other view of its own is left
 * out (the view switcher shows it), and so are the params. `isPage` says the address shows a page.
 */
export function addressParts(address: Address, isPage: boolean): AddressPart[] {
  const { location, view } = address
  const parts: AddressPart[] = []
  if (location === null) {
    if (view === VIEWS.experimentReadme) return [{ kind: 'experiment_readme', name: 'experiment_readme' }]
    if (view === null) return []
    const [first, ...rest] = view.split('/')
    parts.push({ kind: isPage ? 'page' : 'view', name: first! })
    return [...parts, ...rest.map((name): AddressPart => ({ kind: 'view', name }))]
  }
  if (location.root === 'entity') parts.push({ kind: 'entity', name: location.cid })
  else if (location.steps.length === 0) parts.push({ kind: 'run', name: 'run' })
  else parts.push(...location.steps.map((name): AddressPart => ({ kind: 'step', name })))
  parts.push(...location.path.map((name): AddressPart => ({ kind: 'field', name })))
  if (view === VIEWS.code) parts.push({ kind: 'view', name: 'code' })
  parts.push(...nestedNames(view).map((name): AddressPart => ({ kind: 'view', name })))
  return parts
}

/** How many leading parts two lists of parts share. A head dims the parts it shares with the
 *  panel's before it, so what it adds stands out. */
export function sharedParts(parts: readonly AddressPart[], parent: readonly AddressPart[]): number {
  let shared = 0
  while (shared < parts.length && shared < parent.length
    && parts[shared]!.kind === parent[shared]!.kind && parts[shared]!.name === parent[shared]!.name)
    shared += 1
  return shared
}
