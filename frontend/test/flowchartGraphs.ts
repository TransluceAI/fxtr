/**
 * Card dependency graphs for the flowchart layout tests: two production workflows, shapes that
 * stress one aspect of a layered layout each, and seeded random families that check the layout
 * generalizes.
 */
import type { NodeKind, StepDoc } from '../src/types'

export function card(
  id: string,
  deps: string[] = [],
  { name = id, kind, dims = [] }: { name?: string; kind?: NodeKind; dims?: string[] } = {},
): StepDoc {
  return {
    id,
    name,
    kind: kind ?? (deps.length > 0 ? 'step' : 'source'),
    method: deps.length > 0 ? name : null,
    type: { dims: [], schema: 'str' },
    dims,
    mapOver: [],
    replicaDims: [],
    output: null,
    browse: null,
    version: 'v1',
    step: 0,
    deps,
    args: [],
    columns: [],
    progress: { glyph: { shape: 'done', failed: false }, counts: null },
  }
}

/** The root level of job e76f17e7-e624-4562-8880-439a50ad5063 (cc_spoof name_self_rating). */
export function ccSpoofGraph(): StepDoc[] {
  return [
    card('subject', [], { kind: 'source' }),
    card('rating', [], { kind: 'source' }),
    card('analyst', [], { kind: 'source' }),
    card('system_prompts', [], { kind: 'source', dims: ['template'] }),
    card('names', [], { kind: 'source', dims: ['name'] }),
    card('questions', [], { kind: 'source', dims: ['question'] }),
    card('answer', ['subject', 'system_prompts', 'names', 'questions'], { dims: ['template', 'name', 'question'] }),
    card('rate', ['subject', 'rating', 'system_prompts', 'names', 'questions', 'answer'], { dims: ['template', 'name', 'question', 'replica'] }),
    card('name_mention', ['names', 'rate'], { dims: ['template', 'name', 'question', 'replica'] }),
    card('summarize', ['rating', 'names', 'rate', 'name_mention'], { dims: ['template', 'name'] }),
    card('omnibus', ['rate'], { dims: ['template'] }),
    card('compare', ['analyst', 'names', 'rate'], { dims: ['template'] }),
    card('compare_personalization', ['analyst', 'names', 'questions', 'answer'], { dims: ['template', 'question'] }),
    card('$return', [], { kind: 'source' }),
  ]
}

/** The root level of job 8f2af944-0877-42bc-8c6d-48f485c88e8b (behavior_discovery behavior_transfer). */
export function behaviorTransferGraph(): StepDoc[] {
  return [
    card('gut_check_config', [], { name: 'gut_check', kind: 'source' }),
    card('discovery', [], { kind: 'source' }),
    card('rubric', [], { kind: 'source' }),
    card('sim_keys', [], { kind: 'source', dims: ['sim_key'] }),
    card('transcripts', [], { kind: 'source', dims: ['sim_key', 'subject_model', 'replica'] }),
    card('gut_check', ['gut_check_config', 'transcripts'], { dims: ['sim_key', 'subject_model', 'replica'] }),
    card('discover_behaviors', ['discovery', 'gut_check', 'transcripts']),
    card('behaviors', ['discover_behaviors'], { dims: ['behavior'] }),
    card('judge_behavior', ['behaviors', 'rubric', 'transcripts'], { dims: ['sim_key', 'subject_model', 'replica', 'behavior'] }),
    card('combined_fit', ['gut_check', 'judge_behavior', 'sim_keys'], { dims: ['sim_key'] }),
    card('behavior_stats', ['behaviors', 'gut_check', 'judge_behavior'], { dims: ['sim_key', 'behavior'] }),
    card('$return', [], { kind: 'source' }),
  ]
}

export function chain(length: number): StepDoc[] {
  return Array.from({ length }, (_, index) => card(`chain_${index}`, index === 0 ? [] : [`chain_${index - 1}`]))
}

export function fan(width: number): StepDoc[] {
  const middle = Array.from({ length: width }, (_, index) => card(`branch_${index}`, ['root']))
  return [card('root'), ...middle, card('sink', middle.map((node) => node.id))]
}

export function binaryTree(depth: number): StepDoc[] {
  const result = [card('tree')]
  for (let level = 1; level < depth; level += 1) {
    for (let index = 0; index < 2 ** level; index += 1) {
      const parent = level === 1 ? 'tree' : `tree_${level - 1}_${Math.floor(index / 2)}`
      result.push(card(`tree_${level}_${index}`, [parent]))
    }
  }
  return result
}

export function diamonds(count: number): StepDoc[] {
  const result = [card('diamond_root')]
  let previous = 'diamond_root'
  for (let index = 0; index < count; index += 1) {
    result.push(card(`left_${index}`, [previous]), card(`right_${index}`, [previous]), card(`join_${index}`, [`left_${index}`, `right_${index}`]))
    previous = `join_${index}`
  }
  return result
}

export function parallelPipelines(width: number, length: number): StepDoc[] {
  return Array.from({ length: width }, (_, column) => Array.from({ length }, (__, row) => {
    return card(`pipeline_${column}_${row}`, row === 0 ? [] : [`pipeline_${column}_${row - 1}`])
  })).flat()
}

export function disconnected(components: number, length: number): StepDoc[] {
  return Array.from({ length: components }, (_, component) => Array.from({ length }, (__, index) => {
    return card(`component_${component}_${index}`, index === 0 ? [] : [`component_${component}_${index - 1}`])
  })).flat()
}

/** Every card reads three of the previous layer, wrapping around: dense and non-planar. */
export function layered(layers: number, width: number): StepDoc[] {
  const result: StepDoc[] = []
  for (let layer = 0; layer < layers; layer += 1) {
    for (let column = 0; column < width; column += 1) {
      const deps = layer === 0 ? [] : [0, 1, 3].map((offset) => `layer_${layer - 1}_${(column + offset) % width}`)
      result.push(card(`layer_${layer}_${column}`, deps))
    }
  }
  return result
}

/** A chain where later cards also read the first card and a card five back: many long edges. */
export function skipHeavy(length: number): StepDoc[] {
  return Array.from({ length }, (_, index) => {
    if (index === 0) return card('skip_0')
    const deps = [`skip_${index - 1}`]
    if (index > 2) deps.push('skip_0')
    if (index > 5) deps.push(`skip_${index - 5}`)
    return card(`skip_${index}`, deps)
  })
}

function seededRandom(seed: number): () => number {
  let state = seed
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0
    return state / 0x100000000
  }
}

/** Each card reads each earlier one with probability `density`. */
export function randomDag(size: number, seed: number, density = 0.08): StepDoc[] {
  const random = seededRandom(seed)
  return Array.from({ length: size }, (_, index) => {
    const deps = Array.from({ length: index }, (__, candidate) => candidate)
      .filter(() => random() < density)
      .map((candidate) => `random_${candidate}`)
    if (index > 0 && deps.length === 0 && random() < 0.8) deps.push(`random_${Math.floor(random() * index)}`)
    return card(`random_${index}`, deps)
  })
}

/**
 * Shaped like real workflows: a few registered inputs, then steps that each read one to three
 * earlier cards, mostly among the last few emitted, sometimes anything earlier (often an input).
 */
export function workflowLike(seed: number, inputs: number, steps: number): StepDoc[] {
  const random = seededRandom(seed)
  const result = Array.from({ length: inputs }, (_, index) => card(`input_${index}`))
  for (let index = 0; index < steps; index += 1) {
    const deps = new Set<string>()
    const count = 1 + Math.floor(random() * 3)
    for (let pick = 0; pick < count; pick += 1) {
      const recent = random() < 0.6
      const offset = recent
        ? result.length - 1 - Math.floor(random() * Math.min(3, result.length))
        : Math.floor(random() * result.length)
      deps.add(result[offset].id)
    }
    result.push(card(`step_${index}`, [...deps], { dims: random() < 0.4 ? ['sample'] : [] }))
  }
  return result
}

/** Thirty seeded workflow-like graphs of 11 to 34 cards. */
export function workflowFamily(): StepDoc[][] {
  return Array.from({ length: 30 }, (_, index) => workflowLike(index + 1, 3 + (index % 5), 8 + (index % 20)))
}
