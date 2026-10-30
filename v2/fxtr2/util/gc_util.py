import gc
from typing import Any

from fxtr2.leases.lease_types import Lease, LifetimeToken


def unwalk_gc_referrers(obj: object, indent=0, ignore_set=[], max_depth=10):
    """Try to find a path to this object in the GC graph."""
    if max_depth <= 0:
        return
    all_refs = gc.get_referrers(obj)
    for ref in all_refs:
        try:
            if ref is locals() or any(ref is ignore_obj for ignore_obj in ignore_set):
                continue
            if isinstance(ref, list | tuple):
                ixs = []
                for i, item in enumerate(ref):
                    if item is obj:
                        ixs.append(i)
                item = None
                print(" " * indent + f"At indices {ixs} of a {type(ref)}")
                unwalk_gc_referrers(
                    ref,
                    indent + 2,
                    ignore_set=ignore_set + [locals(), all_refs],
                    max_depth=max_depth - 1,
                )
            elif isinstance(ref, dict):
                ixs = []
                for key, value in ref.items():
                    if value is obj:
                        ixs.append(key)
                key = None
                value = None
                print(" " * indent + f"At keys {ixs} of a {type(ref)}")
                unwalk_gc_referrers(
                    ref,
                    indent + 2,
                    ignore_set=ignore_set + [locals(), all_refs],
                    max_depth=max_depth - 1,
                )

            else:
                attrs = []
                for key, value in ref.__dict__.items():
                    if value is obj:
                        attrs.append(key)
                key = None
                value = None
                print(" " * indent + f"At attrs {attrs} of a {type(ref)}")
                unwalk_gc_referrers(
                    ref,
                    indent + 2,
                    ignore_set=ignore_set + [locals(), all_refs],
                    max_depth=max_depth - 1,
                )
        except Exception as e:
            print(
                " " * indent + f"Error unwalking GC referrers for a {type(ref)}: {e}",
            )


def debug_live_leases(token: LifetimeToken[Any], max_depth=5) -> None:
    all_objs = gc.get_objects()
    for obj in all_objs:
        if isinstance(obj, Lease) and obj.is_active() and obj.grant.subject == token:
            print(f"Live lease object: {obj}")
            unwalk_gc_referrers(obj, ignore_set=[locals(), all_objs], indent=2, max_depth=max_depth)
