
- stage2: user-facing: can mount and register visualizers for this data in thingwatcher, and use thingwatcher to explore the pins on a pinning namespace
    - fxtr2: new top level module folder ui/ with
        - namespace_view: attaches a thingwatcher watcher to a FxtrNamespace or PersistentPinningBackend with a string name:
            - Tracks the namespace itself with a stateless snapshotter
            - Anchors the namespace object as a TW root with that name
            - Anchor capturer produces a thingstate with key ["fxtr_ui", "namespace_view", name] and type "fxtr.transluce.org/fxtr_ui/namespace_view"
                - thingstate content is the map of current pins according to list_pins() where each output FxtrID maps to a thing key ["fxtr_entity", entity_id.id_string()]
            - Also creates and attaches a thingwatcher immutable store capturer
                - this accepts keys of ["fxtr_entity", entity_id.id_string()] and ignores others (not found)
                - for ones that match, it uses try_lookup to look up their actual data, and wraps them in a ThingState.
                    - for "d"-tagged nodes, they are reported with type "fxtr.transluce.org/fxtr_entity/d/{entity_type_id}" based on the id_string of the "$fxtr.type" of the entity
                    - for other nodes, they are reported as "fxtr.transluce.org/fxtr_entity_fallback"
                    - the payload is a JSON-ification of the CBOR data of the entity, where each of the special fxtr ID types are transformed:
                        - RawWeakFxtrID(inner_id) becomes {"$!fxtr.link.weak": ["fxtr_entity", entity_id.id_string()]}
                        - RawStrongFxtrID(inner_id) becomes {"$!fxtr.link.strong": ["fxtr_entity", entity_id.id_string()]}
                        - RawFxtrEntityMarkerID(inner_id) becomes {"$!fxtr.link.marker": ["fxtr_entity", entity_id.id_string()]}
                    - each of those ID types also turns into a thingwatcher link, whose type is "weak", "strong", or "marker", and whose link key is the sequence of indices or string keys needed to reach it from the root
            - Registers a renderer for the fxtr_ui namespace_view
        - thingwatcher_renderers:
            - defines a ThingwatcherFxtrRenderConfig (with a global instance) which
                - contains its own mapping from FxtrID of a type to (a thingwatcher renderer config or a function that builds this)
                - has a get_thingwatcher_renderer which dispatches "fxtr.transluce.org/fxtr_entity/d/{entity_type_id}" to the appropriate sub-renderer for it
            - defines a top level fn to register a new renderer for a FxtrID representing a type, just adding it to the dict


---

future:

- prototype of system for "streams" interacting with scopes under thingwatcher, proof of concept for using this for LM streaming with orchestration
- for debugging: can mount a thingwatcher view of the full state of a normal fxtr scope system
    - global fxtr thingwatcher debugger optional
    - track all scopes, all objects involved in sqlite or memory arenas, all database contents (with a full scan each time, but its fine because its for debugging)
    - renderers for the basic types

- Fxtr1 - Fxtr2 compatibility + tooling
    - can store any fxtr1 data in fxtr2, possibly with a wrapper
    - full implementation of fxtr1 session manager on top of fxtr2 data
        - same launching system as fxtr 1
        - but, with fxtr2 as the run data storage, loading, and runset tracking
    - thingwatcher tracking of session manager
