# fxtr changelog

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
