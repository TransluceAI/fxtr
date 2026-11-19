# fxtr changelog

## 0.2.0a1

- Push and pull jobs. `fxtr push` and `fxtr pull` move a job's record to and from a remote, a fxtr
  hub or an OCI registry (the new `oci` extra). `fxtr remote` and `fxtr hub login` set remotes up.
  Job records now carry their settled execution history (record format v0.2); fxtr still imports
  v0.1 record files. `fxtr jobs import --replace` and `fxtr jobs list --limit N` are new (#289).
- This release changes the database schema. Migrations now manage the runner's tables, and fxtr
  upgrades a database in place when it first opens it: it adds the tables `attempt_previews` and
  `job_entities`, and the column `job_attempt_provenance.from_other_job`. fxtr 0.2.0a0 still works
  on the upgraded tables. The codec and entity IDs are unchanged (#285, #289).
- Step previews. A step can publish its result in progress with `ctx.preview(...)`, and the viewer
  shows it while the step runs. `Partial[T]` holds a tagged dataclass value that is still being
  produced (#284, #285).
- The viewer's job picker groups jobs by branch and commit, with starred jobs and a preview of
  each job. The viewer keeps its state in the URL (#303, #318, #322).
- Judgment and citation views for behaviors' judges, laid out like the conversation views. A step
  that finished after its job stopped, or without its job taking the result, shows its result
  (#299, #309, #311, #328).
- The runner's cost per invocation stays flat as a job grows (#301).

## 0.2.0a0

- Rebuild custom renderer bundles for view API 4. After upgrading an installed fxtr, run
  `uv run fxtr views vendor`, run `pnpm install` in the renderer package, then run
  `uv run fxtr views build`. In TypeScript, `LinkTo` and `EntityLinkProps` now take the view
  declaration type, such as `LinkTo<typeof detailView>`. Renderers that read parameters should
  narrow JSON values to the types they expect (#261, #288).
- Entity links can pass JSON parameters to a view. For example, a link to a rollout or
  transcript can open its conversation view at a specific message (#288).
- Viewer links can open pages and explain why a target could not be opened. In the flowchart,
  `=` and `-` zoom and `0` fits the whole graph (#283).
- Installs use DuckDB `>=1.5.0,<2.0` to preserve array query behavior for integer division by
  zero and parameterized SQL (#291).

## 0.1.0a0

- Hierarchical viewer panels, linked custom detail views and pages, invocation tables and detail
  panels, and updated renderer guidance (#243).
- Content-only entity IDs; the storage-ID and inline-ID APIs are removed (#259).
- Dataclass entities check on store that their serialized data loads back reproducibly (#244).
- Batched runner and viewer reads and writes, linear status queries when planner statistics are
  missing, and rendering of behaviors' v2 rollouts (#229, #241, #230, #235).
