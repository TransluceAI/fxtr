# remote Module Overview

Defines the protocol and helpers for transferring entities between a local fxtr store and a remote server. This is the glue layer between the hub (client and server) and the local git/sqlite storage.

## File Structure

```
remote/
├── remote_client.py            # RemoteClient protocol definition
├── transfer_format.py          # CBOR wire format for entity streams
├── transfer_entity_source.py   # In-memory FlatEntitySource for received entities
└── LOCAL_CODE_OVERVIEW.md
```

## How the Pieces Fit Together

```
Local store                    Wire                         Remote server
───────────                    ────                         ─────────────
                       RemoteClient protocol
tsv_namespace  ──→  negotiate(ids)  ──→  TransferService.check_known_entities()
store_sync     ──→  push_entities() ──→  TransferService.ingest_entities()
               ←──  pull_plan(ids)  ←──  TransferService.plan_export()
               ←──  pull_data(ids)  ←──  TransferService.fetch_entities()
                           │
                   transfer_format.py
                   encode/decode CBOR
                           │
                   transfer_entity_source.py
                   wraps received entities
                   for pin_entity_from_arena()
```

Pull uses a two-phase approach: `pull_plan` gets the transitive closure as an
ID list with sizes (no blob I/O), then `pull_data` fetches entity data in
bounded batches. This keeps both server and client memory bounded.

## Module Details

### remote_client.py

Defines the `RemoteClient` protocol — the four-method interface any remote backend must implement:

- `negotiate(entity_ids)` → `(known, missing)` — check which entities the remote already has
- `push_entities(entities)` → `PushResult` — upload entities
- `pull_plan(entity_ids)` → `list[(FxtrID, int)]` — get transitive closure IDs with sizes, in topological order
- `pull_data(entity_ids)` → `list[FxtrEntityFlatData]` — download specific entities (no graph walking)

Also defines `RemoteError` (base exception) and `PushResult` (dataclass). The protocol is `@runtime_checkable` so implementations can be verified at runtime.

The hub client's `HubClient` implements this protocol over HTTP. Other implementations (S3, local file sync) could be added without changing the sync layer.

### transfer_format.py

CBOR-based wire format for streaming entities. Each entity is encoded as a CBOR array:

```
[fxtr_id_bytes, entity_bytes_or_null, [[dep_id_bytes, dep_kind_int], ...]]
```

- `entity_bytes` is null for derived nodes (context, path, untracked) that have no stored content
- `dep_kind_int` is a compact integer mapping of `DepKind` enum values (0=strong_ref, 1=weak_ref, 2=ctx_node, 3=backstep, 4=unwraps_to)
- Entities are expected in topological order (deps before dependents) so the receiver can store them in a single pass

Two functions: `encode_entity_stream()` and `decode_entity_stream()`.

### transfer_entity_source.py

`TransferEntitySource` — a `FlatEntitySource[StaticBrand]` backed by an in-memory dict
of `FxtrEntityFlatData`. Used on the receiving side of a pull to feed downloaded
entities into `pin_entity_from_arena`. The name reflects that it's a narrow *source* of
entity data, not a full arena.

The source has no formal lifetime of its own: it is branded under
`fxtr2.leases.local_lease.STATIC_LIFETIME` (the static grant is never revoked). Every method
takes (and immediately discards) a `Lease[StaticBrand]`, which callers obtain for free
via `STATIC_LIFETIME.lease()`.

Internal state is split into two pieces:

- `_entities` — the dict of available data. Shared across `copy_to_new_arena`-derived
    siblings, so narrowing is O(|ids|) rather than walking the transitive closure.
- `_tracked` — the per-source set of IDs this view currently keeps accessible. Starts
    at a caller-chosen seed (default: all keys of the data dict). `load_flat` adds the
    loaded id and its deps to `_tracked`, matching the `ReadableArena.load_flat`
    contract.

Builtin tags (`.` / `!`) are not special-cased — they are ordinary tracked IDs. Data
for them is never stored; `load_flat` synthesizes an empty derived-node record when the
id is tracked.

`close()` drops this source's reference to the shared dict and clears `_tracked`.
Sibling sources retain their own reference and continue to work.
