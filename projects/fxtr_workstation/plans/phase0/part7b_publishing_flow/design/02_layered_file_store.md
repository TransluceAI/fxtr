# Piece: the layered file store (the nix↔fxtr bridge format)

> Status: provisional first pass (2026-06-12). Line numbers approximate.

The layered file store is the format every nix-built entity travels through,
and the format the part-7 TS step must learn to write. Authoritative docs:
`v2/fxtr2/data/layered_file_store/` (+ its LOCAL_CODE_OVERVIEW.md) and the
original design in `v2/plans/phase3/stage11_nix_layers/part1_layered_file_store.md`.

## 1. On-disk format (version 1)

```
<layer_dir>/
├── layer_format.json      # {"version": 1} — written LAST (completion marker)
├── pins.json              # {pin_name: id_string} — layer-LOCAL pins
├── parents.json           # [abs_path, ...] — parent layers in search order
├── data/<id_string>.cbor  # one FxtrEntityFlatData per entity with local bytes
└── precomp/<id_string>.json  # "lives in the remote" marker + provenance info
```

- `layer_format.py:50-134` — version constant + atomic (tmp+rename) helpers.
  All three top-level JSON files are **required** even when empty.
- **`data/<id>.cbor`** (`layer_format.py:216-241`): single CBOR value in the
  remote transfer format (`data/remote/transfer_format.py:34-49`):
  `[id_bytes, entity_bytes_or_null, [[dep_id_bytes, dep_kind_int], ...]]`,
  dep kinds 0..4 = STRONG_REF, WEAK_REF, CTX_NODE, BACKSTEP, UNWRAPS_TO.
  `entity_bytes` is null for non-contentful derived nodes (c/p/u/./!).
  Filename must match the encoded id.
- **`precomp/<id>.json`** (`layer_format.py:249-277`): optional keys
  `unwraps_to` / `ctx_node` / `backstep` (id-strings). The *existence* of a
  precomp file (and absence of a data file anywhere in the search set) means
  "this entity's bytes live in the runtime-configured remote". Entries are
  transitively closed (`arena_util.py:257-309`, shared `PrecompEntry` type).
- **Pins are layer-local** — a reader of layer L sees only L's `pins.json`;
  **data walks are transitive** through parents (search set expands lazily as
  layers are touched: `search.py:73-195`, including expansion on *miss*).
- **`parents.json` holds absolute paths** captured at write time (typically
  `/nix/store/...`); portability is handled read-side via `ParentPathRemap`
  (`search.py:25-57`) — see the podman materialize remap in `design/03` §2.

## 2. Writer: `LayeredFileStoreIsolatedWriter` (`writer.py:292-667`)

`async with LayeredFileStoreIsolatedWriter(out_dir, parents=[...],
remote=(deep_fetchable, lease)|None, validate_on_close=True)`:

- `pin_to_this_layer(name, entity_id, from_scope)` (`writer.py:520-572`) —
  record the pin and DFS-walk the entity's transitive closure out of
  `from_scope`, writing `data/` files for everything not already covered by a
  parent layer.
- `mark_as_remote(entity_id)` (`writer.py:418-462`) — write a precomp-only
  pointer: validates the id against the configured remote, computes the
  precomp closure, writes `precomp/<id>.json`. No data file.
- `preload(ref)` (`writer.py:464-518`) — fetch a known-remote entity through
  the remote, write its data into this layer (and precomp markers for
  newly-exposed uncovered deps), and return the loaded value. This is how a
  build-step writer "loads some entities from a remote store and does some
  postprocessing".
- `validate_on_close` walks every written data file and asserts each dep is
  resolvable in this layer or a parent (`writer.py:634-653`).
  `layer_format.json` is written only on clean exit, so a crashed build never
  looks like a complete layer.

## 3. Reader: `LayeredFileStoreReader` + `LayeredFileArena`

- `LayeredFileStoreReader(layer_dir, remote=..., parent_path_remap=...)`
  (`reader.py:44-134`) — an `FxtrReadNamespace`; per-retrieve it builds a
  fresh `LayeredFileArena` over the shared search set and attaches the pinned
  id into its scope (`reader.py:180-219`). The reader's scope is marked
  **not deeply fetchable**; remote resolution lives inside the arena.
- `LayeredFileArena.load_flat` resolution order (`arena.py:299-339`):
  local `data/` in the search set → known-remote set → lazy `precomp/`
  discovery (marks known-remote) → `remote.try_lookup(id)` → else
  `LayerEntityMissingError`. Precomp-only id with no remote configured →
  `LayerRemoteNotConfiguredError` (`errors.py:18-27`).
- The remote is a generic `(DeepFetchable, Lease)` pair — anything
  implementing `try_lookup` works: `HttpDeepFetchable`, the repo entity store
  (`RepoEntityStore.try_lookup`), or a custom in-process adapter. **There is
  no on-disk record of *which* remote a layer expects** — the layer just
  asserts "remote-resolvable"; the consumer supplies the remote at open time.
  (For part 7b this is load-bearing: the published flake's layers can say
  "remote" while the serving script decides that the remote is the repo
  entity store.)

## 4. Assessment for part 7b's new writers

**TS writer feasibility** *(deferred per OPEN-6 — kept for the future v2/ts
redo, see `../followups.md`)*: the format is JSON + standalone CBOR files; a
Node-side writer needs (a) canonical CBOR encode of the 3-tuple (the
`cbor2` npm package is already used by `@fxtr/core` with matching canonical
sort), (b) tmp+rename atomic writes, (c) the discipline of writing
`layer_format.json` last, (d) closure completeness — every dep of a written
entity must land in `data/`, a parent layer, or `precomp/`. The dep list for
an entity is derivable from its CBOR (walk tags 33001/33002/33000 +
provenance kinds), which `@fxtr/core`'s visitor already does for weak/strong/
marker; UNWRAPS_TO/CTX_NODE/BACKSTEP kinds need provenance support TS doesn't
have yet (see `design/04` §5 and `discussion_topics.md`).

**TSV→layer rewrite feasibility:** mechanically, TSV rows map 1:1 onto
`pins.json` entries; the subtle part is producing the **precomp closure** for
each pinned id without local data. *Decided (OPEN-2/3/15):* whole-namespace
publication does **not** go through the layer format at all (pure export
manifest, server-side precomp at serve time — see `design/06`); layers enter
only when entities must be readable *inside* builds, via FODs that read
`FXTR_REMOTE_*` and fetch bytes (and `mark_as_remote` precomp markers for
anything deliberately left remote) over the `FXTR_REMOTE_BASE_URL` rehost,
output hash pinned. (Part 2 ships `entityFromTsvPin`; custom such FODs are the
part-3 builder path — see `part2_namespace_export_and_preload.md`.) (Rejected alternatives, for the record: a committed precomp sidecar
next to the TSVs; a "pin without precomp" format relaxation; FOD
namespace-reexport layers.)
