"""The multiplexed shape of execution: a worker elsewhere, fed assignments over one message stream.

The runner executes a body through the executor seam (:data:`~fxtr.runner.driver.RunExecutor`),
and the in-process executor (:func:`~fxtr.runner.worker.run_assignment`) calls it in the same task.
This package holds the other implementation of that seam, for a worker in another process:

* :mod:`~fxtr.runner.experimental.multiplexed` — the :class:`~.multiplexed.WorkerSession` protocol
  (the whole worker-facing vocabulary with the attempt id as an argument, so one worker's session
  carries many runs), the :class:`~.multiplexed.MultiplexedHost` that implements it over per-run
  sessions and is an executor toward the driver, and :func:`~.multiplexed.bind_run`, the worker's
  adapter back to a run session.
* :mod:`~fxtr.runner.experimental.worker` — the :class:`~.worker.Worker` loop, which registers
  what it can run and then runs whatever the host assigns until told to stop.
* :mod:`~fxtr.runner.experimental.remote` — the same protocol across a process boundary, as frames
  over one ordered bytes-message channel.

Nothing here is on the path a local launch takes: a launch runs the bodies in its own process,
through the in-process executor. The package is kept in running form for a persistent-worker
executor (a worker process that outlives one job and is fed work over a connection), and it is
kept honest by the reference suite's remote Postgres entry, which runs the whole semantics suite
with a multiplexed host, a served channel, and a :class:`~.worker.Worker` on the other end.
"""
