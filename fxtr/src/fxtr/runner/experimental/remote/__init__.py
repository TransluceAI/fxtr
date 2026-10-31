"""A :class:`~fxtr.runner.experimental.multiplexed.WorkerSession` across a process boundary.

The two ends of one ordered, reliable bytes-message channel (an
:class:`anyio.abc.ObjectStream` of ``bytes``: in process a stapled pair of memory object
streams, later a socket with length-prefixed framing or a websocket) exchange frames
(:mod:`.wire`): the worker side is a ``WorkerSession`` proxy, the host side an adapter that drives
any ``WorkerSession`` from the frames it receives. Each end keeps, per attempt, the entity ids the
worker may use (:mod:`.known_ids`). Nothing here depends on the ``runner`` extra.
"""
