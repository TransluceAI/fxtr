"""A :class:`SimpleTaskProto` step: Note -> Note through an LLM with a
stored prompt template.

Spawned as ``fxtr.services.prompt_task`` (wasm, git-ref spawn).

**Connect config** is a plain dict (refs/ids ride the connect chain):

- ``"llm"`` — ServiceRef to a running ask-service (``ask(message) ->
  str``; either haiku service flavor works).
- ``"prompt_template"`` — the **FxtrID of a stored**
  :class:`simple_tasks.PromptTemplate` entity. Store it agent-side
  first, then pass ``tmpl.entity_id`` in the config. Its
  ``template`` field is ``str.format``-ed with ``{title}`` /
  ``{body}`` of the input note.

**Output.** The LLM's reply is stored as a
:class:`simple_tasks.NoteWithLinks`: the reply's **first line becomes
the title**, any remaining lines the body — so a template ending
"Reply with ONLY <the thing>, on a single line." produces a note
whose title *is* the thing, composing with title-keyed tasks
downstream (``wikipedia_task`` searches on the input note's title).
Every output carries two provenance links: ``"input"`` (the note the
reply was derived from) and ``"prompt"`` (the template entity), so a
result records both its data input and the instructions that
produced it. Multi-connect on one spawn works: each ``connect``
mints an independent task with its own template.
"""

from __future__ import annotations

from typing import Any, Protocol

from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method
from presentables import Note
from simple_tasks import (
    SIMPLE_TASK_TYPE_ID,
    NoteWithLinks,
    PromptTemplate,
    SimpleTaskProto,
)


@service_protocol
class _AskProto(Protocol):
    """Local re-declaration of the ask-services' surface (dispatch is
    by method name, so any ``ask(message) -> str`` ref re-types to
    this)."""

    async def ask(self, message: str) -> str: ...


class _PromptTaskImpl(TypedServiceImpl[SimpleTaskProto]):
    def __init__(self, llm_ref: ServiceRef[_AskProto], template: PromptTemplate) -> None:
        self._llm_ref = llm_ref
        self._template = template

    def _target_type_annotation(self) -> Any:
        return SimpleTaskProto

    def _service_type_id(self) -> FxtrID:
        return SIMPLE_TASK_TYPE_ID

    @service_method
    async def run(self, input: FxtrID) -> FxtrID:
        note = await EntityRef(entity_id=input, target_type=Note | NoteWithLinks).load()
        prompt = self._template.template.format(title=note.title, body=note.body)
        # Resolve in the dispatch scope so the proxy + its results are
        # cleaned up with this call.
        llm = await self._llm_ref.resolve(get_active_scope())
        reply = (await llm.ask(prompt)).strip()
        if not reply:
            raise ValueError("LLM returned an empty reply")
        first, _, rest = reply.partition("\n")
        # Re-store the template in this dispatch scope: content
        # addressing makes it the same FxtrID as the agent-side
        # original, and storing here guarantees this scope holds a
        # claim on it for the output's "prompt" link.
        tmpl_fid = self._template.store().entity_id
        out = NoteWithLinks(
            title=first.strip(),
            body=rest.strip(),
            links={"input": input, "prompt": tmpl_fid},
        ).store()
        return out.entity_id


async def run(config: FxtrServiceData) -> ServiceRef[SimpleTaskProto]:
    """``fxtr.service`` entry point. ``config`` is a dict — see the
    module docstring."""
    if not isinstance(config, dict):
        raise TypeError(
            "prompt_task connect config must be a dict "
            '{"llm": <ask-service ref>, "prompt_template": <PromptTemplate FxtrID>}'
        )
    llm = config["llm"]
    tmpl_fid = config["prompt_template"]
    if not isinstance(llm, ServiceRef):
        raise TypeError(f'config["llm"] must be a ServiceRef, got {type(llm).__name__}')
    if not isinstance(tmpl_fid, FxtrID):
        raise TypeError(
            'config["prompt_template"] must be the FxtrID of a stored '
            f"PromptTemplate entity, got {type(tmpl_fid).__name__}"
        )
    template = await EntityRef(entity_id=tmpl_fid, target_type=PromptTemplate).load()
    if not template.template.strip():
        raise ValueError("prompt template is empty")
    return await get_active_scope().mount(
        _PromptTaskImpl(llm.replace_service_type(_AskProto), template)
    )
