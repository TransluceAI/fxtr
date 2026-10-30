import contextlib


@contextlib.contextmanager
def exc_note(s):
    try:
        yield
    except Exception as e:
        e.add_note(s)
        raise e


def set_up_treescope():
    import treescope

    treescope.basic_interactive_setup()

    import msgspec

    def render_msgspec_struct(node, path, subtree_renderer):
        fields = msgspec.structs.fields(type(node))
        return treescope.repr_lib.render_object_constructor(
            type(node),
            attributes={field.name: getattr(node, field.name) for field in fields},
            subtree_renderer=subtree_renderer,
            path=path,
        )

    treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[msgspec.Struct] = render_msgspec_struct

    # import tensordict

    # def render_tensordict(node, path, subtree_renderer):
    #     if type(node).__repr__ is not tensordict.TensorDictBase.__repr__:
    #         return NotImplemented
    #     attrs = {
    #         "fields": dict(node),
    #         "batch_size": node.batch_size,
    #         "device": node.device,
    #         "is_shared": node.is_shared(),
    #     }

    #     return treescope.repr_lib.render_object_constructor(
    #         type(node),
    #         attributes=attrs,
    #         subtree_renderer=subtree_renderer,
    #         path=path,
    #     )

    # treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[tensordict.TensorDictBase] = (
    #     render_tensordict
    # )

    # import torch.distributed.tensor

    # def render_dtensor(node, path, subtree_renderer):
    #     return treescope.repr_lib.render_object_constructor(
    #         type(node),
    #         attributes={
    #             "_local": node.to_local(),
    #             "device_mesh": node.device_mesh,
    #             "placements": node.placements,
    #         },
    #         subtree_renderer=subtree_renderer,
    #         path=path,
    #     )

    # treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[torch.distributed.tensor.DTensor] = (
    #     render_dtensor
    # )

    import collections

    treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[collections.deque] = (
        treescope.repr_lib.handle_custom_listlike
    )


def rendercapture(x):
    import IPython.display
    import treescope

    treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[msgspec.Struct] = render_msgspec_struct
    treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[tensordict.TensorDictBase] = (
        render_tensordict
    )
    # treescope.type_registries.TREESCOPE_HANDLER_REGISTRY[torch.distributed.tensor.DTensor] = (
    #     render_dtensor
    # )

    with treescope.active_autovisualizer.set_scoped(treescope.ArrayAutovisualizer()):
        return IPython.display.HTML(treescope.active_renderer.get().to_html(x))


import asyncio
from typing import Callable

import anyio


async def context_task[
    T
](
    cmgr_ctor: Callable[[], contextlib.AbstractAsyncContextManager[T]],
) -> tuple[
    T, asyncio.Task, anyio.Event
]:

    send, recv = anyio.create_memory_object_stream[T](0)
    shutdown_event = anyio.Event()

    async def _run():
        async with send:
            async with cmgr_ctor() as cmgr:
                await send.send(cmgr)
                await shutdown_event.wait()

    task = asyncio.create_task(_run())
    try:
        val = await recv.receive()
        return val, task, shutdown_event
    except anyio.EndOfStream:
        exc = task.exception()
        if exc is not None:
            raise exc
        else:
            raise
