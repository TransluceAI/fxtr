"""A throwaway entity type exercised by the mkEntityFromPython test.

``sample_type_id`` is the ``FxtrID`` minted by ``define_type`` — an entity
object (the ``FxtrBasicType`` type entity) that ``lib.fxtr.mkEntityFromPython``
imports by name and writes into a layer.
"""

from __future__ import annotations

import uuid

from fxtr2.data.system_entities import define_type

PY_TESTING_NS = uuid.UUID("b2e7d1a0-4c63-4f8e-9a21-7d5e3c0f9b84")

sample_type_id = define_type(PY_TESTING_NS, "py_testing_sample")
