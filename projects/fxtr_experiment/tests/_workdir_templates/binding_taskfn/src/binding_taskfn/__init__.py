"""Sandboxed task-fn fixture: a real `fxtr_experiment` task fn in-guest.

The entrypoint takes the standard binding connect request and mounts a
`fxtr_experiment_task.task_adapter.TaskFnAdapter` — i.e. the guest *is* the
``WorkflowTaskFnProto`` service the executor's prologue spawn resolves.
The task fn itself is a plain echo (read the wired input cell, return
its id), so the e2e proves exactly the boundary: grants + the run
capability ref bridge into the guest, the task-side view runtime runs
there, and the effectful ops proxy back to the host engine.

Built as `fxtr.services.binding_taskfn` (container kind — the decided
v0 runtime for sandboxed task fns) and exercised through the executor in
``tests/test_sandbox_e2e.py``.
"""

from __future__ import annotations

from fxtr2.data.entity_ref_scope import ServiceRef, get_active_scope
from fxtr2.services.service_cap_common import UnknownService
from fxtr_sandbox_types import BindingConnectRequest

from fxtr_experiment_task.task_adapter import task_fn


@task_fn
async def _echo(ctx, /, *, inp):
    """Read inputs[x='a'] and echo its entity id (the executor test's
    canonical shape)."""
    del ctx
    return await inp.read(x="a")


async def run(request: BindingConnectRequest) -> ServiceRef[UnknownService]:
    """fxtr.service entry point: serve the task fn for whatever key this
    binding was registered under (the request is echoed nowhere — a task
    fn's identity is its handle key; config/deps ride the request when
    a binding declares them)."""
    del request
    return await get_active_scope().mount(_echo)
