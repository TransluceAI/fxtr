# Fxtr

Fxtr is a data layer for tracking provenance and enabling reproducible experiments. It provides content-addressed, typed, immutable storage for semi-structured data, with tools for saving, loading, version-controlling, sharing, querying, and (eventually) visualizing that data. The long-term goal is to be a foundational data and protocol layer for describing both data and computations in a way that supports automatic provenance tracking, hermetic caching, modular re-use, and observability — enabling both humans and agents to navigate the outputs of complex automated pipelines and trace back how any result was produced.

## Why fxtr?

Analyses, experiments, and investigations about language model behavior are often fairly bespoke. You rarely know exactly what you are looking for in advance, and it is useful to have flexibility to use language models to automate various pieces of the analysis to speed up your exploration of a space.

Two not-satisfying approaches:
- You can use a rigid framework that imposes a particular way of looking at your domain, and allows you to automate analyses that have that exact shape. (UK AISI's Inspect is somewhat of an example of this.) This gives you observability and scalability, but often rules out more bespoke analyses by construction.
- You can vibe code. This can sometimes help you answer questions quickly, but it comes at a big cost: it is difficult to understand what happened and verify it is sensible, and it is also difficult to repeat the same analysis under changing conditions.

Fxtr is trying to address this gap by striking a middle ground. The idea is to define a framework that is opinionated about _verifiability_, _modularity_, and _repeatable analyses_, but is **not** pinionated about the shape of the analysis. You should be able to take pretty much any kind of analysis you want to write, express it in terms of this basic framework, and get these benefits "for free". This should hopefully mean that you can basically tell an agent "Please write an experiment to answer <my question>. Use fxtr" + give it a git repo and a fxtr experiment runner, and it should be able to produce a recipe for an experiment or analysis that:

1. Is executed via custom code, using custom data types, with custom visualizations that help you understand/debug/explore the results in whatever form matches what you want it to do,
2. Sits within a general execution framework that lets you monitor and control the execution of this code, so that you have visibility onto what is going on and can faithfully map what you see onto what is actually running.
3. Where the results can be inspected and traced in a uniform way, with verifiable execution, so that for any piece of the experiment you can inspect and re-run that step and make sure it actually does what the agent claims it does,
4. And where the final artifact can be published to the Behaviors Hub as a self-contained (git) repo, so that anyone who clones that repo can do 2 and 3, and build their own forked analyses on top, even without access to the original computer environment or coding agent trace that was used to author it.

Some examples of things that fxtr wants to support:
- An evaluation like the MH pilot, which rolls out dozens of subject models across hundreds of simulated users, then judges each transcript with dozens of judges, where each judgement runs via three providers and is aggregated via majority vote, and finally assembles them all into a top-level matrix that can be interactively explored.
- Procedures for categorizing, ranking, and summarizing a large volume of language model behaviors into a form like Neil's Behaviors Atlas, which can be repeatedly applied to new distributions of model behaviors (even those found by others).
- A vibe-coded experiment created by asking an agent "Does X happen more when Y is in the input?" and getting a report that answers this question, which would be reproducible enough to upload to the behaviors hub.
- Procedures like ABR's user-writer bot, which generate user simulators to help explore model behaviors in a new domain. (Beyond ABR's current human-centric interface, uwbot-on-fxtr would make it possible to ask an agent to run a whole ABR-based experiment and manage its state, finally uploading an artifact that represents the agent's whole analysis.)


## Goals

Fxtr is designed around a few primary goals:

- **Cross-compatibility and interoperability.** Research outputs should be easy to combine and re-use. Fxtr tries to make the most convenient way to define data types also the way that is best set up for others to consume and build on.

- **Stability, hermeticity, and version control.** You should be able to stably refer to a specific version of any piece of data, and have that reference remain valid over time. Data is content-addressed and immutable, so that storing the same data always produces the same ID, and the same ID always refers to the same data. Computations are also designed to be hermetic, so that running the same computation on different machines

- **Inspectability and provenance.** It should be possible to trace any result back through the process that produced it, and to inspect state at any stage. This creates natural incentives for correctness and makes it easier to audit automated pipelines. In particular, even with a very complicated top-level procedure, you should be able to drill down into individual sub-computations and verify them locally.

- **Composability.** As you build better primitives, you will want to compose them into larger workflows. Fxtr aims to let you express this composition within the system itself, so that you still get provenance tracking, version control, and type safety at every level.

- **Ease of use.** Fxtr should speed you up rather than slow you down. It should be easy enough to adopt incrementally, with each piece providing immediate value.


## Core Concepts

### Entities

An **entity** is the fundamental unit of data in fxtr. Conceptually, an entity is a JSON-like dictionary that:

- Is **immutable** — once stored, it never changes.
- Is **content-addressed** — its identity is determined by a hash of its contents. Storing the same data twice always produces the same ID.
- Has a **type** — a `FxtrID` that identifies what kind of data this is and what contract it follows.
- Can **link to other entities** — via strong or weak references, forming a graph of related data.
- Optionally has a **provenance context** — a sidecar `FxtrID` that identifies the computation that build this entity. These are attached by the system automatically to help trace results of computation back to their source code.

Given some entity data, you can _store_ it to get a "FxtrID" that uniquely refers to that data.

Additionally, fxtr maintains a **complete subgraph invariant:** if entity A refers to entity B's FxtrID, then copying entity A will implicitly also copy entity B, similar to how pushing a git branch pushes all of the referenced commits. This means a single entity can serve as a stable identifier for a large DAG of computations and their results.

### Services

A **service** is the fundamental unit of code execution in fxtr. Service objects are opaque handles you can call methods on, using Fxtr's internal RPC system. Like an entity, a service has a **type**, which identifies what kind of service it is and what methods it supports, and optionally a **provenance context** identifying the source code that implements it.

Services are intended to capture "modular components / actors" such as "a judge", "a subject model", "a user simulator", or "the glue code that runs one step of an experiment". Services may be colocated in the same process or distributed across different environments. For instance, you might have a service running inside an MCP server, which connects to a service running on a cluster, which has a fleet of services that each run inside sandbox VMs on the server. In this case the MCP agent can write experiments that build and then interface with these services without worrying about where they are running. For instance, the agent could write a python file defining their glue code, then push that code to the server and ask the server to spawn it, and receive back a service handle that can be used to run that code over server-owned data. The agent could also ask the server to run the code itself, and download the result later.

Services can accept other services as arguments or return them as results, making it possible to express complex procedures like "an adapter service which takes primitive LLM request services and boostraps them into a user simulator service".

Trusted services can be implemented directly and run without any restrictions. However, fxtr also includes _sandbox_ machinery to isolate agent-written code. This both ensures that the agent has properly declared what the code will do and what side effects it will have (ensuring it can be faithfully reproduced and applied to new settings), and provides a security boundary that makes it possible to safely run untrusted code uploaded to the Behaviors Hub.

### Scopes and lifetimes

Resource lifetimes and concurrency are difficult to get right when vibe coding. Fxtr adapts and extends ideas from the "structured concurrency" programming discipline, and uses it to manage resource lifetimes in a consistent way. The goals are:
- Enforce a programming style that agents can follow to make it possible to reason locally about lifetimes in a way that enables global correctness.
- Decouple resource _location_ from resource _interfaces_. Code that uses fxtr entities should work exactly the same way across entities loaded from a remote website, saved to a local database, built just in time by a build system, or cached in memory. Code that uses fxtr services should be able to interface with locally-defined services and remote or sandboxed agent-written service code in the exact same way.

To achieve this, fxtr uses a notion of "scopes" for its lifetime relationships. A scope is an async context manager that gates access to a group of resources (entities and services). Scopes can be nested, in which case the child scope gets access to its parents but not vice versa.

- Entities can be stored into scopes, which validates their integrity and persists them into the fxtr storage (often initially staging them into memory but they can later be saved to disk too). To store an entity in a scope, all other entities referenced by that entity must already be readable in that scope, guaranteeing that data is not silently lost.
- Entities can be loaded from scopes by providing their IDs. Once an entity is loaded, all other entities referenced by the loaded entity also become visible to the scope, so that chasing a set of pointers works regardless of where that entity is stored.
- Local python objects can be mounted into scopes as services, which ties their lifetime to the lifetime of the scope. These services remain active until no more references to their scope remains, and then run user-defined cleanup logic.
- Services can then be passed around between processes via RPC and then eventually _resolved_ into other scopes, which gives the caller a typed interface to the service. All method calls then proxy back to the original service object, as if they were both running in the same process.

### Visualization and reports

Fxtr's approach to visualization is to separate presentation from content. To visualize some data, you first store that data with a fxtr type tag that identifies what kind of thing it is, and then you register a set of React visualizers for it in different contexts (e.g. "what does this look like when rendered in a panel?", "what does this look like when you embed an inline link to it?", "what does this look like when embedded as a figure in a report").

To enable this, fxtr includes a frontend UI rendering system that manages dynamic dispatch:
- The backend maintains a map from (fxtr type ID, rendering context) to a JavaScript ESModule containing its code.
- The frontend pulls these modules when needed, and splices their components into the frontend.

There is a basic form of "reports" built on top of this: agents can write MDX code that references entities, which then gets transformed into fxtr-native entity data, which then participates in this dynamic dispatch system. (This is a slight misuse of MDX but it seems to work OK. Will likely be cleaned up in the future.)

NOTE: Currently all agent-written components get full access permissions on the whole page. This is basically fine for local development / non-malicious agents, but eventually we will want to (a) put things in an iframe to isolate them, (b) ensure that you "trust" any visualizer code that you have turned on.

### Fxtr repo configuration via nix

Fxtr currently uses [nix](nix.dev) to make agent-written code reproducible. This is used for about four purposes:
- Building fxtr entity data out of files in git, when this can be expressed as a deterministic (and cheap / LLM-free) computation.
    - For instance, a project can compile an MDX file into a fxtr entity that can then be served via fxtr's workstation server.
    - The main purpose of this is to provide a convenient way to express data outside of fxtr and then deterministically project it into fxtr, so that it can be loaded by an agent in the MCP or pushed to the Behaviors Hub.
- Compiling and configuring dynamic dispatcher frontend code via ESModules.
    - Every frontend renderer has its own module. Nix is used to compile these so that the frontend code can be shared and customized.
- Setting up sandboxed services that execute inside fxtr.
    - Nix is the primary way to assemble agent-written services. This synergizes well with fxtr's design:
        - Nix makes it possible to deterministically express how to configure a Python environment or container image, which will produce the same results regardless of the environment in which it is executed.
        - Fxtr can then refer to the nix hash of the service for provenance tracking.
- Top level configuration of a fxtr project.
    - This is mostly for convenience, since we already use nix for these other purposes.
    - The ultimate goal is that you can upload any git repo to the Behaviors Hub, and the server will use nix to assemble it and construct a rich view of the data, types, services, and reports contained inside it.

Fxtr core does not require nix, but most of fxtr's project tooling (including the workstation and sandboxing systems) requires it. Right now, by default fxtr manages its own instance of nix inside a podman container, and so [podman](https://podman.io/docs#installing-podman) must be installed to work with fxtr projects.

### Storage, tasks, and durable execution (WIP)

Fxtr currently stores entity data in a sqlite database. There are some WIP tools to also sync this to a remote server.

The original v1 version of fxtr also supported durable execution of DAGs of tasks. This is in the process of being adapted to the current fxtr codebase. The desired end state is to allow researchers to iteratively build out their experiment definition, and then upload it to the Behaviors Hub, so that the full flow of the experiment and all of its dependencies are tracked.

An important feature here is _staleness detection_: when the inputs to a set of tasks change, some of the results may become invalid. Detecting this is important for verifying that the final results of an analysis faithfully reflect the actual code that was executed.

### Fxtr's type system (WIP)

Currently, each of fxtr's types is a combination of a human readable name with a _globally unique ID_. The design intent is that every type is globally distinct, and refers to a _specific contract_ about what the producer and consumer can assume about the data or service.

There is not yet a mechanism for ensuring uniqueness, instead IDs are just generated at random. The eventual plan is to augment fxtr's types with a registry URL that defines a source of truth for "what the contract actually is", with this URL likely pointing to a behaviors hub page.

In the longer term, the type system is planned to support subtyping relationships inspired by Rust traits and Haskell typeclasses. The idea here is that every data object would have:
- a concrete _spec_ declaring exactly what the producer of the data promises is true about the data
- a set of _traits_ or _views_ (similar to "abstract base classes") that consumers of the data can use to inspect it

For instance, a spec might declare something like "this is version 3 of a yaml-format judge result, with the raw outputs available at these fields" along with traits like "this object is a measurement with a `score` field between 0 and 1 and can be correctly consumed by anyone expecting a `score` field between 0 and 1" or "this data has a judge reasoning summary string at `reasoning`, and it is intended to be parsed via the Docent citation format".

This would then make it possible to extend the system with new types that add more data, while still remaining interoperable with code that was not expecting that new data, and also providing a clear "correctness invariant" that enables the producer code and consumer code to be graded locally for correctness (e.g. producer must produce something matching its declared spec, spec must declare that the data implements the trait, consumer must accept any input that implements the trait).

## Layout of this codebase

This codebase is broken up into a number of different python projects, with some typescript as well. By convention, every such project has a file called `CODE_OVERVIEW.md` which describes the content of the project, and _must be kept up to date by any agent touching the code_. (See v2/notes/code_overview_guide.md.) These `CODE_OVERVIEW.md` docs (as well as `LOCAL_CODE_OVERVIEW.md` docs where applicable) are likely to be the best starting point for navigating the codebase.

TODO: describe in slightly more detail.

- Fxtr (fxtr2) core (in `v2/`)
    - Implements the core system and all of the major concepts

- Active / important fxtr "projects" (under `projects/`)
    - fxtr_nix_runner and `nix` dir:
        - Allows running nix either natively or inside of podman. Currently the recommended way to get reproducible execution of code and reproducible builds of reports. Somewhat experimental.
    - fxtr_sandboxing:
        - Hermetic execution of services. Intended to be the backend for running agent-written code which simultaneously (a) forces the agent to declare all of the inputs and side effects of the code so that it is verifiable and reproducible, (b) allows agent-written code to be executed safely without damaging the host system.
        - Builds on nix: agent expresses sandboxed services as python code that is built into an executable artifact via nix and then run in a sandbox
            - Variant 1: OCI container running in a podman virtual machine (most flexible)
            - Variant 2: Python running in `eryx` sandbox (earlier, a bit more secure? faster to build, slower to start, still a WIP)
            - Variant 3: Python running in pyodide inside a permission-locked deno subprocess (middle ground: supports PEP 783 binary wheels, ~1.5s spawns, no VM; V8-isolate-grade isolation)
        - Future: allow a more bare-metal experience (e.g. write services in rust)
    - fxtr_workstation:
        - Backend system for interactively viewing and interacting with fxtr entities and services. Conceptually sort of like a jupyter kernel that speaks fxtr.
        - Allows basic services to be registered (in particular the sandbox spawner and external APIs)
        - Then allows either agents (via MCP) or humans (via web UI) to call methods on those services, retrieve data, and perform various operations
    - fxtr_workstation/frontend_lib:
        - Frontend system originally designed for the workstation. Somewhat independent of the backend but located inside fxtr_workstation due to originating there.
        - React library which uses jotai + a novel (?) "invoke slot" system to ensure a consistent view of state, so that frontend logic can drive agent-written backend flows
        - Dynamic dispatch system allows vibe-coded components to compose with each other:
            - Any React component can use a dynamic dispatch component `<RenderDispatch stub={entity or service} as={desired component type}>`, which then dynamically looks up whether it has a renderer for that object for a given type
            - This flexibility makes renderers flexible while still allowing system-level affordances (e.g. opening any object in a new panel)
        - Currently the main way to interface with this is via a "panel viewer" interface; simpler versions are planned to support consumer-facing report views
    - fxtr_workstation_agent_plugin
        - MCP system that enables an agent to interact with the fxtr workstation kernel
        - Written in "code mode": agent writes code that executes in a local sandbox and interfaces with fxtr that way
        - Slightly janky, may be improved later once the core mechanics are more solid

- Examples to consult to get started (under `examples/`)
    - fxtr_workstation_demo: Demo of the "agent writes code, launches it, visualizes results" flow. Not well integrated with storage and durable execution yet (but hopefully soon). Should be runnable if you install fxtr and podman, and then you can use the MCP client to drive it with Claude Code.
    - fxtr_publishing_demo (WIP): New demo of "publishing" flow, where a git repository is used as the publication format for a fxtr project. Prototype of how you would configure an experiment and report for publication on the Behaviors Hub.

- Additional / WIP fxtr "projects" (under `projects/`)
    - fxtr_report_builder: Somewhat janky report-builder utility. Used by some demos.

- Deprecated and v1 things
    - projects/fxtr_v1: original V1 fxtr codebase, used by MH pilot and behaviors lib, not actively developed. Has some form of entities, services, and tasks, but also has many rough edges (no lifetime system, difficult-to-use types, and an input-addressed task caching system that has ended up being pretty painful to use). Not recommended for use.
    - spikes/thingwatcher: old prototype of fxtr frontend visualization system
    - spikes/clydra: old prototype of agent SDK integration, using thingwatcher
    - spikes/mcp_plugin_spike: old prototype of MCP system, predating new agent plugin
    - spikes/old_typescript: an attempt to port all of fxtr2 to typescript, predating new session-focused frontend system
