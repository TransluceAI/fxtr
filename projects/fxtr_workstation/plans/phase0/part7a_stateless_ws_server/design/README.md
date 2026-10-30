# Part 7 detailed design docs

Read [`../overview.md`](../overview.md) first for the goal, the
workstation-vs-stateless inversion, and the governing principle
("a workstation server running in the browser"). Then:

| Doc | Piece |
|---|---|
| [`01_wire_protocol.md`](01_wire_protocol.md) | identifiers, descriptors (incl. connection-scoped transients), escape-codec modes, HTTP + ws invocation encodings, disconnect semantics |
| [`02_server_backends.md`](02_server_backends.md) | Python backend library (TS deferred): codec, registry/discovery, simple-service adapter, ws endpoint, tester script |
| [`03_entity_loading.md`](03_entity_loading.md) | `/data` + precomp, client CBOR→`ResultValue` decode + parity contract, client-side metadata |
| [`04_client_connector.md`](04_client_connector.md) | connector: storage interface + key schema, shortId table, discovery lists, ws pool + generations, session registry |
| [`05_client_session.md`](05_client_session.md) | the session reducer fork: keep/drop inventory, write path, persistence/rehydration, inboxes + `subscribe_to` |
| [`06_shell_decoupling.md`](06_shell_decoupling.md) | the PanelViewer refactor (Part 4): generic panel-viewer core + injected connector-specific panel registrations; minimal connector; drops shared lists/records |
| [`07_dynamic_dispatch.md`](07_dynamic_dispatch.md) | dispatch over the stateless connector; the Part 1 dispatcher reload-on-break + manual reload; WS dispatch hot reload |

All design questions are resolved; the landings are recorded in
[`../open_questions.md`](../open_questions.md) (referenced inline as
"resolved Q-X"). Each doc opens with re-orientation pointers
(file:line) into the existing code.
