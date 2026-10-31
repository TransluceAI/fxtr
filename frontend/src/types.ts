import type { CID } from 'multiformats/cid'
import type { Counts, Glyph, RunState } from './lib/progress'

/**
 * A decoded array cell: what DAG-CBOR gives back for one stored value. Entity ids arrive as `CID`
 * objects and `bytes` as `Uint8Array`, so neither is mistaken for a string; big integers may come
 * back as `bigint`.
 */
export type Cell =
  | null
  | boolean
  | number
  | bigint
  | string
  | Uint8Array
  | CID
  | Cell[]
  | { [key: string]: Cell }

/** Dim keys are the `int` and `str` values an array is indexed by. */
export type KeyValue = string | number

export type DimKindName = 'int' | 'str'

/** A value schema as the backend serializes it (`json_values.schema_to_json`). */
export type SchemaJson =
  | 'int'
  | 'float'
  | 'bool'
  | 'str'
  | 'bytes'
  | 'id'
  | { id: string }
  | { list: SchemaJson }
  | { struct: Record<string, SchemaJson> }
  | { nullable: SchemaJson }

/** An array type as the backend serializes it: dims as `[name, kind]` pairs plus the schema. */
export interface ArrayTypeJson {
  dims: [string, DimKindName][]
  schema: SchemaJson
}

/**
 * A column is either a dim of the array (key) or a field of its value schema (value). An array
 * whose value is not a struct gets a single anonymous value column.
 */
export interface Column {
  /** Unique within the table: `key:<dim>` or `value:<field>` (`value:` for the anonymous value). */
  key: string
  label: string
  group: 'key' | 'value'
  schema: SchemaJson
  numeric: boolean
  /** Its cells describe the data rather than hold it (an invocation's row count): drawn faint and
   *  italic. */
  describes?: boolean
}

export interface TableRow {
  id: string
  /** This row's dim key, in dim order. */
  key: KeyValue[]
  cells: Record<string, Cell>
  /** A failed invocation standing in its rows' place: its key cells are the invocation's keys,
   *  and it has no value cells. */
  failure?: { jobId: string; invocation: StepInvocation }
  /** The invocation the row stands for, in a table listing a run's invocations. */
  invocation?: StepInvocation
}

/** A stored array or a run's current output, and the keys picked out of it: a run's invocation
 *  dims and any dim produced inside one invocation. */
export type OutputSource = { cid: string; selection?: Record<string, KeyValue> } | {
  jobId: string; pathname: string; nodeId: string; selection?: Record<string, KeyValue>
}

export interface RunSource { jobId: string; pathname: string; nodeId: string }

/**
 * How a node entered the workflow: a child workflow, a step, or a leaf it was given (an input or a
 * registered source array).
 */
export type NodeKind = 'workflow' | 'step' | 'source'

/**
 * One token of an argument expression: a literal piece of the call, or a reference to a step of
 * the same level. `query(previous)` is `['query(', { ref }, ')']`.
 */
export type ExprToken = string | { ref: string }

/** One argument a run node was called with, as the expression that built it from the level's steps. */
export interface StepArg {
  name: string
  expr: ExprToken[]
  /** The dims of the argument's array type; the mapped ones are what an invocation sees a slice of. */
  dims: string[]
}

/** One invocation of a run node: a spawn, keyed by the mapped and replica dim values. */
export interface StepInvocation {
  /** The mapped dim values then the replica dim values, by dim name. */
  key: Record<string, KeyValue>
  /** A child workflow's invocation pathname, for opening its level. */
  childPathname: string | null
  /** A step's cache key, for looking up its state. */
  stepCacheKey: string | null
  /** The array this invocation produced, if any. */
  resultCid: string | null
  state: RunState
}

/** When a run's invocations ran, counting only attempts the job ran itself. Times are this
 *  browser's (comparable with `Date.now()`): the database's, shifted by how far its clock was
 *  ahead when the overview arrived. */
export interface RunActivity {
  firstStartedAt: number | null
  lastFinishedAt: number | null
  /** How many invocations the job finished, completed or failed. */
  finished: number
  /** When the longest-running invocation still running started. */
  oldestRunningSince: number | null
}

/** A run's failed invocations sharing an error type, with one of their messages. */
export interface FailureGroup {
  typeName: string
  message: string
  count: number
}

/** How far a card has got: its glyph, and for a run how many of its invocations are in each state,
 *  when they ran, and how they failed. */
export interface CardProgress {
  glyph: Glyph
  /** Null, like `activity`, for a node that was registered rather than run. */
  counts: Counts | null
  activity: RunActivity | null
  /** Most frequent first; empty for a node that was not run. */
  failures: FailureGroup[]
}

/** Metadata used to label and render an output table, independent of its loaded page. */
export interface TableDescriptor {
  id: string
  name: string
  type: ArrayTypeJson
  dims: string[]
  mapOver: string[]
  replicaDims: string[]
  columns: Column[]
  progress: CardProgress
}

export interface TablePage extends TableDescriptor { rows: TableRow[] }

/** A table's order: one column, ascending or descending. */
export interface Sort {
  key: string
  direction: 'asc' | 'desc'
}

/** Which page of an output a table reads: the rows its filters match (column key → text), in its
 *  order, from `offset` (`last` for the last page, whatever the total). */
export interface TableQuery {
  filters: Record<string, string>
  sort: Sort | null
  offset: number | 'last'
}

/** One graph card. Row and invocation pages belong to their mounted readers. */
export interface StepDoc extends TableDescriptor {
  kind: NodeKind
  /** The function behind this node (a step or workflow name); null for a registered array. */
  method: string | null
  /** The code version (a CID) the level's latest attempt ran, which says where `method` is
   *  implemented (see `useSource`); null before an attempt. */
  codeVersion: string | null
  /** Position in the level, by emission order. */
  step: number
  /** Steps of this level the node reads, through its arguments. */
  deps: string[]
  /** What the run was called with, in order. Empty for a node that has no function. */
  args: StepArg[]
  output: OutputSource | null
  browse: RunSource | null
  version: string
}

/**
 * Where one function is implemented, from the backend: as a job's code version records it, at its
 * pinned commit (debug_web's `recorded_source_response`), or as the backend's working tree has it
 * (`source_response`, with a null `commit`).
 */
export interface SourceInfo {
  file: string
  repoRoot: string
  symbol: string
  startLine: number
  endLine: number
  docstring: string
  commit: string | null
}

/** A file of the project's repository, from the backend (debug_web's `file_response`): as
 *  `commit` has it, or as the working tree does when `commit` is null. */
export interface FileInfo {
  path: string
  commit: string | null
  text: string
}

/** The project's EXPERIMENT.md the backend serves (see `EXPERIMENT_README_URL`): the file it read and its markdown,
 *  unrendered. */
export interface ExperimentReadmeInfo {
  path: string
  markdown: string
}


/** The outcome and timing of one invocation: a step's attempt, or a workflow's last attempt. */
export interface InvocationStatus {
  state: string
  startedAt: string | null
  finishedAt: string | null
  error: { typeName: string; message: string } | null
}

/** What a workflow invocation returned. */
export interface LevelResult {
  /** The step returned when it was a card of the level; null when a derived node was returned. */
  stepId: string | null
  /** The expression of the returned node over the level's steps. */
  expr: ExprToken[]
  /**
   * The returned value as a table, when the returned node is derived (no card holds its value) and
   * the invocation recorded a result.
   */
  table: StepDoc | null
}

/** One workflow invocation's DAG, as a list of step cards, with what it returned. */
export interface Level {
  jobId: string
  pathname: string
  workflowName: string
  /** The code version (a CID) the latest attempt ran, which says where the workflow and its
   *  nodes' functions are implemented (see `useSource`); null before an attempt. */
  codeVersion: string | null
  status: InvocationStatus
  attemptCount: number
  steps: StepDoc[]
  /** Null when the invocation has not returned (it failed, or is still running). */
  result: LevelResult | null
  /** The array the invocation returned, once it has a result. */
  resultCid: string | null
  /** The invocation's own state, red when it or any invocation it spawned failed. */
  glyph: Glyph
  /** Whether anything shown here may still change: the invocation or one of its spawns is live. */
  live: boolean
  /**
   * The node the running attempt last asked to observe, when that is the last thing it did: most
   * likely what the body is waiting on (a heuristic; the observe may have been answered already).
   */
  awaiting: { id: string; expr: ExprToken[] } | null
}

/** A workflow invocation reached from the root, as shown in the breadcrumbs. */
export interface LevelRef {
  pathname: string
  /** The last segment of the pathname: the run node's name with the mapped key. */
  label: string
}

export interface JobSummary {
  id: string
  workflowName: string
  root: string
  createdAt: string
  state: string
  failed: boolean
}
