"""Streaming LLM prompt task (container/OCI flavor).

A :class:`streaming_tasks.StreamingTaskProto` that runs a prompt
template over an input note through Claude and emits the reply *as it
streams* — chunk by chunk — through the per-cell channel whose writer
the streaming scheduler hands it (the task owns it: start / emit /
finish). The streamed reply is then stored as a
:class:`simple_tasks.NoteWithLinks` (first line -> title, rest -> body),
exactly like the non-streaming :mod:`prompt_task`.

Built as ``fxtr.services.streaming_prompt_task_oci`` (container, via
``mkContainerService``) and spawned with ``NixContainerServiceDefn``.
OCI on purpose: the image runs real Linux CPython, so the actual
``anthropic`` SDK is an ordinary dependency and its native streaming
(``messages.stream(...)``) handles the SSE wire for us — the wasm flavor
couldn't (no wasm wheels for the SDK's native deps). Network flows
through the connect-time ``anthropic-authorizer`` tunnel; this code
never sees the API key (the host proxy injects ``x-api-key``).

**Connect config** is a dict (refs/ids ride the connect chain):

- ``"authorizer"`` — ServiceRef to the workstation's
  ``anthropic-authorizer`` (a ``NetworkHandleAuthorizerProto``).
- ``"prompt_template"`` — the FxtrID of a stored
  :class:`simple_tasks.PromptTemplate`; its ``template`` field is
  ``str.format``-ed with ``{title}`` / ``{body}`` of the input note. For
  a ready-made one, store :data:`VIVID_DESCRIPTION_PROMPT` (see
  :mod:`streaming_prompt_task_oci.prompts`) and pass its ``entity_id``::

      from streaming_prompt_task_oci import VIVID_DESCRIPTION_PROMPT
      tmpl = VIVID_DESCRIPTION_PROMPT.store()
      # ... connect(..., {"authorizer": authz, "prompt_template": tmpl.entity_id})

Progress messages use the shared streaming vocabulary
(:mod:`streaming_tasks`): ``{"kind": "partial", "text": <reply so far>}``
— each message carries the full text so far, so the streaming-scheduler
grid renders the live token stream with no frontend change. Emissions
are lightly coalesced (at most one per ``_MIN_EMIT_INTERVAL`` seconds,
plus a final flush) to keep the queue-mode cell hub tame.

Every output carries ``"input"`` + ``"prompt"`` provenance links.
Multi-connect on one spawn works: each ``connect`` mints an independent
task with its own template.

The service impl lives in :mod:`streaming_prompt_task_oci.service` and is
imported lazily by :func:`run`, so importing this package agent-side
(e.g. for :data:`VIVID_DESCRIPTION_PROMPT`) does not require the
container-only deps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .prompts import VIVID_DESCRIPTION_PROMPT

if TYPE_CHECKING:
    from fxtr2.data.entity_ref_scope import ServiceRef
    from fxtr2.services.service_cap_common import FxtrServiceData
    from streaming_tasks import StreamingTaskProto

__all__ = ["VIVID_DESCRIPTION_PROMPT", "run"]


async def run(config: "FxtrServiceData") -> "ServiceRef[StreamingTaskProto]":
    """``fxtr.service`` entry point. Lazily imports the heavy service
    impl (anthropic SDK etc.) so this package stays importable agent-side
    without the container-only deps. See the module docstring for the
    connect config."""
    from .service import run as _run

    return await _run(config)
