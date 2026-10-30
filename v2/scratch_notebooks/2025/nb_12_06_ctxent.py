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

logging.basicConfig(level=logging.INFO)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

import dataclasses
import hashlib

import cbor2
import fxtr2.data.entity_core as entity_core

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

entity_core.FxtrID.from_tag_and_payload("!", b"hello")

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


entity_core.FxtrID.from_tag_and_payload(
    "p", cbor2.dumps([hashlib.sha256(b"hello").digest(), 0, 3, 16, 327, 4, 3])
)

# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


entity_core.FxtrID.from_tag_and_payload(
    "d",
    hashlib.sha256(b"abracadabra").digest(),
    provenance_parent=entity_core.FxtrID.from_tag_and_payload(
        "p", cbor2.dumps([hashlib.sha256(b"hello").digest(), 0, 16, 327, 4, 3])
    ),
)


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓


@dataclasses.dataclass(frozen=True)
class LeafEntity(
    entity_core.DataclassEntity,
    fxtr_type=entity_core.FxtrID.from_tag_and_payload("!", b"LeafEntity"),
):
    name: str


@dataclasses.dataclass(frozen=True)
class MyEntity(
    entity_core.DataclassEntity,
    fxtr_type=entity_core.FxtrID.from_tag_and_payload("!", b"MyEntity"),
):
    weak_id: entity_core.FxtrID
    strong_id: entity_core.StrongLinkedFxtrID
    weak_ref: entity_core.EntityRef[LeafEntity]
    strong_ref: entity_core.StrongLinkedEntityRef[LeafEntity]


test_entity = MyEntity(
    weak_id=entity_core.FxtrID.from_tag_and_payload("!", b"one"),
    strong_id=entity_core.FxtrID.from_tag_and_payload("!", b"two"),
    weak_ref=entity_core.EntityRef(
        ref=entity_core.FxtrID.from_tag_and_payload("!", b"three"),
        converter=lambda x: x,
    ),
    strong_ref=entity_core.EntityRef(
        ref=entity_core.FxtrID.from_tag_and_payload("!", b"four"),
        converter=lambda x: x,
    ),
)


# ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
# %% ╱━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
