/**
 * The job picker's commit graph: which commits are drawn, and where each one's dot and lines go.
 *
 * The commits arrive newest first, a commit before each of its parents (`api/jobs.ts`).
 * `keepCommits` narrows them (a search, a branch) without breaking the graph: a commit left out
 * hands its parents to the commits that named it. `layoutGraph` then gives each commit a lane,
 * as git clients do: a lane is a vertical line waiting for one commit, a commit sits on the first
 * lane waiting for it, its first parent continues that lane, and each further parent takes a lane
 * of its own. With one branch every commit is on lane 0 and the graph is a straight line.
 */

export interface GraphCommit {
  hash: string
  parents: string[]
  branch: string | null
}

/** A line across one commit's row, between lanes; coloured by `branch`, the branch of the commit
 *  at its upper end. */
export interface GraphLine {
  from: number
  to: number
  branch: string | null
}

export interface GraphRow {
  /** The lane the commit's dot is on. */
  lane: number
  /** Lines from the top of the row into the dot: one per child drawn above. */
  into: GraphLine[]
  /** Lines from the dot to the bottom of the row: one per parent. */
  out: GraphLine[]
  /** Lanes passing straight through the row, on their way to a commit further down. */
  through: GraphLine[]
}

export interface GraphLayout {
  rows: GraphRow[]
  /** How many lanes the widest row uses. */
  lanes: number
}

/** `commits` less those `keep` rejects, each kept commit's parents being the nearest kept commits
 *  its parents lead to (less any that another of them descends from). */
export function keepCommits<T extends GraphCommit>(commits: readonly T[], keep: (commit: T) => boolean): T[] {
  const nearest = new Map<string, string[]>()
  const ancestors = new Map<string, Set<string>>()
  const kept: T[] = []
  for (let index = commits.length - 1; index >= 0; index -= 1) {
    const commit = commits[index]
    const above = [...new Set(commit.parents.flatMap((parent) => nearest.get(parent) ?? []))]
    const reduced = above.filter((a) => !above.some((other) => ancestors.get(other)?.has(a)))
    if (!keep(commit)) {
      nearest.set(commit.hash, reduced)
      continue
    }
    const all = new Set(reduced)
    for (const parent of reduced) for (const ancestor of ancestors.get(parent) ?? []) all.add(ancestor)
    ancestors.set(commit.hash, all)
    nearest.set(commit.hash, [commit.hash])
    kept.push({ ...commit, parents: reduced })
  }
  return kept.reverse()
}

export function layoutGraph(commits: readonly GraphCommit[]): GraphLayout {
  /** Each lane: the commit it waits for and the branch of the commit that opened it. */
  const lanes: ({ hash: string; branch: string | null } | null)[] = []
  const free = () => {
    const index = lanes.indexOf(null)
    return index === -1 ? lanes.length : index
  }
  const rows: GraphRow[] = []
  let widest = 1
  for (const commit of commits) {
    const waiting = lanes.flatMap((lane, index) => (lane?.hash === commit.hash ? [index] : []))
    const lane = waiting[0] ?? free()
    const into = waiting.map((index) => ({ from: index, to: lane, branch: lanes[index]!.branch }))
    for (const index of waiting) lanes[index] = null
    const through = lanes.flatMap((other, index) => (other ? [{ from: index, to: index, branch: other.branch }] : []))
    const out: GraphLine[] = []
    commit.parents.forEach((parent, position) => {
      const open = lanes.findIndex((other) => other?.hash === parent)
      // A parent some lane already leads to joins that lane; else the first parent continues this
      // commit's lane and any other takes a free one.
      const target = open !== -1 ? open : position === 0 && lanes[lane] == null ? lane : free()
      if (open === -1) lanes[target] = { hash: parent, branch: commit.branch }
      out.push({ from: lane, to: target, branch: commit.branch })
    })
    rows.push({ lane, into, out, through })
    widest = Math.max(widest, lane + 1, lanes.length)
    while (lanes.length > 0 && lanes[lanes.length - 1] === null) lanes.pop()
  }
  return { rows, lanes: widest }
}
