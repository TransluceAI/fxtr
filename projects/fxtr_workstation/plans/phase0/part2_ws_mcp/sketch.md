
we will want to combine two things:
- the client-facing RPC endpoints from the existing workstation server
- an earlier spike demonstrating that it is possible to create an MCP server where an agent runs some code in a sandbox ( @spikes/mcp_plugin_spike )

I'd like to create a new folder projects/fxtr_workstation_agent_plugin with the new logic. this should contain:

```
projects/fxtr_workstation_agent_plugin/
    plans/
        ...
    fxtr_workstation_mcp/  # implements the MCP server
        pyproject.toml
        ...
        fxtr_workstation_mcp/
            server.py
            ...
        tests/
            ...
    sandbox/  # stuff that we load into the sandbox, not for the host
        fxtr_ws_mcp_sandbox_bridge/  # package of stuff in the sandbox that connects to the host, like _
            __init__.py
            ...
    claude_plugin/
        .claude_plugin/
            marketplace.json
            plugin.json
        skills/drive_fxtr_workstation/SKILL.md
        .mcp.json  # uses uv to start fxtr_workstation_mcp
    CODE_OVERVIEW.md
    CLAUDE.md
    README.md  # instructions for how to connect / use the plugin
```

the MCP server functionality should expose similar sandboxed execution primitives as the mcp_plugin_spike and use the same file format for the agent working dir. however, it should have a few differences:
- the working dir is expected to have a `.fxtr_workstation.toml` which contains a `workstation_url` key pointing to a running fxtr workstation
- the MCP server gets some new methods
    - `open_session` allocates a session from the workstation and _saves_ it to the .fxtr_workstation.toml at field active_session. errors out if active_session is already set.
    - `close_session` closes the session on the remote and removes it from .fxtr_workstation.toml
    - `connect`: opens the websocket client + RPC connections if not connected (but we also connect automatically anytime another command is run if not already connected)
    - (the scheduler methods were for testing and get removed)
- the MCP server expands its own state significantly so that it can serve as a client for a persistent workstation session, speaking the workstation's /client protocol
    - see the sketch for the overall principles of interaction
    - some more things that are more specific to what the /client protocol ended up as:
        - the MCP server should always try to ack data it receives, and copy that data out of the transient event notifications into its workstation-remote state JSON + its tracked entity/service sets
        - its state JSON can contain:
            - data_by_key: dict[str, tuple[str, Any]] which maps agent-facing short slot keys to a tagged state value like
                - ["exported_entity", entity_shortid] (implies that entity_shortid is tracked)
                - ["exported_service", service_shortid] (implies that service_shortid is tracked)
                - ["invoke_pending", slot_index] (implies that we previously invoked a call with this slot)
                - ["invoke_result", Any] holds the raw (wire-escaped) value from received invoke calls
        - so, when it receives a new event for its session:
            - if a state update: discard its own state, reinitialize from current state, notify the agent that state was reloaded from the workstation, print out a summary of data_by_key (abbreviating invoke results)
            - if an invoke result: copy the result into slot_results, then _notify_ the agent via the channel system
            - if an ack of its own mutation: usually we are waiting for this and this will be our signal to return control back to the agent
            - if a rejection of its own mutation: return control to the agent with the error message
    - for simplicity we do _not_ use client methods to load entity data. instead, we use our RPC connection to `get_entity` by short ID, and then expose this to the sandboxed code over our own RPC channel.
    - so the basic flow of a sandboxed run_script invocation is:
        - build the fake builtins module with the new suite of methods from the sketch
            - where `lookup_entity` specifically is bridged over via the host RPC impl, which itself delegates to our workstation RPC connection `get_entity`, which then bridges that entity all the way into the sandbox
            - get_service / get_id_for_service are just metadata ops
            - export_entity and schedule_invoke schedule work to be done after the script finishes:
                - export_entity causes us to immediately copy this entity out (into a host scope that is still a child of the workstation RPC context) and then schedule exposing it
                    - when we finish the sandbox script, all of these get registered over our RPC connection temporarily while we invoke a mutation request over the client connection (which tracks the entity and puts the short ID into data_by_key as an "exported_entity"), then unregistered
                - schedule_invoke causes us to escape/unwrap services in the args and also immediately copy out any entities that are referred to (just like export_entity), then record some log of what was invoked
                    - when we finish the sandbox script, all of these get registered over our RPC connection temporarily while we invoke a mutation request over the client connection (which tracks the entities, runs an invoke op in a new slot, and adds the slot as a "invoke_pending"), then unregistered
            - retrieve_result walks a result and un-escapes it and then converts it
                - ["service", ...] values map to ServiceRefStub
                - ["entity", ...] values map to actual entities that we bridge in via `lookup_entity`
            - forget records our intent to discard the result
                - when we finish the sandbox script, the final mutation will include dropping this key of the data_by_key
        - actually invoke the script, copying entities out or bridging them in as needed, and accumulating a list of mutation ops we need to apply
        - finally, once the script is done, we have:
            - a list of ops to apply
            - a collection of copied-out entities that were encountered in the sandbox
        - we then register all of these entities over our RPC connection, assigning them UUIDs
        - we then trigger a mutation over our client connection, doing all of the ops and applying the state updates
        - we then wait to receive a confirmation from the client connection that our request was accepted; if not we surface the error
        - then we unregister all the entities, drain our scopes, and return control back to the agent

things we initially skipped but have now implemented:
- callback support
- debouncing and wait, also cancellation scoping
- packaging up code into sandboxable bundles

things we can implement eventually:
- typed service ref stubs; initially we can just allow `UnknownService` and thus just need to return and accept raw FxtrServiceData without invoking type conversion
- idle mutations to update the remote server with the local state even without a run_script
