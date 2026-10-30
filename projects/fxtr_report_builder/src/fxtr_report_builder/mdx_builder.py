"""Compile MDX source to a :data:`FrozenFxtrCbor` tree via a TS subprocess.

The TS subprocess (``fxtr-report-builder-cli``) is a self-contained
bundle that runs the MDX compiler against a custom JSX runtime,
flattens the result to ``[type, props, children]`` tuples, and
re-encodes the tree as CBOR.

Ref-typed inputs cross the Python/TS boundary as raw CBOR bytes
with tag numbers 33000 / 33001 / 33002 (encoded by
``fxtr_cbor_dumps``). The TS side never interprets these tags —
they round-trip verbatim through MDX expansion as opaque values
and land back in the output CBOR. Python decodes the response and
the same :class:`RawWeakFxtrID` instances reappear at the same
positions in the tree.

**Provenance preservation.** :func:`fxtr_cbor_dumps` only carries
the bare ``_raw_data_id`` of each FxtrID across the wire, so
naively shipping a layered ``(data, prov)`` input would drop the
provenance half. To round-trip provenance we mirror the
``persistence.py`` pattern: every input FxtrID is projected to its
bare provenance form before encoding (``extract_provenance_parent``
for already-layered IDs; ``create_u_node`` for bare data IDs), and
every FxtrID in the decoded response tree is re-layered via
``embed_as_provenance_parent``. The active :class:`FxtrScope` is
captured at the call site via :func:`get_active_scope` and threaded
through both halves. Bare provenance-tag inputs (``n``/``c``/``p``/
``u`` with no provenance parent) are rejected as ambiguous — same
reasoning as the preserve-prov persistence cap.
"""

from __future__ import annotations

import base64
import json
import subprocess
import typing
from collections.abc import Mapping, Sequence

import anyio
from fxtr2.data.entity_core import (
    FrozenFxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawStrongFxtrID,
    RawWeakFxtrID,
    freeze_fxtr_cbor,
    fxtr_cbor_dumps,
    fxtr_cbor_loads,
)
from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope, get_active_scope

from fxtr_report_builder._ts_dist_path import MdxSubprocessError, resolve_report_cli_command
from fxtr_report_builder.entities import RootReport, SubReport


_PROVENANCE_NODE_TAGS = frozenset({"n", "c", "p", "u"})


def _project_id_for_subprocess(scope: FxtrScope, entity: FxtrID) -> FxtrID:
    """Return the bare provenance form of ``entity`` for shipping.

    - Layered ``(data, prov)``: returns the bare prov half via
      :meth:`FxtrScope.extract_provenance_parent`.
    - Bare data-tag input: lifts to ``u(data)`` via
      :meth:`FxtrScope.create_u_node`.
    - Bare provenance-tag input with no ``_provenance_parent``:
      rejected as ambiguous — see ``persistence._layer_for_storage``
      for the same rationale.
    """
    if entity._provenance_parent is not None:
        return scope.extract_provenance_parent(entity)
    tag = entity.get_tag()
    if tag in _PROVENANCE_NODE_TAGS:
        raise ValueError(
            f"build_mdx_tree input {entity} is a bare {tag!r}-tag (provenance) "
            "node with no provenance parent. Either layer it via "
            "embed_as_provenance_parent first, or wrap it via create_u_node, "
            "then re-pass it as an input."
        )
    return scope.create_u_node(entity)


type _RawId = RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID


def _project_raw_id_for_subprocess(scope: FxtrScope, raw: _RawId) -> _RawId:
    """Re-wrap the projected inner FxtrID in the same raw wrapper class."""
    projected = _project_id_for_subprocess(scope, raw.inner_id)
    return type(raw)(inner_id=projected)


def _relayer_decoded_cbor(scope: FxtrScope, value: typing.Any) -> typing.Any:
    """Walk decoded CBOR and re-layer every raw ID via
    :meth:`FxtrScope.embed_as_provenance_parent`.

    The subprocess ships back the same bare provenance nodes we
    sent in (it never inspects the CBOR tags). The decode hook
    produces ``RawWeakFxtrID(inner_id=FxtrID(raw_data_id=..., _provenance_parent=None))``
    — i.e. bare. We re-embed so the layered ``(data, prov)`` form
    flows back to the caller. For inputs that were bare data
    (lifted to ``u(data)`` on the way out), this yields
    ``(data, u(data))``: identical at the data level, with the
    inserted u-node carrying the trivial provenance.
    """
    match value:
        case RawWeakFxtrID() | RawStrongFxtrID() | RawFxtrEntityMarkerID():
            layered = scope.embed_as_provenance_parent(value.inner_id)
            return type(value)(inner_id=layered)
        case list():
            return [_relayer_decoded_cbor(scope, v) for v in value]
        case dict():
            return {k: _relayer_decoded_cbor(scope, v) for k, v in value.items()}
        case _:
            return value


class MdxBuildError(RuntimeError):
    """The TS subprocess reported a typed compile failure.

    Distinct from :class:`MdxSubprocessError`: this is a user-level
    error (MDX syntax error, invalid input, etc.) communicated
    through the ``{ok: false, error: str}`` response envelope.
    """


_DEFAULT_PASSTHROUGH_COMPONENTS: tuple[str, ...] = ("RenderFigure", "RenderLink")


async def build_mdx_tree(
    source: str,
    *,
    inputs: Mapping[str, RawWeakFxtrID | RawStrongFxtrID] | None = None,
    passthrough_components: Sequence[str] = _DEFAULT_PASSTHROUGH_COMPONENTS,
    cli_path: str | typing.Any | None = None,
) -> FrozenFxtrCbor:
    """Compile MDX ``source`` to a :data:`FrozenFxtrCbor` tree.

    ``inputs`` maps a name (referenced inside the MDX source as
    ``props.fxtr_inputs.<name>``) to a raw ref. Each ref is shipped
    to the TS subprocess as opaque CBOR-tagged bytes; it survives
    MDX expansion verbatim and lands back in the returned tree.

    ``passthrough_components`` names JSX element types that flatten
    to ``[name, props, []]`` tuples. The TS subprocess additionally
    assigns each ``RenderFigure`` node a stable ``figureKey``
    counter.

    ``cli_path`` optionally names a self-contained CLI executable (the
    nix-built ``reportBuilderCli`` wrapper); see
    :func:`resolve_report_cli_command` for the arg → env → dev order.
    """
    inputs_dict: dict[str, RawWeakFxtrID | RawStrongFxtrID] = dict(inputs or {})
    scope = get_active_scope()

    encoded_inputs: dict[str, str] = {}
    for name, ref in inputs_dict.items():
        projected = _project_raw_id_for_subprocess(scope, ref)
        cbor_bytes = fxtr_cbor_dumps(projected)
        encoded_inputs[name] = base64.b64encode(cbor_bytes).decode("ascii")

    request_envelope = {
        "source": source,
        "inputs": encoded_inputs,
        "passthroughComponents": list(passthrough_components),
    }
    request_payload = json.dumps(request_envelope).encode("utf-8")

    command = resolve_report_cli_command(cli_path)
    response_text, stderr_text = await _run_subprocess(command, request_payload)

    try:
        response_envelope = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise MdxSubprocessError(
            f"TS subprocess produced malformed JSON: {e}",
            stderr=stderr_text,
        ) from e

    if not isinstance(response_envelope, dict):
        raise MdxSubprocessError(
            f"TS subprocess response is not an object: {response_envelope!r}",
            stderr=stderr_text,
        )

    ok = response_envelope.get("ok")
    if ok is True:
        tree_cbor_b64 = response_envelope.get("treeCbor")
        if not isinstance(tree_cbor_b64, str):
            raise MdxSubprocessError(
                f"Success response missing treeCbor: {response_envelope!r}",
                stderr=stderr_text,
            )
        try:
            tree_bytes = base64.b64decode(tree_cbor_b64)
        except (ValueError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
            raise MdxSubprocessError(
                f"Failed to base64-decode treeCbor: {e}",
                stderr=stderr_text,
            ) from e
        tree = fxtr_cbor_loads(tree_bytes)
        relayered = _relayer_decoded_cbor(scope, tree)
        return freeze_fxtr_cbor(typing.cast("typing.Any", relayered))

    if ok is False:
        error = response_envelope.get("error", "<no error message>")
        raise MdxBuildError(str(error))

    raise MdxSubprocessError(
        f"TS subprocess response has unexpected `ok` field: {response_envelope!r}",
        stderr=stderr_text,
    )


type _HighLevelInput = EntityRef[typing.Any] | FxtrID | RawWeakFxtrID | RawStrongFxtrID


def _to_raw_weak(value: _HighLevelInput) -> RawWeakFxtrID:
    """Normalize the high-level input shape to a `RawWeakFxtrID`.

    Accepts:
    - :class:`RawWeakFxtrID` / :class:`RawStrongFxtrID` — used as-is
      (strong gets unwrapped to weak since the MDX context is just
      a tree, not a strong-link bearer).
    - :class:`EntityRef` — extracts `.entity_id`.
    - :class:`FxtrID` — wraps directly.
    """
    if isinstance(value, RawWeakFxtrID):
        return value
    if isinstance(value, RawStrongFxtrID):
        return RawWeakFxtrID(inner_id=value.inner_id)
    if isinstance(value, EntityRef):
        return RawWeakFxtrID(inner_id=value.entity_id)
    if isinstance(value, FxtrID):
        return RawWeakFxtrID(inner_id=value)
    raise TypeError(
        f"build_*_report inputs must be EntityRef / FxtrID / RawWeakFxtrID / "
        f"RawStrongFxtrID; got {type(value).__name__}"
    )


async def build_root_report(
    *,
    title: str,
    summary: str,
    source: str,
    inputs: Mapping[str, _HighLevelInput] | None = None,
    cli_path: str | typing.Any | None = None,
) -> RootReport:
    """High-level helper: compile MDX + construct a :class:`RootReport`.

    Hardcodes the ``RenderFigure`` / ``RenderLink`` passthrough
    components. Accepts ``inputs`` in the broad shape
    (:class:`EntityRef` / :class:`FxtrID` / :class:`RawWeakFxtrID`
    / :class:`RawStrongFxtrID`) for ergonomic call sites; converts
    internally before shipping to the subprocess.
    """
    normalized = {name: _to_raw_weak(ref) for name, ref in (inputs or {}).items()}
    contents = await build_mdx_tree(source, inputs=normalized, cli_path=cli_path)
    return RootReport(title=title, summary=summary, contents=contents)


async def build_sub_report(
    *,
    label: str,
    overview_source: str,
    detail_source: str,
    overview_inputs: Mapping[str, _HighLevelInput] | None = None,
    detail_inputs: Mapping[str, _HighLevelInput] | None = None,
    cli_path: str | typing.Any | None = None,
) -> SubReport:
    """High-level helper: compile two MDX bodies + construct a :class:`SubReport`.

    ``overview`` and ``detail`` are compiled separately; inputs are
    per-field so the two bodies can reference different refs.
    """
    overview = await build_mdx_tree(
        overview_source,
        inputs={name: _to_raw_weak(ref) for name, ref in (overview_inputs or {}).items()},
        cli_path=cli_path,
    )
    detail = await build_mdx_tree(
        detail_source,
        inputs={name: _to_raw_weak(ref) for name, ref in (detail_inputs or {}).items()},
        cli_path=cli_path,
    )
    return SubReport(label=label, overview=overview, detail=detail)


async def _run_subprocess(command: list[str], request_payload: bytes) -> tuple[str, str]:
    """Run the CLI ``command`` argv (from :func:`resolve_report_cli_command`)
    with the given stdin payload.

    Returns ``(stdout_text, stderr_text)`` on a clean (exit-code 0)
    process. Raises :class:`MdxSubprocessError` if the process exits
    non-zero — the message includes the captured stderr and exit
    code so debugging is one read of the exception away.
    """
    async with await anyio.open_process(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        stdout_bytes: bytes = b""
        stderr_bytes: bytes = b""

        async def _write_stdin() -> None:
            assert process.stdin is not None
            try:
                await process.stdin.send(request_payload)
            finally:
                await process.stdin.aclose()

        async def _read_stdout() -> None:
            nonlocal stdout_bytes
            assert process.stdout is not None
            chunks: list[bytes] = []
            async for chunk in process.stdout:
                chunks.append(chunk)
            stdout_bytes = b"".join(chunks)

        async def _read_stderr() -> None:
            nonlocal stderr_bytes
            assert process.stderr is not None
            chunks: list[bytes] = []
            async for chunk in process.stderr:
                chunks.append(chunk)
            stderr_bytes = b"".join(chunks)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_write_stdin)
            tg.start_soon(_read_stdout)
            tg.start_soon(_read_stderr)

        await process.wait()

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    if process.returncode != 0:
        raise MdxSubprocessError(
            f"TS subprocess exited with code {process.returncode}",
            stderr=stderr_text,
            returncode=process.returncode,
        )

    return stdout_text, stderr_text
