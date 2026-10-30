"""Container-only service impl for the streaming LLM prompt task.

Imported lazily by :func:`streaming_prompt_task_oci.run` (the
``fxtr.service`` entry point) so the heavy, container-only deps
(``anthropic`` SDK, ``fxtr_sandbox_rpc_adapter``) are pulled in only
when the service actually runs inside its OCI sandbox — never when the
package is imported agent-side for :data:`prompts.VIVID_DESCRIPTION_PROMPT`.

See the package ``__init__`` docstring for the connect config and the
streamed-progress message shape.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic
import fxtr_sandbox_rpc_adapter
from fxtr2.common.channels import FxtrChannelWriter, start_channel_and_infer_types
from fxtr2.conversion.converters.mashumaro_entities import EntityRef
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing as _U
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method
from fxtr_sandbox_types.protocols import NetworkHandleAuthorizerProto
from presentables import Note
from simple_tasks import NoteWithLinks, PromptTemplate
from streaming_tasks import STREAMING_TASK_TYPE_ID, StreamingTaskProto, TaskProgressTag

_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_TOKENS = 1024

#: Coalesce streamed deltas to at most one progress emit per this
#: interval (plus a final flush). Keeps the unbounded queue-mode cell
#: hub tame under a fast token stream (see the backpressure note on
#: :mod:`streaming_tasks`).
_MIN_EMIT_INTERVAL = 0.05


class _StreamingPromptTaskImpl(TypedServiceImpl[StreamingTaskProto]):
    def __init__(
        self,
        authorizer: ServiceRef[NetworkHandleAuthorizerProto],
        template: PromptTemplate,
    ) -> None:
        self._authorizer = authorizer
        self._template = template

    def _target_type_annotation(self) -> Any:
        return StreamingTaskProto

    def _service_type_id(self) -> FxtrID:
        return STREAMING_TASK_TYPE_ID

    @service_method
    async def run(
        self, input: FxtrID, channel: ServiceRef[FxtrChannelWriter[_U, _U, _U, _U]]
    ) -> FxtrID:
        note = await EntityRef(entity_id=input, target_type=Note | NoteWithLinks).load()
        prompt = self._template.template.format(title=note.title, body=note.body)
        # The task owns its cell channel: start it, stream partials, finish.
        scope = get_active_scope()  # dispatch scope; dies with the call
        tag = TaskProgressTag(task_label=self._template.label).store(scope)
        writer_ref = await start_channel_and_infer_types(channel, tag, None, scope=scope)
        writer = await writer_ref.resolve(scope)

        # The SDK rides a custom httpx client whose every connection tunnels
        # through the authorizer's transport side-channel. base_url's host is
        # cosmetic (the authorizer pins upstream) but must be plain http: TLS
        # is terminated host-side, the in-sandbox leg is the raw tunneled
        # byte stream. The api_key is a placeholder the host overwrites.
        http_client = fxtr_sandbox_rpc_adapter.get_client(
            self._authorizer, base_url="http://api.anthropic.com"
        )
        accumulated: list[str] = []
        last_emit = 0.0
        async with http_client:
            sdk = anthropic.AsyncAnthropic(
                api_key="key-injected-by-host-authorizer",
                base_url="http://api.anthropic.com",
                http_client=http_client,
            )
            async with sdk.messages.stream(
                model=_MODEL,
                max_tokens=_DEFAULT_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    accumulated.append(text)
                    now = time.monotonic()
                    if now - last_emit >= _MIN_EMIT_INTERVAL:
                        await writer.emit(
                            events=[{"kind": "partial", "text": "".join(accumulated)}]
                        )
                        last_emit = now
                final_message = await stream.get_final_message()

        reply = "".join(accumulated).strip()
        if not reply:
            # Fall back to the final message if the text stream was empty.
            parts = [b.text for b in final_message.content if b.type == "text"]
            reply = "\n".join(parts).strip()
        if not reply:
            raise ValueError("LLM returned an empty reply")
        # Final flush so the last partial reflects the complete reply.
        await writer.emit(events=[{"kind": "partial", "text": reply}])

        # First line -> title, the rest -> body (the format the example
        # prompts instruct the model to follow; see prompts.py).
        first, _, rest = reply.partition("\n")
        # Re-store the template in this dispatch scope: content addressing
        # makes it the same FxtrID as the agent-side original, and storing
        # here guarantees this scope holds a claim on it for the "prompt" link.
        tmpl_fid = self._template.store().entity_id
        out = NoteWithLinks(
            title=first.strip(),
            body=rest.strip(),
            links={"input": input, "prompt": tmpl_fid},
        ).store()
        # The task owns the cell channel: finish it with the output id.
        await writer.finish(out.entity_id)
        return out.entity_id


async def run(config: FxtrServiceData) -> ServiceRef[StreamingTaskProto]:
    """``fxtr.service`` entry point. ``config`` is a dict — see the
    package ``__init__`` docstring."""
    if not isinstance(config, dict):
        raise TypeError(
            "streaming_prompt_task_oci connect config must be a dict "
            '{"authorizer": <anthropic-authorizer ref>, '
            '"prompt_template": <PromptTemplate FxtrID>}'
        )
    authorizer = config["authorizer"]
    tmpl_fid = config["prompt_template"]
    if not isinstance(authorizer, ServiceRef):
        raise TypeError(
            f'config["authorizer"] must be a ServiceRef, got {type(authorizer).__name__}'
        )
    if not isinstance(tmpl_fid, FxtrID):
        raise TypeError(
            'config["prompt_template"] must be the FxtrID of a stored '
            f"PromptTemplate entity, got {type(tmpl_fid).__name__}"
        )
    template = await EntityRef(entity_id=tmpl_fid, target_type=PromptTemplate).load()
    if not template.template.strip():
        raise ValueError("prompt template is empty")
    return await get_active_scope().mount(
        _StreamingPromptTaskImpl(
            authorizer.replace_service_type(NetworkHandleAuthorizerProto), template
        )
    )
