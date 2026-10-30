
Overall idea: A multi-client workstation for fxtr, enabling human users and AI assistants to collaborate on data generation and analysis workflows.

- A single workstation server manages the state of all resourcess
- The server also has a number of persistent stateful _sessions_. Each session is an isolated connection that holds some local state.
- Clients are (mostly) reactive views on the server-side state (plus very small amounts of purely-local state in local storage) and do not hold significant state in memory. This means you are free to e.g. close and reopen a client and continue where you left off
- The workstation server, however, is dynamic and stateful. It may hold onto long-running processes and in-memory state, especially for services.
    - It tries to offload _data_ storage onto disk whenever possible, but does not attempt to checkpoint all of its state
- There are two kinds of clients:
    - Browser clients are for humans interacting with the system in a browser. Usually these will multiplex over sessions with one session per panel, plus some raw debug views of what else is going on. This is also where most interaction between agents and humans happens. Sessions in a browser client are usually controlled by type-specific UI code that the human has opened; the human usually won't directly manipulate state.
    - Agent clients are for agents interacting with the system over MCP. Usually each agent session has a single workstation session, and it represents all of that agent's work. These are highly dynamic. Sessions in an agent client are usually controlled via the agent running sandboxed code to update the state of the session. The low-level session state is managed by the MCP server and functions mostly as a durable namespace for the agent's dynamic work.
        - Agents will often accomplish tasks in the MCP by writing code: they can package up code to run in a sandbox as tools for themselves, and they can also build and register visualizers for the browser clients, to enable collaboration with humans.
- Despite being used differently, the two client types have the same basic affordances; they refer to the same universe of objects, and can trigger actions and subscribe to their results in the same way. This makes it possible to build components that can be flexibly passed between humans and AI agents, and expose any general-purpose tools to both.

we have a basic version of this system in place, with relevant parts being
- the agent MCP at @projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md
    - with special attention to how the agent prepares sandboxed code, which we are going to be modifying here
- the workstation server at @projects/fxtr_workstation/CODE_OVERVIEW.md
    - which has a demo package that sets everything up together at projects/fxtr_workstation_demo/README.md
- the sandboxing system at @projects/fxtr_sandboxing/CODE_OVERVIEW.md
    - which has a direct path that the MCP uses directly, and a spawner path that consumes entities that the agent should use
- the browser frontend at @projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md
    - with special attention to the "dynamic dispatch" system, which we are going to be modifying here
- the fxtr2 core at @v2/CODE_OVERVIEW.md

we also have a recent proof of concept for using nix to configure fxtr data, with an actual working prototype at @/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/README.md and a writeup of findings at @v2/plans/phase3/stage11_nix_layers/part2_nix_exploration_findings.md . (I actually also want to change this somewhat when we adapt it because I have realized hardcoding the localhost URL into the FOD script itself is unnecessary; it would be better to use impureEnvVars and have the remote URL and port be configured through this. maybe to be future-proof we have two env vars, one for a base URL and one for a bearer token to send alongside requests to it?)

this part: a fairly ambitious redesign of how the agent manipulates their state, that will try to make all of these pieces cohere together in a more robust and elegant way. the pitch is to take nix and make it the single, unified system that a fxtr workstation agent can use to bridge data and configuration into both its sandbox and the remote workstation:
- each workstation working directory would have a single `flake.nix` adjacent to its `.fxtr_workstation.toml`
- this flake would produce a custom attrset defining the configuration and runtime state for interacting with the workstation, with a few sections:
    - `fxtr.workstation.sandbox.prelude`: path to the prelude file (now owned by nix and read through the nix store, no longer assumed to be in the working dir)
    - `fxtr.workstation.sandbox.venv`: path to a virtual environment that the sandbox should run in; this will get bind-mounted into the sandbox (assumed to have only pure-python packages, _replacing_ the manual packges-walking that we do in the MCP)
    - `fxtr.entities.<name>`: an entity declaration in a layered file store, something like `{ type = "fxtr_entity", layer = "<path to a layered file store>", layerKey = "key in that store" }`
        - these entities would then be available to retrieve by name via some new builtin in the MCP like `retrieve_nix_entity(...)`
        - this would replace the `prepare_package_bundle` builtin in the sandbox: the new pattern would be to prepare the package _as a python script in nix_ and actually turn it into a sandbox bundle object precomputed in a layered file store
        - there could also be a specific "UI preset" type of entity. to start, it could be something like "a list of entries, where each entry contains a FxtrID that is the type ID it is for, and then a map from dispatcher key UUID to a pair (module_subtype_key: str, module: EntityRef[RendererEntity])"
            - we could then define some convention so that the agent can submit this to a running dynamic dispatcher service, and have it reconfigure itself to match this preset, similar to the existing demo but with compilation driven by nix
            - nix would handle building of the esmodules and then running a python script to wire them together into an entity object
- every time the assistant runs a script, if the flake or its files changed (or maybe just relying on nix's own caching) the MCP script would redo a nix build, then use the sandbox prelude and venv from that. so, the agent could edit its nix config and its various files, and then just immediately start using them
    - or would it make more sense for this to be explicit? maybe in v0 the assistant just has to run `nix build` themselves. although the MCP does have to query nix for the built paths, TBD best way to do this
- at some point the "remote entity store" would have to be configured somewhere
    - for nix FOD preloaders, this would be assumed to be passed in via an impure env var + an optional bearer token
    - a v0 of this could just not worry about preloaders, and focus on self-contained layer writers that build everything they need from scratch. although we should make sure we are setting things up so that they are easy to support in the future
    - an intermediate state we might aim for is that
        - to the workstation, the remote entity store is just a "remote store" service registered on the workstation backend
        - during builds the MCP could temporarily re-host the workstation's store on its own localhost and feed in the appropriate impure env var to nix
        - during sandbox executions it would just read out of `fxtr.entities.` via a layered file store reader with a custom in-memory DeepFetchable that connects to the remote store backend
    - eventually maybe the remote is just some actual hosted service and both the workstation and the builder talk directly to that, but we don't have to worry about that right now
