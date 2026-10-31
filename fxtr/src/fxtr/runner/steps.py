"""Step requests: what a ``run_step`` node asks for at one joint key, and the cache key it asks
for it under.

A :class:`~fxtr.runner.nodes.RunStep` node describes a step over *nodes*; once its inputs have
values and it is expanded (:func:`~fxtr.runner.nodes.expand`), each spawn is a concrete call: this
step, on these exact input arrays. That call is a :class:`StepRequest`, an entity
whose cid is the call's **fingerprint**: everything about the call, including the sliced inputs by
reference, and nothing about who asked. Two requests with equal fingerprints are the same call and
may share a result; a cache key already holding a different fingerprint is a conflict.

The request deliberately omits the declared result type: the requesting node declares that, and a
result, cached or fresh, must be assignable to it where it is consumed. It also carries nothing
about the step's code: the step's name is its whole identity here, so a result stays matched
across code changes until its cache key is cleared; which code produced a result is recorded
separately, on the attempt that ran it.

The **cache key** is the address a request's result is saved under and replayed from: the run
node's cache key prefix with the mapped key appended (:func:`cache_key`). Two requests at one key
must agree; equal requests at different keys are different calls, which is how a key breaks the
symmetry of a nondeterministic step, like a seed.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from frozendict import frozendict

from fxtr.core.entities import BoundID
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, KeyValue
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.workflows import append_mapped_key
from fxtr.runner.nodes import RunStep


@dataclasses.dataclass(frozen=True, kw_only=True)
class StepRequest(DataclassEntity, fxtr_type="org.transluce.fxtr.runner.StepRequest"):
    """One concrete call of a step: the fingerprint entity.

    Attributes:
        step_name: The step (:attr:`~fxtr.experiment.steps.Step.name`).
        inputs: The input arrays by the step's argument names, as references to stored arrays
            (the run node's inputs sliced to the spawn's joint key).
    """

    step_name: str
    inputs: frozendict[str, BoundID[Array[Any]]]


def request_for(node: RunStep, inputs: Mapping[str, BoundID[Array[Any]]]) -> StepRequest:
    """The request one spawn of a run node makes, given its sliced inputs stored as arrays."""
    return StepRequest(step_name=node.step_name, inputs=frozendict(inputs))


def fingerprint_of(request: StepRequest) -> EntityID:
    """The request's fingerprint: its :meth:`~fxtr.core.entities.Entity.content_id`, the cid it
    has as a stored entity. The runner's name for that id, nothing more."""
    return request.content_id()


def cache_key(node: RunStep, mapped: Mapping[str, KeyValue]) -> str:
    """The cache key of one spawn of a run node: the node's prefix with the mapped key appended
    (see :func:`~fxtr.experiment.workflows.append_mapped_key`)."""
    return append_mapped_key(node.cache_key_prefix, mapped)


__all__ = ["StepRequest", "cache_key", "fingerprint_of", "request_for"]
