"""Purge the present ancestors of a missing entity from the entity store.

Companion to ``scan_runset_corruption.py``. Use it when a run's ``run_data`` subtree references an
entity that is missing from the store, and content-addressed dedup has made the gap permanent:
``save_entity`` prunes its subgraph walk at any entity already present (``include_if = not
has_entity``), so once a *parent* is present without one of its descendants, re-running the
(deterministic) task re-derives the same content-addressed subgraph, the walk prunes at the present
parent, and the missing descendant is never re-written. See ``scan_runset_corruption.py`` for the
full diagnosis.

This script breaks that cycle. Starting from a root CID it walks the subtree *manually* over the raw
decoded CBOR (collecting ``cbrrr.CID`` links directly, never the normal entity loader -- which is
exactly what fails on the missing node), records which referenced entities are actually missing, and
computes every *present* entity that sits on a path between the root and a missing entity (i.e. every
present ancestor of a missing node, reachable through present nodes).

With ``--delete`` it removes those present ancestors from the entity store. That makes their
``has_entity`` return False, so the next ``save_entity`` of the re-derived result descends all the
way down and re-writes the whole chain (including the previously-missing node, *if* the task can
re-derive it). This can enlarge the hole (you're deleting present data), but for runs that don't
share this sub-data that's harmless, and if a re-run then reveals a deeper missing node you can purge
again until the chain is clean.

The root defaults to a RUN cid (as printed by scan_runset_corruption.py): the run entity is loaded
(that part works -- only its run_data subtree is broken) and its ``run_data`` is used as the walk
root. Pass ``--data-root`` to instead treat the positional CID as the subtree root directly.

    uv run scripts/purge_missing_subtree.py fxtr.toml <run_cid>
    uv run scripts/purge_missing_subtree.py fxtr.toml <run_cid> --delete
    uv run scripts/purge_missing_subtree.py fxtr.toml <entity_cid> --data-root --delete
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path
from typing import Iterator

import cbrrr

from fxtr.api import api_lib, fxtrconfig
from fxtr.object_model import core
from fxtr.object_model import locator as locator_module


def _b32(cid: cbrrr.CID) -> str:
    return cid.encode(base="base32")


def _iter_child_cids(data: object) -> Iterator[cbrrr.CID]:
    """Yield every CID link in a decoded DAG-CBOR value (the direct children of one entity).

    Walks the raw structure -- CIDs come back as ``cbrrr.CID`` from ``decode_cbor`` -- so we never
    construct the entity and never trip over a missing descendant. Map keys are always strings in
    DAG-CBOR, so only values need scanning.
    """
    if isinstance(data, cbrrr.CID):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from _iter_child_cids(value)
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from _iter_child_cids(item)


def _delete_entity(locator: locator_module.Locator, cid: cbrrr.CID) -> None:
    """Delete a single entity from the backing store (LMDB or CBOR-file)."""
    if isinstance(locator, locator_module.LMDBEntityLocator):
        env, db = locator._get_env()
        with env.begin(db=db, write=True) as txn:
            txn.delete(bytes(cid))
    elif isinstance(locator, locator_module.ClosedBinaryCBOROnDiskLocator):
        path = locator._get_file_path(cid)
        path.unlink(missing_ok=True)
    else:
        raise TypeError(f"Don't know how to delete from locator type {type(locator).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to the fxtr.toml config file.")
    parser.add_argument("cid", help="Root CID: a run CID by default (see --data-root).")
    parser.add_argument(
        "--data-root",
        action="store_true",
        help="Treat the positional CID as the subtree root directly, not as a run CID.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the present ancestors of the missing node(s) from the entity store.",
    )
    args = parser.parse_args()

    config = fxtrconfig.parse_fxtr_config(args.config)
    locator = api_lib.get_active_data_locator(config)
    loader = locator_module.CachingEntityLoader.from_locator(locator)

    given_cid = cbrrr.CID.decode(args.cid)
    if args.data_root:
        root = given_cid
    else:
        # Loading the Run entity itself works -- CID_Of fields (incl. run_data) are not eagerly
        # resolved -- so this succeeds even though the run_data subtree is broken.
        run = loader(given_cid)
        assert isinstance(run, core.Run), f"{args.cid} is not a Run (pass --data-root?)"
        root = run.run_data
        print(f"Run {_b32(given_cid)} -> run_data root {_b32(root)}")

    # Manual DFS over raw decoded CBOR, following only present entities.
    present: dict[cbrrr.CID, list[cbrrr.CID]] = {}  # cid -> its child cids
    missing: set[cbrrr.CID] = set()
    rev: dict[cbrrr.CID, list[cbrrr.CID]] = {}  # child cid -> present parent cids
    stack = [root]
    seen: set[cbrrr.CID] = set()
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        retrieved = locator.try_retrieve(cid)
        if retrieved is None:
            missing.add(cid)
            continue
        data, _ = retrieved
        children = list(_iter_child_cids(data))
        present[cid] = children
        for child in children:
            rev.setdefault(child, []).append(cid)
            if child not in seen:
                stack.append(child)

    print(f"Walked subtree from root {_b32(root)}:")
    print(f"  present entities: {len(present)}")
    print(f"  missing entities: {len(missing)}")

    if not missing:
        print("\nNo missing entities found in this subtree; nothing to purge.")
        return 0

    print("\nMissing entities:")
    for cid in missing:
        print(f"  {_b32(cid)}")

    # Present ancestors of any missing node (reachable upward through present nodes) = the paths
    # between the root and the missing thing(s). These are what we delete so the walk stops pruning.
    tainted: set[cbrrr.CID] = set()
    queue: deque[cbrrr.CID] = deque()
    for m in missing:
        for parent in rev.get(m, ()):
            if parent not in tainted:
                tainted.add(parent)
                queue.append(parent)
    while queue:
        node = queue.popleft()
        for parent in rev.get(node, ()):
            if parent not in tainted:
                tainted.add(parent)
                queue.append(parent)

    root_reaches_missing = root in tainted or root in missing
    print(
        f"\nPresent entities on a root->missing path (to delete): {len(tainted)}"
        f"  [root {'IS' if root_reaches_missing else 'is NOT'} on such a path]"
    )
    for cid in tainted:
        print(f"  {_b32(cid)}")

    if args.delete:
        if not tainted:
            print("\nNothing to delete (no present ancestors of the missing node).")
            return 0
        print(f"\n--delete: removing {len(tainted)} entities from the entity store.")
        for cid in tainted:
            _delete_entity(locator, cid)
        print(
            "Done. The entity store no longer prunes on these; re-run the task to re-persist the"
            " subtree (and re-scan to confirm, purging again if a deeper node is still missing)."
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
