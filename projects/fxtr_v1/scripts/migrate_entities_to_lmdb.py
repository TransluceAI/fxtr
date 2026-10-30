"""Migrate a CBOR-file entity store to an LMDB entity store.

Converts an old-style entity directory (millions of CID-addressed ``.cbor`` files written by
``ClosedBinaryCBOROnDiskLocator``) into a single LMDB database that ``LMDBEntityLocator`` reads. The
source is only ever read; nothing is deleted or modified. After migrating, cut over by setting

    [data]
    entity_store_format = "lmdb"

in your fxtr config (and you may then delete the old ``entities/`` directory yourself).

Why it's fast:

  - **No decode / re-encode.** The ``.cbor`` file bytes ARE the LMDB value; we copy them verbatim.
  - **No re-hashing.** Each entity's CID is recovered from its path (the inverse of the file
    locator's ``_get_file_path``), per the migration plan (trust path-derived CIDs).
  - **Append-mode writes, batched.** Shards (the ``bafyrei/XX/YY/`` directories) are processed in
    CID order and accumulated into a transaction that commits once it holds at least
    ``--commit-batch-size`` entities (default 100) — sparse stores have tiny shards, so this avoids a
    transaction per 2-5 entities. The batch's keys are sorted and, since shards are processed in
    order, are all greater than everything already committed, so writes use ``MDB_APPEND`` (no B-tree
    rebalancing). NOTE: the base32 CID alphabet is NOT in ASCII order, so ordering is by base32
    *symbol value* (== raw CID byte order), not string order.
  - **Parallel reads.** Files within a shard are read across a thread pool (the source walk is the
    bottleneck — it's the inode pressure we're escaping).

Resumable: batches commit only at shard boundaries (every shard lands wholly in one transaction) and
completed shards are recorded in a ``<dest>.progress.json`` sidecar; re-running skips them. The
sidecar is only an optimization — if a run crashed after a commit but before recording it, the
affected shards are re-read and written again idempotently (content-addressed), so resume is correct
regardless of sidecar staleness.

Run from a checkout (``lmdb`` is a core fxtr dependency, so the workspace env already has it):

    uv run scripts/migrate_entities_to_lmdb.py /path/to/fxtr.toml
    uv run scripts/migrate_entities_to_lmdb.py /path/to/fxtr.toml --source ENTITIES --dest OUT.lmdb
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cbrrr
import lmdb
from tqdm.auto import tqdm

# base32 multibase alphabet used by CIDs (multibase prefix 'b'). The index is the symbol's value, so
# ordering by these indices matches raw CID byte order; plain ASCII ordering does NOT (e.g. '2' has
# value 26 but ASCII 50 > 'a').
_B32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_B32_ORDER = {c: i for i, c in enumerate(_B32_ALPHABET)}
# Every fxtr entity CID base32-encodes to this constant prefix (cidv1 dag-cbor sha-256).
_CID_PREFIX = "bafyrei"
# Sub-database name; must match LMDBEntityLocator so the migrated store is readable.
_DB_NAME = b"entities"


def _symbol_sort_key(name: str) -> list[int]:
    """Sort key for a base32 string by symbol value (== raw byte order)."""
    return [_B32_ORDER[c] for c in name]


def _cid_key_from_path(d1: str, d2: str, stem: str) -> bytes:
    """Recover raw CID bytes from a file's shard dirs + stem (inverse of _get_file_path)."""
    return bytes(cbrrr.CID.decode(_CID_PREFIX + d1 + d2 + stem))


def _load_progress(progress_path: Path) -> set[str]:
    if progress_path.exists():
        return set(json.loads(progress_path.read_text()).get("done", []))
    return set()


def _save_progress(progress_path: Path, done: set[str]) -> None:
    tmp = progress_path.with_name(progress_path.name + ".tmp")
    tmp.write_text(json.dumps({"done": sorted(done)}))
    tmp.replace(progress_path)


def _read_shard(d1: str, d2: str, files: list[Path], workers: int) -> list[tuple[bytes, bytes]]:
    """Read all .cbor files in a shard into (cid_key, value_bytes) pairs (value = raw file bytes)."""

    def read_one(f: Path) -> tuple[bytes, bytes]:
        stem = f.name[: -len(".cbor")]
        return _cid_key_from_path(d1, d2, stem), f.read_bytes()

    if workers <= 1:
        return [read_one(f) for f in files]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(read_one, files))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("config", type=Path, help="Path to the fxtr config TOML (paths/map size).")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="CBOR-file entity store to read (default: <data.active>/entities).",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="LMDB store directory to write (default: <data.active>/entities.lmdb).",
    )
    parser.add_argument(
        "--map-size-gb",
        type=int,
        default=None,
        help="LMDB map size in GiB (default: config's data.lmdb_map_size_gb).",
    )
    parser.add_argument(
        "--read-workers",
        type=int,
        default=16,
        help="Threads used to read files within a shard (default 16).",
    )
    parser.add_argument(
        "--commit-batch-size",
        type=int,
        default=100,
        help="Accumulate whole shards into one transaction until it holds at least this many"
        " entities, then commit (default 100). Sparse stores have tiny shards, so batching avoids a"
        " transaction per 2-5 entities. Flushes only at shard boundaries, so resume stays at shard"
        " granularity (same .progress sidecar).",
    )
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2

    from fxtr.api import fxtrconfig

    config = fxtrconfig.parse_fxtr_config(args.config)
    source = args.source or fxtrconfig.get_active_entity_store(config)
    dest = args.dest or fxtrconfig.get_active_entity_store_lmdb(config)
    map_size = (args.map_size_gb or config.data.lmdb_map_size_gb) * 1024**3

    bafyrei = source / _CID_PREFIX
    if not bafyrei.is_dir():
        print(
            f"Source has no '{_CID_PREFIX}' directory (not an entity store?): {source}",
            file=sys.stderr,
        )
        return 2

    dest.mkdir(parents=True, exist_ok=True)
    progress_path = dest.with_name(dest.name + ".progress.json")
    done = _load_progress(progress_path)

    print(f"Source (read-only): {source}")
    print(f"Dest (LMDB):        {dest}")
    print(f"Map size:           {map_size // 1024**3} GiB")
    if done:
        print(f"Resuming: {len(done)} shards already migrated (skipped).")

    # Enumerate shards in CID (symbol) order so append-mode writes stay globally ordered.
    d1_dirs = sorted(
        (d for d in bafyrei.iterdir() if d.is_dir()), key=lambda p: _symbol_sort_key(p.name)
    )
    shards: list[tuple[Path, str, str]] = []
    for d1 in d1_dirs:
        for d2 in sorted(
            (d for d in d1.iterdir() if d.is_dir()), key=lambda p: _symbol_sort_key(p.name)
        ):
            shards.append((d2, d1.name, d2.name))

    # Migrator-only fast env flags: writeable mmap, async/no-sync during the bulk load; sync once at
    # the end. (Live processes open without writemap; see LMDBEntityLocator.)
    env = lmdb.Environment(
        str(dest),
        map_size=map_size,
        subdir=True,
        max_dbs=1,
        writemap=True,
        map_async=True,
        metasync=False,
        sync=False,
    )
    db = env.open_db(_DB_NAME, create=True)

    # Accumulate whole shards into a buffer and commit once it holds >= commit_batch_size entities.
    # Flushing only at shard boundaries keeps every shard wholly inside one transaction, so a crash
    # never half-writes a shard and resume stays at shard granularity. Within a batch the combined
    # keys are sorted ascending; since shards are processed in CID order, each batch's keys are also
    # all greater than everything already committed, so append mode stays valid across batches.
    batch_size = args.commit_batch_size
    buf_pairs: list[tuple[bytes, bytes]] = []
    buf_shards: list[str] = []
    total_entities = 0

    def flush() -> None:
        nonlocal buf_pairs, buf_shards, total_entities
        if buf_pairs:
            buf_pairs.sort(key=lambda kv: kv[0])
            with env.begin(write=True, db=db) as txn:
                cur = txn.cursor()
                _consumed, added = cur.putmulti(buf_pairs, append=True)
                if added != len(buf_pairs):
                    # Some keys already present (resuming over a batch a previous run committed but
                    # whose progress it didn't record before crashing) — append silently skips them.
                    # Re-put the whole batch idempotently without append; safe because entities are
                    # content-addressed (identical bytes), just slower than the append fast path.
                    cur.putmulti(buf_pairs, append=False, overwrite=True)
            total_entities += len(buf_pairs)
        if buf_shards:
            done.update(buf_shards)
            _save_progress(progress_path, done)
        buf_pairs = []
        buf_shards = []

    try:
        with tqdm(total=len(shards), desc="Migrating shards", smoothing=0) as pbar:
            for shard_dir, d1name, d2name in shards:
                shard_id = f"{d1name}/{d2name}"
                if shard_id in done:
                    pbar.update(1)
                    continue
                files = [f for f in shard_dir.iterdir() if f.is_file() and f.suffix == ".cbor"]
                if files:
                    buf_pairs.extend(_read_shard(d1name, d2name, files, args.read_workers))
                buf_shards.append(shard_id)
                if len(buf_pairs) >= batch_size:
                    flush()
                pbar.update(1)
                pbar.set_postfix(entities=total_entities + len(buf_pairs))
            flush()  # commit the final remainder
        print("Syncing LMDB to disk ...")
        env.sync(True)
    finally:
        env.close()

    print(
        f"Done. Migrated {total_entities} entities this run into {dest}"
        f" ({len(shards)} shards total)."
    )
    print(
        'Source was not modified. Cut over by setting [data] entity_store_format = "lmdb" in your'
        " config; you may then delete the old entities/ directory."
    )
    print(f"(Progress sidecar kept at {progress_path}; delete it once you're done.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
