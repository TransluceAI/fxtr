see @projects/fxtr_workstation/frontend_lib/CODE_OVERVIEW.md and @projects/fxtr_workstation/CODE_OVERVIEW.md

right now the workstation frontend uses a websocket to a full fxtr RPC backend which allows you to thread entities and services around. this is great for development. but I also want some version of this where I can share the frontend UI but swap the backend for something CDN-friendly.

there is already a HTTP API for fxtr entity data, referenced from @v2/CODE_OVERVIEW.md in the experimental section. there is also with an equivalent client and server in the v2/ts code which implements a small subset of fxtr functionality, with the server in particular being of interest

I'd like to set things up so that there is a "HTTP connector" implemented in the frontend lib, which exposes the same Session interface as the existing workstation connector. however:
- all state would live on the _client_ in some local storage. client manages all short names of all entities and services. server itself is always stateless
- server exposes entities that it knows
- server can also expose _services_ that it knows. but server services are extremely restricted to make it simpler to implement without actually implementing the full fxtr RPC system:
    - there are exactly two types of service:
        - simple services are identified via a regular _URL_ which is the target of the service, and communicated with via GET or POST (this is passed alongside the service identifier so the frontend knows what it should send)
        - event stream services are identified via a websocket URL and a service name; the client speaks to them over a simple JSON-based RPC system (structurally similar to the workstation's client rpc system but simpler)
        - (for both, the client session impl just holds what it knows about the service; it is assumed that on a reconnect the client will be able to continue interacting via these URLs)
    - inputs are serialized to JSON and sent in the message body, tag-escaped in a similar way as the current client backend
        - any entities referred to by the client must already be known to the server, since there's no way for the server to actually request their data
        - the client-side session can specifically refer to one of its own callback notification inbox services by sending its unique ID to the server, which allows server messages to route back to it
    - results are returned as JSON, tag-escaped in a similar way as the current client backend
        - the server can return entity IDs, which are assumed to be statelessly fetchable from the server's entity endpoint (no scope mechanics)
        - the server can also return new services identifiers, usually URLs on this same server
        - all services returned are also assumed stateless and assumed to live arbitrarily long (no lifetime)
    - for a websocket service, all websocket services with the same websocket URL can multiplex over the same connection
        - new requests name the service name and method name they are calling, and pass tag-encoded args
        - the server can respond over this same connection with emitting messages back to the subscriber inbox, identifying it with whatever ID was used by the client when sending it to the service
        - the server then references the ID of the original request when giving its final response or error
- the server would speak a combination of:
    - entity data endpoint (same design as now)
    - registered entity/service discovery endpoint
    - service endpoints as described / introduced by the discovery endpoint

basically this would allow us to expose the same shape of system to the frontend that the frontend already expects, but just structurally limited by the fact that all state (other than transient websocket connection subscriber state) is on the client, and the server is required to be stateless, in exchange for a significantly simpler backend.

components of this:
- part 1: the backend library implementation of this. we should actually implement this twice: once as a python backend and once as a typescript backend. writing the backend should feel like (and mostly be) using an existing backend like Starlette or Hono and then layering on helpers for handling the common logic. e.g. the existing ASGI app / typescript equivalents can be how entities are exposed, and we can assume a fixed number of services at fixed routes, but with support / tooling to help implement the conversion of args and results, the message routing of websocket messages, etc. we can figure out exactly what is worth abstracting in a library as we go, we don't need it to be super complex, and its fine if in practice we just duplicate some code in backends. we will want to have some tester scripts for each that expose at least one example of all of the relevant functionality, for use in testing the frontend.
- part 2: the frontend implementation of this. probably more work than the backend. a connector backend in the frontend_ui system, which implements sessions in local storage, manages all of the short IDs and such itself, and figures out how to talk to the backend. tested by talking to probably the typescript backend. also whatever plumbing is necessary so that a demo could use the new connector in place of the old one.
- part 3: a live demo. it can live under examples/ in the root of the repo. it should spin up a version of the frontend which is basically a full workstation UI except over the client-state-only connector system. goal should be to have it show off all of the main features of the frontend UI over the new backend, including showing entities, having those entities dynamically rendered via dynamic dispatch as a GET service, calling GET services and POST services and getting data and entities and services (which are existing but at undiscoverable URLs) back and then loading them.
