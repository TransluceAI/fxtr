"""Tests for the remote protocol's wire: the value codec, every frame's round trip, strict
decoding, and the error envelope."""

from __future__ import annotations

from typing import Any

import cbrrr
import pytest
from frozendict import frozendict

from fxtr.core import codec
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.schema import struct_of
from fxtr.runner.experimental.multiplexed import (
    AlreadyRegisteredError,
    Assigned,
    AttemptCancelled,
    Shutdown,
    StepSpec,
    WorkflowSpec,
)
from fxtr.runner.experimental.remote import wire
from fxtr.runner.experimental.remote.wire import (
    PROTOCOL_VERSION,
    HostError,
    ProtocolError,
    Refusal,
    WireError,
    WireValue,
    cid_of,
    decode_array,
    decode_frame,
    decode_value,
    encode_frame,
    encode_value,
    refusal_of,
)
from fxtr.runner.session import (
    AttemptClosedError,
    AttemptError,
    AttemptID,
    NodeFailedError,
    NodeToken,
    StepAssignment,
    WorkflowAssignment,
)

ATTEMPT = AttemptID("attempt-1")
PROMPTS = ArrayType.of(("[prompt: int]", str))
REFS = ArrayType.of(("[k: int]", EntityID))


def cid(n: int) -> bytes:
    return b"\x01\x71\x12\x20" + n.to_bytes(32, "big")


def T(n: int) -> NodeToken:
    return NodeToken(n)


# ---- values ----


def test_encode_value_round_trips() -> None:
    value = {"$type": "t", "refs": [EntityID(cid(1)), EntityID(cid(2))]}
    w = encode_value(value)
    assert w.encoded == codec.encode(value)
    assert decode_value(w) == value
    with pytest.raises(WireError, match="cannot be encoded"):
        encode_value({"$type": "t", "bad": object()})  # type: ignore[dict-item]


def test_cid_of_is_the_stored_entitys_cid() -> None:
    value = {"$type": "t", "ref": EntityID(cid(1)), "n": 3}
    assert cid_of(encode_value(value)) == codec.compute_cid(codec.encode(value))


def test_arrays_cross_as_their_entity_data() -> None:
    array = Array.from_items([((1,), EntityID(cid(1))), ((2,), EntityID(cid(2)))], REFS)
    w = WireValue(codec.encode(DEFAULT_CONVERTER.unstructure_entity(array)))
    assert decode_array(w) == array
    with pytest.raises(WireError, match="not an array"):
        decode_array(encode_value({"$type": "t"}))


def test_decode_rejects_malformed_bytes() -> None:
    with pytest.raises(WireError, match="Malformed"):
        decode_value(WireValue(b"\xff\xff"))
    with pytest.raises(WireError, match="Malformed"):
        decode_frame(b"\xff\xff")
    with pytest.raises(WireError, match="expected a map"):
        decode_frame(cbrrr.encode_dag_cbor([1, 2]))


# ---- frames ----

VALUE = encode_value({"$type": "t", "ref": EntityID(cid(1))})
ERROR = AttemptError("ValueError", "boom", "Traceback...")
RUN_FIELDS: dict[str, Any] = dict(
    attempt_id=ATTEMPT,
    token=T(4),
    name="sample",
    inputs=frozendict({"prompt": T(0), "n": T(1)}),
    map_over=("prompt",),
    join="inner",
    replicas=frozendict({"rep": (0, 1), "tag": ("a", "b")}),
    cache_key_prefix="cache/wf/sample",
    type=ArrayType.of(("[prompt: int, rep: int, tag: str, sample: int]", struct_of(x=int))),
)
WORKFLOW_ASSIGNMENT = WorkflowAssignment(
    attempt_id=ATTEMPT,
    workflow_name="wf",
    pathname="root/wf",
    cache_key_prefix="cache/wf",
    inputs=frozendict({"prompts": (T(0), PROMPTS), "n": (T(1), ArrayType.of(int))}),
)
STEP_ASSIGNMENT = StepAssignment(
    attempt_id=ATTEMPT,
    step_name="step",
    cache_key="cache/wf/step[k:1]",
    inputs=frozendict({"a": EntityID(cid(1)).bind_type(Array)}),
)

FRAMES: list[Any] = [
    wire.Hello(PROTOCOL_VERSION),
    wire.Register(1, (StepSpec("s"), WorkflowSpec("w")), 3),
    wire.Drain(2),
    wire.Observe(4, ATTEMPT, T(5)),
    wire.CompleteWorkflow(5, ATTEMPT, T(9)),
    wire.CompleteStep(6, ATTEMPT, VALUE),
    wire.Fail(7, ATTEMPT, ERROR),
    wire.Abandon(8, ATTEMPT, "worker stopped"),
    wire.StoreRequest(9, ATTEMPT, VALUE),
    wire.LoadRawData(10, ATTEMPT, EntityID(cid(2))),
    wire.EmitSourceArray(ATTEMPT, T(2), "prompts", VALUE),
    wire.EmitCombine(ATTEMPT, T(3), frozendict({"a": T(0), "b": T(1)}), "outer"),
    wire.EmitStack(ATTEMPT, T(3), (("y", T(0)), (2, T(1)), ("x", T(0))), "which"),
    wire.EmitRunStep(step_name="steps.sample", **RUN_FIELDS),
    wire.EmitRunChildWorkflow(workflow_name="workflows.score", **RUN_FIELDS),
    wire.LogMessage(ATTEMPT, "hello"),
    wire.Store(ATTEMPT, EntityID(cid_of(VALUE)), VALUE),
    wire.Reply(1, wire.NoResult()),
    wire.Reply(4, wire.ValueResult(VALUE)),
    wire.Reply(9, wire.IdResult(EntityID(cid(3)))),
    wire.Refused(4, Refusal("NodeFailedError", "node failed")),
    wire.Refused(8, Refusal("HostError", "what happened", "fxtr.SomeError")),
    Assigned(WORKFLOW_ASSIGNMENT),
    Assigned(STEP_ASSIGNMENT),
    AttemptCancelled(ATTEMPT, "superseded"),
    Shutdown("drained"),
]


@pytest.mark.parametrize("frame", FRAMES, ids=lambda f: type(f).__name__)
def test_every_frame_round_trips(frame: Any) -> None:
    back = decode_frame(encode_frame(frame))
    assert back == frame
    assert type(back) is type(frame)


def test_step_inputs_keep_their_loader() -> None:
    back = decode_frame(encode_frame(Assigned(STEP_ASSIGNMENT)))
    assert isinstance(back, Assigned) and isinstance(back.assignment, StepAssignment)
    ref = back.assignment.inputs["a"]
    assert ref.entity_id == EntityID(cid(1))
    assert ref.loader is STEP_ASSIGNMENT.inputs["a"].loader
    back = decode_frame(encode_frame(Assigned(WORKFLOW_ASSIGNMENT)))
    assert isinstance(back, Assigned) and isinstance(back.assignment, WorkflowAssignment)
    assert isinstance(back.assignment.inputs, frozendict)


def test_stack_inputs_keep_int_and_str_keys_and_order() -> None:
    frame = wire.EmitStack(ATTEMPT, T(3), (("y", T(0)), (2, T(1)), ("x", T(0))), "which")
    back = decode_frame(encode_frame(frame))
    assert isinstance(back, wire.EmitStack)
    assert back.inputs == (("y", 0), (2, 1), ("x", 0))
    assert [type(k) for k, _ in back.inputs] == [str, int, str]


def test_decoding_is_strict() -> None:
    def frame_bytes(**fields: Any) -> bytes:
        return cbrrr.encode_dag_cbor(fields)

    with pytest.raises(WireError, match="Unknown frame kind 'nope'"):
        decode_frame(frame_bytes(kind="nope"))
    with pytest.raises(WireError, match="extra fields found"):
        decode_frame(frame_bytes(kind="drain", request_id=2, extra=1))
    with pytest.raises(WireError, match="required field missing"):
        decode_frame(frame_bytes(kind="drain"))
    with pytest.raises(WireError, match=r"expected int @ \$\.request_id"):
        decode_frame(frame_bytes(kind="drain", request_id="2"))
    with pytest.raises(WireError, match=r"expected int @ \$\.request_id"):
        decode_frame(frame_bytes(kind="drain", request_id=True))
    with pytest.raises(WireError, match=r"\$\.join"):
        decode_frame(
            frame_bytes(kind="emit_combine", attempt_id="a", token=1, inputs={}, join="left")
        )
    with pytest.raises(WireError, match=r"\$\.functions\[0\]"):
        decode_frame(
            frame_bytes(kind="register", request_id=1, functions=[{"kind": "x"}], max_concurrency=1)
        )
    with pytest.raises(WireError, match=r"\$\.result\.id"):
        decode_frame(frame_bytes(kind="reply", request_id=1, result={"kind": "id_result", "id": 5}))
    for bad_id in ([1, 2], [b"x", 1], 5, "x"):
        with pytest.raises(WireError, match=r"\$\.entity_id"):
            decode_frame(
                frame_bytes(kind="load_raw_data", request_id=1, attempt_id="a", entity_id=bad_id)
            )
    with pytest.raises(WireError, match=r"\$\.inputs"):
        decode_frame(
            frame_bytes(kind="emit_stack", attempt_id="a", token=1, inputs=[[True, 2]], dim="d")
        )


def test_request_id_of() -> None:
    assert wire.request_id_of(wire.Observe(4, ATTEMPT, T(5))) == 4
    assert wire.request_id_of(wire.LogMessage(ATTEMPT, "x")) is None
    assert wire.request_id_of(Shutdown("bye")) is None


# ---- the error envelope ----


def test_refusals_name_protocol_errors_and_wrap_others() -> None:
    for exc in [
        NodeFailedError("node failed"),
        AttemptClosedError("closed"),
        AlreadyRegisteredError("twice"),
        ProtocolError("bad handle"),
    ]:
        refusal = refusal_of(exc)
        assert refusal.type_name is None
        back = decode_frame(encode_frame(wire.Refused(1, refusal)))
        assert isinstance(back, wire.Refused)
        raised = back.error.exception()
        assert type(raised) is type(exc) and str(raised) == str(exc)

    class Surprising(Exception):
        pass

    refusal = refusal_of(Surprising("uh oh"))
    assert refusal.type == "HostError" and refusal.type_name is not None
    assert refusal.type_name.endswith("Surprising")
    raised = refusal.exception()
    assert isinstance(raised, HostError) and raised.args[0] == "uh oh"
    assert str(raised).endswith("Surprising: uh oh")
    # A HostError re-encoded keeps the original's name.
    assert refusal_of(raised) == refusal
    with pytest.raises(WireError, match="unknown error class"):
        Refusal("Nope", "").exception()
