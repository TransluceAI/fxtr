
# Spec for debug visualizer

## Overview

I have designed an "entity graph" object model that is a layer on top of the IPLD data model. The core idea is that there are:
- immutable values, described by type specs
- "entities", which are values that know their own type spec and are always referenced by CID
- executed module metadata, which is actually built on top of the existing enities, but adds additional metadata about where they were defined and what documentation they have.

I ultimately want the visualizer to be an interactive debugger for the entity graph and allow you to navigate between entities and also view a summary of things defined in a module.

## Protocols and formats

For the frontend, I want to use react typescript. There should be a subdirectory in this directory where the react typescript lives. One of these files should be a script that is designed to run in a minimal HTML page to set up the view. There should also be this minimal HTML page. The script can get requested at, say `/app/main.js`.

For the backend, I want you to use Python to serve it. The way I want the Python side to work is:
- There should be a HubDebugServer object, which owns the state.
- There should be an async method `serve` on the HubDebugServer which makes a Starlette app and calls `serve` on it.
- When the HubDebugServer is created, it should find the path to the typescript package by using `__file__`, identify the main app file, and then use the following to compile it:
    ```
        build_result = subprocess.run(
            ["pnpm", "exec", "esbuild", "--bundle", "--loader=tsx", "--format=esm"],
            check=False,
            text=True,
            input=build_stdin,
            capture_output=True,
            cwd=package_dir,
        )
    ```
    It should then serve the compiled file at the right path.
- When the HubDebugServer gets a request for anything under `/view/` it should just serve the static HTML page. The JS app can figure out what to do by looking at the URL (client-side routing)
- When the HubDebugServer gets a request for `/block/{cid}` it should serve a JSON blob corresponding to that CID.
- When the HubDebugServer gets a request for `/` it redirects to `/view/collection/{module_cid}`

You should use the conversion in object_model/dag_util.py to convert values to an escaped JSON form when passing to typescript. The typescript can just operate on this escaped JSON form directly.

On the python side, you should be able to pass in a ExecutedModuleEntityData to the view constructor. It should then walk the graph using the utility in `dag_util` to turn this into a CID-indexed JSON-escaped subgraph. Each of those objects then becomes a block that the server should serve.

We can assume that every node in this system is a valid entity according to the definitions in `object_model.core`.

## Implementation plan

### Stage 1 (done):

- Python side should take the ExecutedModuleEntityData, process it, and compile the index tsx file. It should also be able to serve all the pages as described above.
- Typescript side should make sure the page is at url `/view/collection/{module_cid}`, then fetch the JSON blob for that CID, then just dump it into a code block on the page. It doesn't need to actually inspect the object. Keep this simple because we will be extending it later.

### Stage 2 (done):

- Typescript side for `/view/collection/{module_cid}` should extract info from the module and show it in addition to the raw module data (which should go in a little details block). Things it should extract:
    - path_in_project (as name)
    - module code (string, also collapsed in a details block)
    - dependencies (each as a link to a `/view/collection/{module_cid}`)
    - entity defns: for each one make a link pointing to `/view/collection/{module_cid}/entdefn/{i}` where `i` is the offset
- Typescript side for `/view/collection/{module_cid}/entdefn/{i}` should
    - request the module CID
    - find the entity defn at that index
    - request the CID for the entity itself as defined in the entity defn, to get the entity
    - show the entity defn object in a code block, and also show the entity data retrieved in a separate code block

### Stage 3 (done):

Goal for stage 3 is to generalize the way values are shown in the UI to enable interactive exploration.

The overall idea is to build a collapsible tree that renders with inline elements. How this works is:
- Every component in the collapsible tree renders itself in inline display mode.
- There's a react context that provides an indentation level, together with a "force_collapse" boolean.
- Every time a component in the collapsible tree wants to emit a newline, it checks the current indentation level with useContext and inserts that many spaces after the newline. Also, if in a force_collapse context, the component should instead render itself on a single line with no newlines.
- Every time a *multiline* component in the collapsible tree renders a child, it provides an updated context around that child that sets the indent level to a larger value than it received, so that lines of the child are indented more than the original indent level.
- Most components that are multiline have a click handler on themselves, and a state saying whether they are collapsed. Clicking toggles the collapsed state. When collapsed they render without extra newlines, don't increment indent state, and set the force_collapse boolean. When expanded locally they check if they are force collapsed already (in which case they display as if collapsed but still treat themselves as expanded in their state) and if not they render with newlines and also increment the indent state for their children.

The main workhorse is a component <BasicCollapsibleView value={...}> which
takes a value to render and a handler for links. It should check its value against known JSON types and render them accordingly, e.g. lists render as either inline or multiline one-per-line lists, dicts the same. Primitives just render themselves normally and don't display differently when collapsed (you don't even have to track collapsed state for these, but it's fine to also have it and just not use it). A good way to structure this might be to have a different function component that builds the view for each specific type, and then have BasicCollapsibleView route to the right one by inspecting its arg.

In stage 3, we will use the BasicCollapsibleView to show each of the JSON objects that we were previously putting in bare code block, providing an initial indent level of 0.

Click events should bubble as follows:
- if the current object is force-collapsed
    - if it is also actually collapsed, it uncollapses locally
    - if it is open, it does NOT uncollapse locally
    - however in both cases it allows the event to bubble up
- if the current object is NOT force-collapsed
    - toggle collapse state locally
    - do NOT bubble up


### Stage 4 (done):

Goal for stage 4 is to make the JSON conversion a bit more robust and deal with escaping. There should be a conversion function that does a similar thing as `dag_util.escaped_atjson_to_dag_cbor`, but instead of producing a DAG-CBOR object, it just builds some lightweight TS classes: one for a CID string (still in string form), one for a bytes object. It also reverses the escaping of other keys.

This should then be used to parse each of the values we get from the `/block/` fetches before passing it into the renderers.

### Stage 5 (done)

Goal for stage 5 is to add basic support for entity linking. There should be a new react context called linkRenderer, and it should hold a callable that you can call with one of the custom CID objects from stage 4. Then `BasicCollapsibleView` can use this when it finds a CID.

As one implementation of this linkRenderer, we can implement a function that takes a CID object and renders it as an orange-outlined `CID(...)` thing. This can be a link, and it should link to `/view/entity/{cid}`

Then we can add a new frontend route for `/view/entity/{cid}` which serves the entity data at that CID directly, also showing it with `BasicCollapsibleView`.

### Stage 6 (done)

For stage 6 we will be generalizing the viewer to allow customizing the display of an object based on its spec.

The main workhorse is a component <HubValueView value={...} spec={...}> where
- `value` is the unescaped-JSON-with-CIDs representation of a value to render.
- `spec` is the unescaped-JSON-with-CIDs representation of the typespec for the value.

What HubValueView should do for now is as follows:
- if `spec` is not recognized, render as `BasicCollapsibleView`
- if `spec` is given and has a $kind field that is one of the strings {int, float, str, bool, none}, also render as `BasicCollapsibleView`
- if `spec` is given and has a $kind corresponding to a tuple or string-keyed frozendict according to the registry in `core.py`, it should render like a tuple or a dict, similar to the collapsing strategy of BasicCollapsibleView but also looking up the child type spec and recursively rendering as a HubValueView
- if `spec` is given and has a $kind corresponding to a data entity, and the value is NOT a link, it should fetch the schema entity for the data entity to find the type id, and then render it like {type_id}({attr1}={value1}, {...}), with each value rendered with the spec for the field in the schema entity

### Interlude: Refactor collapsible contexts

To simplify how collapsing entities for display works:

CollapsibleContext.tsx should define a new component which is usable like this, rougly:

```
<CollapsibleContainer
    incrementIndent={2}
    renderCollapsed={
        () => {
            // return a component to render if this value is collapsed on one line
            // this will automatically become clickable to expand the element
            return <span>(oneline {some_child})</span>;
        }
    }
    renderExpanded={
        (indentString: str, CollapseTarget: Component) => {
            // return a component to render if this value is multiline
            // can wrap things with <CollapseTarget> to make them clickable to collapse
            return <span>
                <CollapseTarget>(</CollapseTarget>
                {'\n'}{indentString}{'  '}{'multiline '}{some_child}
                {'\n'}{indentString}<CollapseTarget>)</CollapseTarget>
            </span>;
        }
    }
/>
```

The CollapsibleContainer should handle all the event listeners etc, so that
- it tracks collapse state for itself
- if it was already force collapsed it just uses renderCollapsed, and also registers a click handler to uncollapse
- if it not force collapsed but is collapsed here, it uses renderCollapsed, and also wraps it in a provider that sets the force collapse to true, and also registers a click handler to uncollapse
- if it is expanded, it uses renderExpanded, but wraps it in a provider to increment indent level by incrementIndent. it then calls renderExpanded with the _original_ indent (before increment), and with a dynamic component function that adds a click handler that will re-collapse, but only for the collapse target
- for all its clickable things it also handles the pointer css so that children don't have to

All components in BasicCollapsibleView and HubValueView should then use this to simplify their logic as much as possible and remove redundant logic.

### Stage 7 (done)

For stage 7 we will be customizing the way entity links are rendered using the new system. The goal is to allow you to load entities in by clicking on them, and populate them with metadata whenever possible so that you can observe info on the LocalEntityDefn for them.

This will involve changing the current way entities are rendered. New version:

- CollapsibleContext should allow a CollapsibleContainer to start collapsed (and the multiline string should do this)

- The link renderer should take an optional spec for the link it is showing. The HubValueView shouldprovide that spec if it exists.

- Component <EntityView entitySpec={spec} entityData?={data} collapseBehavior={...} onExpandWithoutData?={...}> which renders an arbitrary entity, possibly lazily.
    - Checks the $kind of its spec to figure out what its typespec is (similar to how HubValue checks the provided spec) and then dispatches according to that
        - If it is "data_entity" it should render itself as a <DataEntityContents ...> which will involve first looking up the schema entity (like the data entity does now); it can render as a loading message while the schema loads
    - Across all concrete implementations (e.g. in DataEntityContents) it follows these rules:
        - Always starts collapsed
        - When collapsed, renders differently depending on collapseBehavior which has a few options, inspected using equality or an isinstance check:
            - null -> render children normally in collapsed mode, or render as a loading message
            - CIDLink(CID(...)) -> show as something like "MyType(...) [cid: ...]" where the [] part is actually a rounded outline around the CID and both the text and outline for CID are orange
            - CanonicalPath(str) -> show as something like "MyType(...) [path: ...]" where the [] part is actually a rounded outline around the CID and both the text and outline for CID are a light blue
        - When expanded,
            - if it has data, renders its children in an entity-dependent way (for DataEntityContents it should walk its fields and show them)
            - if it does not have data, it immediately triggers onExpandWithoutData, and renders as a loading message

- In the EntityPage, EntityDefinitionPage, and ModulePage, we load the main entity immediately, and then we can provide both the entity and its spec to the EntityView with collapseBehavior null and no callback.

- We also implement a new link renderer in the same file that is defining the EntityView, called render_nested_entities_by_cid.
    - If the renderer is given a spec, it should give that spec to the EntityView, but not yet fetch the entity.
    - If not given a spec, it should fetch the entity and retrieve the spec from it.
    - In either case it should create an EntityView with CID collapse mode. Also when it creates the EntityView it should install a double-click handler on it that links it to the CID-based entity page for the entity that was double-clicked on.
    - The onExpandWithoutData callback should go fetch the entity and populate it, if it isn't already there.

- Finally we use this renderer around any part of a page where we are rendering entities.



# Interlude: Adapting the typespec visitors to track IPLD paths

To enable flexibly querying entities in a generic way, we can modify the core typespec visitor system to track _both_ (1) the path to get to the child in Python and (2) the path to get to the child in IPLD.

This will involve a few steps that are not really related to the visualizer but just improve the object model.

- Change the AccessPath classes to not be entity classes but instead msgspec.Struct subclasses with tag set, move them near the top of the file, and add functions to convert from JSON to AccessPath and from AccessPath to JSON.
- Change the signature for a visitor so that the visit takes two paths: a python path and a IPLD path. Change the recursive visitor to track both.
- Fix the dag_util visitor
- Add a AnyFrozenIPLDable union type, and convert to/from this in the hub representation as a special case
- Add a TypeSpec_AnyFrozenIPLDable type spec, which accepts frozen tuples, frozen lists, primitives, string maps, bytes, and entities. It should (de)serialize them directly to IPLD, with the entities turned into links.
- Modify the blueprint loader file to serialize its access paths and defn keys as AnyFrozenIPLDable, and to save both the python and cbor access paths

# Stage 8a (done)

For stage 8 we will be upgrading the system from stage 7 to also support sub-defn linking.

- Add support for any_frozen_ipldable kind to visualizer -> just call basic collapsible
- Add support for DataEntitySchemaEntity (from core.py) to visualizer -> format it like a data entity but with the hardcoded fields and typespecs for them
    - for this it probably makes sense to reuse the renderEntityExpanded/renderEntityCollapsed but pass the hardcoded field entries instead
- Modify the hub value view and basic collapsible view to both pass IPLD paths through themselves as props (extending them when passing them to their children), and feed it to the link renderer
    - paths should use the same format in JSON as is curerntly used by the `to_ipld` on the `AccessPathStep` variants in `core.py`; only getitem int and getitem str should be needed for traversing IPLD

# Stage 8b

Next we will be modifying the link renderer to allow linking to sub-definitions in an entity.

This turns out to be a bit tricky because of how definitions are linked.

To address this we can use the following design.
1. Write a self-contained async function which takes a module CID and definition offset, fetches the corresponding module, looks up the definition offset, fetches the definition by its CID, and finally returns the loaded entity definition object (a LocalEntityDefn) along with the _name_ of the module it was in. This can go in its own file called like module_util
2. Write a self-contained sync function which takes a current-module CID and LocalEntityDefn object and creates a map from child path (serialized to JSON) to a new definition key {moduleCid: ..., offset: ...}. The module CID comes either from the import key for imported children, or copies the current-module CID if the child path leads to a local definition key.
3. Define a helper function that renders a canonical Python path given a module name and a path list (as something like `load("modulename").qux[1].foo["bar"][2].baz`)
4. Write a link renderer called createEntityDefinitionPathAwareLinkRenderer in `HubValueView` which takes this map as input.
    - When it gets called, it should look up the path in the map, use that to get the module CID and offset, then use the async function from step 1 to load the actual module name and definition object for where that child was defined. If this fails and we can't find or load the defn, fall back to the separate CID entity renderer (which does not need entity defn) and emit a warning.
    - If we have a defn, the renderer should then render the entity using CanonicalPath mode if the loaded definition has a canonical path, or CID otherwise. In this case it should wrap this renderer in a _new_ link renderer which is the same as the default link renderer (e.g. it should just disable itself).
    - This entity should be wrapped with an auxclick handler which links to the /module/{}/entdefn/{} path for the child definition
    - This entity should ALSO be wrapped in a _new_ link renderer which is another instance of createEntityDefinitionPathAwareLinkRenderer, but configured with a new map that was created from the new definition we loaded for the entity we are showing; this will ensure that paths within this new entity are interpreted relative to that entity's own definition.
5. Finally, use createEntityDefinitionPathAwareLinkRenderer inside the entity definition page.

# Stage 9

In Stage 9 we will make a slight improvement to the default-expansion behavior of NestedEntityWithDefnLookup.

- createEntityDefinitionPathAwareLinkRenderer should also take an optional activeModuleCid and activeCanonicalPath arguments, and pass these into NestedEntityWithDefnLookup
- NestedEntityWithDefnLookup should use this as follows:
    - When it retrieves an entity definition, if the moduleCid it gets is the same as the activeModuleCid, and the retrieved entity defn has a canonical path, and the activeCanonicalPath is a _prefix_ of the retrieved entity defn's canonical path (meaning that the new entity is canonically a child of the "active" entity we are currently in the rendering context for), it stores a new field "defnIsCanonicalChild" with value `true` in the setDefnInfo. If this is not the case, it stores `false`.
    - When it builds its own createEntityDefinitionPathAwareLinkRenderer, it passes the new moduleCid from the path info, and the canonical path from the loaded entity definition. This ensures that the new renderer uses a scope for this new entity.
    - If the defnIsCanonicalChild is True, the entityview should be expanded by default.
- When the entity definition page and the module page use createEntityDefinitionPathAwareLinkRenderer, they should also pass the module CID and canonical path accordingly
