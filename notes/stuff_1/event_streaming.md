# Event streaming

Goal: Allow events to be streamed from running fxtr tasks.

# Current state

FxtrProtocolImpl objects expose some methods that have inputs and outputs as fxtr types. These impl objects can be sent over a websocket-based RPC system and proxied through it, enabling communication between objects.

However, every method of a FxtrProtocolImpl is a simple `async def` method whose inputs and outputs are immutable. Generators are not supported, and there is no way to have any side effects that indicate progress is happening.

We want to be able to log the state of computations as they run, but do so in an unobtrusive way.

# Desired state

TODO: refine with feedback
