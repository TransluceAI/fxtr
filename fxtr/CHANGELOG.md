# fxtr changelog

## 0.1.0a0

- Hierarchical viewer panels, linked custom detail views and pages, invocation tables and detail
  panels, and updated renderer guidance (#243).
- Content-only entity IDs; the storage-ID and inline-ID APIs are removed (#259).
- Dataclass entities check on store that their serialized data loads back reproducibly (#244).
- Batched runner and viewer reads and writes, linear status queries when planner statistics are
  missing, and rendering of behaviors' v2 rollouts (#229, #241, #230, #235).
