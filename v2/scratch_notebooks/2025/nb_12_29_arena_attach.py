# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportMissingParameterType=false
# pyright: reportUnusedImport=false
# isort: skip_file

# %% ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
from __future__ import annotations

if __name__ != "__main__":
    raise ValueError("This notebook is not meant to be imported")

import os

print(f"Running in {os.getcwd()} on {os.uname().nodename}")
print(f"This kernel is associated with file: {os.path.abspath(__file__)}")
print(f"CUDA_VISIBLE_DEVICES: {os.environ.get('CUDA_VISIBLE_DEVICES')}")

#! %reload_ext autoreload
#! %autoreload 2
#! %aimport -ray


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

import dotenv

dotenv.load_dotenv()

import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import nbutil

nbutil.set_up_treescope()

import logging

logging.basicConfig(level=logging.DEBUG)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


from fxtr2.data import entity_core
from fxtr2.data import memory_arena

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

parent_1_store = memory_arena.MemoryStore(force_async_sleep=True)
parent_1_arena_1 = parent_1_store.new_empty_arena()
parent_1_arena_2 = parent_1_store.new_empty_arena()

parent_2_store = memory_arena.MemoryStore(force_async_sleep=True)
parent_2_arena_1 = parent_2_store.new_empty_arena()

child_store = memory_arena.MemoryStore(force_async_sleep=True)
child_arena = child_store.new_empty_arena()


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

# TODO: save into the parent arenas, then attach the parents to the child, make sure the loading
# works properly


def mk_ents(name: str, arena: memory_arena.MemoryArena):
    dot = arena.track_builtin(entity_core.FxtrID.from_tag_and_payload(".", b""))
    foo = {
        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(dot),
        "type": "foo",
        "foo": 42,
    }
    foo_id = arena.store(foo)
    bar = {
        "$fxtr.ctx": entity_core.RawFxtrEntityMarkerID(dot),
        "type": "bar",
        "bar": name,
        "shared": entity_core.RawStrongFxtrID(foo_id),
    }
    bar_id = arena.store(bar)
    return foo_id, bar_id


foo_1_id, bar_1_id = mk_ents("foo_1", parent_1_arena_1)
foo_2_id, bar_2_id = mk_ents("foo_2", parent_1_arena_2)
foo_3_id, bar_3_id = mk_ents("foo_3", parent_2_arena_1)


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

child_arena.lazily_attach_from_arena(bar_1_id, parent_1_arena_1)
child_arena.lazily_attach_from_arena(bar_2_id, parent_1_arena_2)
child_arena.lazily_attach_from_arena(bar_3_id, parent_2_arena_1)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

child_store._entity_states, child_store._inherited_nodes

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

bar_1 = await child_arena.load(bar_1_id)
dict(
    bar_1=bar_1,
    entity_states=child_store._entity_states,
    inherited_nodes=child_store._inherited_nodes,
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

bar_3 = await child_arena.load(bar_3_id)
dict(
    bar_3=bar_3,
    entity_states=child_store._entity_states,
    inherited_nodes=child_store._inherited_nodes,
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

foo = await child_arena.load(bar_1["shared"].inner_id)
dict(
    foo=foo,
    entity_states=child_store._entity_states,
    inherited_nodes=child_store._inherited_nodes,
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

bar_2 = await child_arena.load(bar_2_id)
dict(
    bar_2=bar_2,
    entity_states=child_store._entity_states,
    inherited_nodes=child_store._inherited_nodes,
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


# LATER TODO: after testing this, add a feature to ReadableArena that allows you to ask if a
# given node is reachable from the arena and how
# (basic idea: walk either forward or back links using an iterative search until they converge or
# possibly until proven impossible. could do work on whichever set seems the smallest. usually
# nodes only have one parent so that is usually smallest? or could step from the one node only
# as long as it still has only one parent?)
