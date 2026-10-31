import { createContext, useContext } from 'react'
import type { KeyValue, Sort } from '../types'
import type { Store } from './store'
import type { Selected } from '../panes/useColumns'

/**
 * How a node's panel shows it: its output (`output`: see `StepPanel`), through an `EntityPanel`
 * renderer of the output's entity type (`outputRenderer`), or through an `InvocationPanel`
 * renderer of the step (`invocation`).
 */
export type PanelView =
  | { kind: 'output' }
  | { kind: 'outputRenderer'; renderer: string }
  | { kind: 'invocation'; renderer: string }

/** The output as the viewer draws it, with no renderer. */
export const OUTPUT_VIEW: PanelView = { kind: 'output' }

/** What a run's card lists at its foot: its invocations, the rows they produced, or the values of
 *  its invocation dims. A node that was not run lists its rows. */
export type CardMode = 'invocations' | 'data' | 'keys'

/**
 * A card's table (`CardTable`): what it lists (`mode`), its rows'
 * filters and order, the page of rows it is on, whether its rows are shown (or only its header
 * row; shown until toggled), the invocations' filters, order and page, whether those are only the
 * failed ones, and the invocation the keys picked, for the node in the slice named by `sliceKey`.
 * Another slice starts at the first pages with nothing picked,
 * keeping the rest (`useCardTable`).
 */
export interface CardTableState {
  sliceKey: string
  mode: CardMode
  filters: Record<string, string>
  sort: Sort | null
  offset: number | 'last'
  rowsShown: boolean
  /** The invocations' filters and order, apart from the rows': their columns are not the rows'. */
  invocationFilters: Record<string, string>
  invocationSort: Sort | null
  /** List only the failed invocations the slice picks out: the card's failed count, which shows
   *  the invocations. */
  failedOnly: boolean
  /** Where the invocations' page starts, apart from the rows' `offset`. */
  invocationsOffset: number | 'last'
  /** The invocation picked in `keys` mode, a value for each invocation dim; null for none. */
  keysPick: Readonly<Record<string, KeyValue>> | null
}

/**
 * State the cards and panes of one level share: the slice, the value picked for each dim sliced
 * on, one per dim name, picked anywhere (so slicing at `model=X` in one card slices every card and
 * pane with a `model` dim at it, whether or not the node maps over `model`), and kept across levels;
 * and each card's table state: a store, which a card reads its own entry of, so paging one card re-renders only it (`useCardTable`). Provided by
 * `JobView`; the card tables are reset on a level change.
 */
type Slice = Readonly<Record<string, KeyValue>>

export interface LevelState {
  slice: Readonly<Record<string, KeyValue>>
  /** Set the slice, or update it from the current one (for a caller that picks after an await,
   *  so picks made meanwhile are kept). Every card's table changes height with it, so the trace
   *  holds one card still on screen while they settle: `anchor`, the id of the card the pick was
   *  made in, or by default the first card whose top is on screen (`lib/scrollAnchor.ts`). */
  setSlice: (next: Slice | ((current: Slice) => Slice), anchor?: string) => void
  /** What the panels opened from this view (from those, and so on) show on dims it does not bind:
   *  not to filter on, but for a custom view to show among the rest (fxtr-view's `useKeys`). The
   *  viewer's own tables light only a row whose panel is open. */
  selected: Selected
  /** The step names from the root workflow down to the level's workflow: the start of the address
   *  of every node of the level. */
  levelPath: readonly string[]
  cardTables: Store<ReadonlyMap<string, CardTableState>>
}

export const LevelStateContext = createContext<LevelState | null>(null)

export function useLevelState(): LevelState {
  const state = useContext(LevelStateContext)
  if (!state) throw new Error('the level state was read outside a LevelStateContext provider')
  return state
}
