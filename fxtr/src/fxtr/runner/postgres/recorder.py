"""The recorder: the task that commits what a workflow body does, as it does it.

For one live attempt (:class:`~fxtr.runner.postgres.attempts.Attempt`) the :class:`Recorder`
drains the attempt's item stream and records each batch in one transaction: flush the attempt's
entity store (a source array the body stored may be referenced by what follows), insert the emitted
nodes with their edges, names, and ``emit`` events, log ``observe`` events, record the return, and
write any free-form ``ctx.log`` lines the batch carried (into ``attempt_logs``, via
:func:`fxtr.runner.postgres.logs.record_logs`). Events are logged in the order the body acted. The
determinism checks of :mod:`fxtr.runner.postgres.ledger` run inside the transaction. Once it has
committed, the recorder publishes to the shared attempt state (new node states, pending observes,
the returned node) and wakes the evaluator.

The recorder knows nothing about node values beyond loading a leaf's array to derive its type; it
does not compute, answer observes, or finish the attempt. Those are the evaluator's
(:mod:`fxtr.runner.postgres.evaluator`).

**Reports from the body.** When a run reports ``fail`` or ``abandon``, a
:class:`~fxtr.runner.postgres.attempts.Flush` barrier is pushed and awaited before the attempt is
closed, so everything the body pushed before it stopped is on record; a body that emits
synchronously and then raises leaves its emissions in the ledger.

**Failures while recording.** A write guard refusing the attempt (:class:`StaleAttemptError`)
means someone else closed it: the batch is dropped and the attempt is closed locally. Anything else
(a determinism violation, a malformed emission, an unknown node) fails the attempt with the error
and cancels the body; nothing of the batch is recorded.
"""

from __future__ import annotations

from typing import Any

import anyio

from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.runner import nodes
from fxtr.runner.nodes import Node
from fxtr.runner.postgres import execution, ledger
from fxtr.runner.postgres.attempts import (
    Attempt,
    Emit,
    Flush,
    Item,
    Log,
    Observe,
    Request,
    Return,
)
from fxtr.runner.postgres.database import Database, Transaction
from fxtr.runner.postgres.evaluator import Dag, derive
from fxtr.runner.postgres.execution import StaleAttemptError
from fxtr.runner.postgres.ledger import ActiveAttempt, Emission
from fxtr.runner.postgres.logs import record_logs
from fxtr.runner.session import AttemptClosedError, AttemptError


class Recorder:
    """The recorder task for one live attempt (see the module docstring).

    Args:
        attempt: The attempt whose stream this drains.
        database: The runner's database.
    """

    def __init__(self, attempt: Attempt, database: Database) -> None:
        self._attempt = attempt
        self._database = database

    async def run(self) -> None:
        """Record batches until the attempt is closed, then refuse whatever is left on the
        stream."""
        attempt = self._attempt
        while not attempt.ended:
            try:
                first = await attempt.items_receive.receive()
            except anyio.EndOfStream:
                break
            await self.record([first, *self._drain()])
        for item in self._drain():
            if isinstance(item, Request):
                item.finish(AttemptClosedError(f"Attempt {attempt.id} has ended"))
        attempt.items_receive.close()

    def _drain(self) -> list[Item]:
        """Everything on the stream right now."""
        items: list[Item] = []
        while True:
            try:
                items.append(self._attempt.items_receive.receive_nowait())
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                return items

    async def record(self, batch: list[Item]) -> None:
        """Record one batch in one transaction, then publish it (see the module docstring)."""
        attempt = self._attempt
        requests = [item for item in batch if isinstance(item, Request)]
        new_states: Dag = {}
        try:
            async with self._database.transaction() as tx:
                active = await execution.guard(tx, attempt.id)
                await attempt.entities.flush(tx)
                emissions: list[Emission] = []
                log_messages: list[str] = []
                for item in batch:
                    match item:
                        case Emit(node):
                            emissions.append(await self._emission(tx, new_states, node))
                        case Log(message=message):
                            log_messages.append(message)
                        case Observe(identity=identity):
                            await self._flush_emissions(tx, active, emissions)
                            await ledger.record_observation(tx, active, identity)
                        case Return(identity=identity):
                            await self._flush_emissions(tx, active, emissions)
                            await ledger.record_return(tx, active, identity)
                        case Flush():
                            pass  # a barrier: finished once this batch has committed
                await self._flush_emissions(tx, active, emissions)
                await record_logs(tx, active.id, log_messages)
        except StaleAttemptError as exc:
            for item in requests:
                item.finish(AttemptClosedError(str(exc)))
            attempt.mark_closed(refusal=AttemptClosedError(str(exc)))
            return
        except Exception as exc:
            error = AttemptError.from_exception(exc)
            for item in requests:
                item.finish(AttemptClosedError(f"{error.type_name}: {error.message}"))
            attempt.fail(error, cancel_body=True)
            await attempt.close()
            return
        # Committed. Publish without yielding, so the evaluator sees the batch whole.
        attempt.dag.update(new_states)
        for item in batch:
            match item:
                case Observe():
                    attempt.add_observe(item)
                case Return(identity=identity):
                    attempt.returned = identity
                    item.finish()
                case Flush():
                    item.finish()
                case _:
                    pass
        attempt.notify()

    @staticmethod
    async def _flush_emissions(
        tx: Transaction, active: ActiveAttempt, emissions: list[Emission]
    ) -> None:
        """Record the emissions gathered so far and empty the list, so that the events of an
        observe or a return that follows come after them in the log."""
        await ledger.record_emissions(tx, active, emissions)
        emissions.clear()

    async def _emission(self, tx: Transaction, new_states: Dag, node: Node) -> Emission:
        """Prepare a node for recording: derive its state (type, and value for a leaf) unless the
        invocation already has it, in which case it is recorded as an emission of this attempt too
        (what the determinism check at return counts) with nothing new derived.

        Args:
            new_states: The states derived earlier in this batch, extended with this node's.
            node: The emitted node.
        """
        identity = node.content_id()
        existing = new_states.get(identity) or self._attempt.dag.get(identity)
        if existing is not None:
            return Emission(node, existing.type)
        leaf: Array[Any] | None = None
        match node:
            case nodes.SourceArray(array=ref):
                data = await self._attempt.entities.load_raw_data(ref.entity_id, tx)
                leaf = DEFAULT_CONVERTER.structure(data, Array[Any])
            case _:
                pass
        state = derive(node, {**self._attempt.dag, **new_states}, leaf)
        new_states[identity] = state
        return Emission(node, state.type)


__all__ = ["Recorder"]
