"""Scan the active runset for runs whose ``run_data`` entity is missing from the entity store.

A runset maps ``task_cid -> run_cid``. Each ``Run`` references its result via ``run.run_data`` (a
CID into the entity store). If that entity is missing, the runset is corrupt: loading the run graph
will blow up as soon as something touches that node. This script finds those runs.

For every run in the active runset it loads the (cheap) ``Run`` entity and checks whether
``run.run_data`` exists in the entity store via ``locator.has_entity`` (a top-level existence check
-- it does NOT recursively verify the run_data subtree). A run is *corrupt* if the ``Run`` entity
itself won't load, or its ``run_data`` is missing.

It then walks the co-located run-graph cache (the ``_rg/`` rows in ``active.sqlite``) to find the
transitive downstream *dependents* of the corrupt runs -- runs that (directly or indirectly) consume
a corrupt run's output. These are reported and counted separately.

With ``--delete``, the corrupt runs AND their transitive dependents are removed from the runset:
both the runset rows and their matching ``_rg/`` cache rows are deleted from ``active.sqlite``. The
underlying entity store is never touched. Dropping the downstream closure (and keeping upstream)
preserves the runset invariant that every run's dependencies are also in the runset.

Run from a checkout:

    uv run scripts/scan_runset_corruption.py /path/to/fxtr.toml
    uv run scripts/scan_runset_corruption.py /path/to/fxtr.toml --delete
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cbrrr
from sqlitedict import SqliteDict
from tqdm.auto import tqdm

from fxtr.api import api_lib, fxtrconfig
from fxtr.object_model import core, locator as locator_module


def _b32(cid: cbrrr.CID) -> str:
    return cid.encode(base="base32")


def _load_dependents_map(
    active_store_path: Path,
) -> dict[cbrrr.CID, list[cbrrr.CID]]:
    """Read the co-located run-graph cache and return ``task_cid -> [direct dependent task_cids]``.

    Reads only the ``_rg/``-prefixed rows of ``active.sqlite``; the value of each is a record whose
    ``dependent_cids`` field lists the task CIDs that directly depend on this node.
    """
    prefix = api_lib.RUN_GRAPH_CACHE_KEY_PREFIX
    dependents: dict[cbrrr.CID, list[cbrrr.CID]] = {}
    if not active_store_path.exists():
        return dependents
    with SqliteDict(str(active_store_path)) as db:
        for key in tqdm(db.keys(), total=len(db), desc="Reading run-graph cache"):
            if not key.startswith(prefix):
                continue
            task_cid = cbrrr.CID.decode(key[len(prefix) :])
            record = db[key]
            dependents[task_cid] = list(record["dependent_cids"])
    return dependents


def _transitive_dependents(
    roots: set[cbrrr.CID],
    dependents_map: dict[cbrrr.CID, list[cbrrr.CID]],
) -> set[cbrrr.CID]:
    """Transitive downstream closure of ``roots`` following ``dependents_map`` edges.

    Excludes the roots themselves from the returned set.
    """
    seen: set[cbrrr.CID] = set(roots)
    stack: list[cbrrr.CID] = list(roots)
    while stack:
        cid = stack.pop()
        for dependent in dependents_map.get(cid, ()):
            if dependent not in seen:
                seen.add(dependent)
                stack.append(dependent)
    return seen - set(roots)


def _delete_from_runset(active_store_path: Path, cids: set[cbrrr.CID]) -> None:
    """Delete the runset row and matching ``_rg/`` cache row for each CID from ``active.sqlite``."""
    prefix = api_lib.RUN_GRAPH_CACHE_KEY_PREFIX
    with SqliteDict(str(active_store_path), autocommit=False) as db:
        for cid in tqdm(cids, desc="Deleting runs"):
            db.pop(_b32(cid), None)
            db.pop(prefix + _b32(cid), None)
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to the fxtr.toml config file.")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the corrupt runs and their transitive dependents from the runset.",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Instead of a top-level has_entity check on run_data, fully load the run_data entity "
            "(including all children) via the locator, catching any load failure. Catches entities "
            "missing deep inside the run_data subtree, but is much slower and more memory-heavy."
        ),
    )
    args = parser.parse_args()

    config = fxtrconfig.parse_fxtr_config(args.config)
    locator = api_lib.get_active_data_locator(config)
    loader = locator_module.CachingEntityLoader.from_locator(locator)
    active_store_path = api_lib.get_active_runset_path(config)

    runset = api_lib.load_active_runset(config)
    print(f"Active runset has {len(runset.runs)} runs.")

    # A corrupt run: either the Run entity won't load, or its run_data is unavailable. With --deep
    # we fully load the run_data subtree (catching any missing descendant); otherwise we just check
    # that the top-level run_data entity exists.
    if args.deep:
        print("Deep mode: fully loading each run_data subtree (slower).")
    ok_count = 0
    corrupt: list[tuple[cbrrr.CID, cbrrr.CID, str]] = []  # (task_cid, run_cid, timestamp-or-reason)
    for task_cid, run_cid in tqdm(runset.runs.items(), desc="Checking runs"):
        try:
            run = loader(run_cid)
            assert isinstance(run, core.Run)
        except Exception as e:
            corrupt.append((task_cid, run_cid, f"<run entity failed to load: {e!r}>"))
            continue
        if args.deep:
            try:
                loader(run.run_data)
            except Exception as e:
                corrupt.append(
                    (task_cid, run_cid, f"{run.run_info.timestamp} <run_data load failed: {e!r}>")
                )
            else:
                ok_count += 1
        elif locator.has_entity(run.run_data):
            ok_count += 1
        else:
            corrupt.append((task_cid, run_cid, run.run_info.timestamp))

    # Transitive downstream dependents of the corrupt runs (counted separately).
    corrupt_task_cids = {task_cid for task_cid, _, _ in corrupt}
    dependents_map = _load_dependents_map(active_store_path)
    dependent_task_cids = _transitive_dependents(corrupt_task_cids, dependents_map)

    print()
    print("=" * 80)
    print(f"OK runs:                    {ok_count}")
    print(f"Corrupt runs (missing data):{len(corrupt)}")
    print(f"Downstream dependents:      {len(dependent_task_cids)}")
    print("=" * 80)

    if corrupt:
        print("\nCorrupt runs (task_cid, run_cid, run timestamp):")
        for task_cid, run_cid, ts in corrupt:
            print(f"  task={_b32(task_cid)}  run={_b32(run_cid)}  ts={ts}")

    if dependent_task_cids:
        print("\nDownstream dependents (task_cid) that transitively depend on a corrupt run:")
        for task_cid in dependent_task_cids:
            run_cid = runset.runs.get(task_cid)
            run_str = _b32(run_cid) if run_cid is not None else "<not in runset>"
            print(f"  task={_b32(task_cid)}  run={run_str}")

    if args.delete:
        to_delete = corrupt_task_cids | dependent_task_cids
        if not to_delete:
            print("\nNothing to delete.")
            return 0
        print(
            f"\n--delete: removing {len(to_delete)} runs "
            f"({len(corrupt_task_cids)} corrupt + {len(dependent_task_cids)} dependents) "
            f"from {active_store_path}"
        )
        _delete_from_runset(active_store_path, to_delete)
        print("Done. (Entity store untouched.)")

    return 1 if corrupt else 0


if __name__ == "__main__":
    sys.exit(main())
