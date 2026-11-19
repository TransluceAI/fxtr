"""A request decoded from a dialect needs no adaptation to go back out in it.

Every path of each dialect's request schema, decoded, adapted by the same dialect's codec and
encoded: adaptation changes nothing and records no loss, and the encoder takes the result. The
exception is what the provider enforces of a schema (`contract_adaptation.ENFORCEMENT_CODES`): a
request in the provider's own dialect meets those limits too, and the audit's witnesses are
minimal schemas that often do. This is
what keeps the passthrough reconstruction check (which encodes without adapting) meaningful, and
it holds for requests whose reasoning came from the model they are sent to: another model's signed
reasoning is a recorded change even within one dialect.
"""

import functools
import json

import pytest

from behaviors.implementations.dialects.contract_adaptation import ENFORCEMENT_CODES
from behaviors.types.dialect import Dialect
from behaviors.types.response import ModelRequest

from .audit import build_witness, schema_paths
from .dialects import AUDITS


@functools.cache
def _decoded(dialect: Dialect) -> tuple[tuple[str, str, ModelRequest], ...]:
    spec = AUDITS[dialect]()
    out: list[tuple[str, str, ModelRequest]] = []
    for path in schema_paths(spec.schema):
        if path.unwalkable is not None:
            continue
        try:
            witness = build_witness(spec.schema, path)
            body = witness.body
            headers = spec.adjust(body, witness.pointer)[0] if spec.adjust is not None else ()
            decoded = spec.codec.decode_request(spec.target, headers, json.dumps(body).encode())
        except Exception:  # noqa: BLE001 - not a request the codec reads; the audit says so
            continue
        out.append((path.label, decoded.model or "probe", decoded.request))
    return tuple(out)


@pytest.mark.parametrize("dialect", sorted(AUDITS))
def test_a_decoded_request_goes_back_out_unadapted(dialect: Dialect) -> None:
    codec = AUDITS[dialect]().codec
    decoded = _decoded(dialect)
    assert decoded
    problems: list[str] = []
    for label, model, request in decoded:
        adapted = codec.adapt_request(model, request)
        other = [loss.code for loss in adapted.losses if loss.code not in ENFORCEMENT_CODES]
        if other or (adapted.request != request and not adapted.losses):
            problems.append(f"{label}: {other}")
            continue
        codec.encode_request(model, adapted.request, streaming=True)
    assert not problems, f"{len(problems)} adapted path(s):\n" + "\n".join(problems[:20])
