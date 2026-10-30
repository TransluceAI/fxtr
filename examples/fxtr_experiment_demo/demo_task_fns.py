"""Host-side task fns for the experiment demo's scenarios.

Plain :mod:`fxtr_experiment` task fns (any async ``(ctx, /, **args)``
callable) operating on :class:`experiment_notes.ExpNote` values, so
every result is readable in the browser. The runner mount
(:mod:`experiment_runner`) auto-binds the :data:`STANDARD_FNS` at mount
time; :data:`EXTRA_FN_NAME` names the deliberately *unbound* fn behind
the driver's handle-boundness scenario.

Inside a task fn the active scope is the run scope set by the framework
(the sanctioned user-code style), so bare ``.store()`` / ``.load()``
are correct here — these fns are demo *user code*, not library code.
"""

from __future__ import annotations

import uuid

import anyio
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.system_entities import define_type
from fxtr_experiment_task import model as m
from fxtr_experiment_task.view_runtime import step_fn

from experiment_notes import ExpNote

#: Stable namespace UUID for the demo's step type ids.
_DEMO_STEPS_NS = uuid.UUID("f0b7a4d2-61c9-4e35-9a88-3c5d7e0f1a26")


async def _load_note(view) -> ExpNote:
    """Read the (fully bound) view and load its ExpNote."""
    ref: FxtrID = await view.read()
    return await EntityRef(ref, ExpNote).load()


class UppercaseNote:
    """Pipeline stage 1: uppercase the input note's body."""

    async def __call__(self, ctx, /, *, note):
        n = await _load_note(note)
        return ExpNote(title=f"{n.title} (upper)", body=n.body.upper()).store().entity_id


class CombineNotes:
    """Pipeline stage 2: combine the original input with stage 1's output."""

    async def __call__(self, ctx, /, *, original, shouted):
        a = await _load_note(original)
        b = await _load_note(shouted)
        return (
            ExpNote(
                title=f"{a.title} (combined)",
                body=f"{a.body}\n---\n{b.body}",
            )
            .store()
            .entity_id
        )


@step_fn(type_id=define_type(_DEMO_STEPS_NS, "slow_step"))
async def slow_step(ctx, *, i: int, seconds: float, text: str):
    """One durable unit of the slow task: sleep, then return a fragment.

    The sleep (and the ``log()`` line — watch it stream into the cell
    drawer's run log) runs on the miss path only: a checkpoint replay
    (resume after an interruption, or the safety pass) serves the
    recorded result without re-invoking this fn.
    """
    await ctx.log(f"step {i}: sleeping {seconds}s before emitting a fragment")
    await anyio.sleep(seconds)
    return f"[step {i}: {text}]"


class SlowSteps:
    """The kill/resume scenario: N checkpointed steps, each ``seconds`` long.

    Interrupt the process mid-run and relaunch: the generation shows
    ``interrupted``, and a re-demand resumes the SAME generation with
    completed steps served from their durable checkpoints.
    """

    def __init__(self, *, steps: int = 6, seconds: float = 5.0) -> None:
        self.steps = steps
        self.seconds = seconds

    async def __call__(self, ctx, /, *, note):
        n = await _load_note(note)
        # An outer-level (task) log line — no step identity, distinct from
        # the per-step `ctx.log()` in `slow_step`; watch both stream into
        # the drawer's run log.
        await ctx.log(f"running {self.steps} slow steps")
        ckpt = await ctx.own_internals().open_checkpoint("steps", axes=(m.AxisSpec("i", "int"),))
        fragments = []
        for i in range(self.steps):
            fragments.append(
                await ckpt.run_step(
                    slow_step,
                    args={"i": i, "seconds": self.seconds, "text": n.body},
                    coords={"i": i},
                )
            )
        return ExpNote(title=f"{n.title} (slow)", body="\n".join(fragments)).store().entity_id


class FlakyNote:
    """The retry scenario: fails the first ``failures`` invocations.

    The planted failures burn down across *invocations*, so the first
    demand's run errors (a per-generation failure record, status
    ``errored``) and the retry's run — plus its safety replay — succeed
    deterministically with input-derived results.
    """

    def __init__(self, *, failures: int = 1) -> None:
        self.failures_remaining = failures

    async def __call__(self, ctx, /, *, note):
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("flaky task fn: planted failure (retry me)")
        n = await _load_note(note)
        return ExpNote(title=f"{n.title} (flaky)", body=f"eventually: {n.body}").store().entity_id


class PlanNotes:
    """The run-time-creation scenario: this task creates grids while it runs.

    Inside its own internals it opens a ``draft`` data grid (writes a
    derived note) and a nested ``polish`` *task* grid wired — through
    the granted ``fns`` handle view — to the ``uppercase`` fn over that
    draft, then reads the child task's output. Browsing the owning
    cell's internals in the dashboard shows both created grids,
    including a task grid whose own generation ran inside the parent's.
    """

    async def __call__(self, ctx, /, *, note, fns):
        n = await _load_note(note)
        internals = ctx.own_internals()
        drafts = await internals.open_data("draft", ())
        draft = ExpNote(title=f"{n.title} (draft)", body=f"draft of: {n.body}")
        await drafts.write(draft.store().entity_id)
        polish = await internals.open_task(
            "polish",
            axes=(),
            task_fn=fns.at(fn="uppercase"),
            args={"note": drafts.read_only()},
        )
        polished = await EntityRef(await polish.output_entity().read(), ExpNote).load()
        return ExpNote(title=f"{n.title} (planned)", body=polished.body).store().entity_id


class ReverseNote:
    """The boundness scenario's fn — deliberately NOT auto-bound."""

    async def __call__(self, ctx, /, *, note):
        n = await _load_note(note)
        return ExpNote(title=f"{n.title} (reversed)", body=n.body[::-1]).store().entity_id


#: Fns the runner mount auto-binds at mount time (name → factory). Fresh
#: instances per mount so FlakyNote's planted failure re-arms on relaunch.
STANDARD_FNS = {
    "uppercase": UppercaseNote,
    "combine": CombineNotes,
    "slow": SlowSteps,
    "flaky": FlakyNote,
    "plan": PlanNotes,
}

#: The deliberately unbound fn (bound/released by the driver's
#: boundness-scenario methods, never by the auto-bind).
EXTRA_FN_NAME = "reverse"
EXTRA_FN_FACTORY = ReverseNote


def fn_key_note(name: str) -> ExpNote:
    """The (content-addressed) handle-key entity for fn ``name``.

    An ExpNote so handle cells render readably; content-addressing makes
    the key id stable across relaunches, which is what lets the mount's
    auto-bind line up with handle cells written in earlier sessions.
    """
    return ExpNote(title=f"task fn: {name}", body=f"demo task fn {name!r}")
