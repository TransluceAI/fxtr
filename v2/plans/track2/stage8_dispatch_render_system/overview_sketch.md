Goal: dynamic dispatch render system for fxtr.

Example of kind of thing we want to support: "report-style" rendering. this means, main column + possible side annotations (sections on left, citations on right) + figures inline. but then click-to-focus: clicking on a value moves it to the main column.

Components:
- fxtr-react-dispatch: Type-driven dispatch of fxtr IDs to typed renderers. Keyed by (a) the kind of react component it needs to dispatch to / the props it needs, (b) the ID to render, (c) the props.
    - in terms of dispatch resolution fn keyed by some symbol for each component type + ID types. cached, but can clear the cache
    - react component that resolves through this fn and handles its lifecycle
- report library packages: Report-specific dispatch types and implementations
    - report core
        - Types for components as a main view, as a figure, as other things
        - Outer scaffold component for stitching together these: give it a dispatch registry fn, a root ID, and a scope, and it makes the full view
        - Support for view transition API in browser
    - report datamodel: Fxtr data types for central report components
    - report impls: Renderers for the datamodel in terms of the report core
    - (eventually these could be three separate subpackages)
- example-demo-report: Demo app that assembles some data for a report in the datamodel, attaches the default report impl dispatch handler, builds the root view, and serves it

## Basic dispatch system

See step1_dispatch_design.md

Overall idea of the design:

- React component `<Render entity={myFxtrID} as={interfaceKey} props...>` is the user-level entry point
- `<RenderRegistryProvider handler={...}>` sets up the context for rendering
    - Maintains a cache from (interfaceKey -> $fxtr.type ID -> renderer object)
    - On cache miss, async calls handler to find one, then caches it
    - Handler must implement a typescript interface that has:
        - async fn to find a handler for a specific interface marker and fxtr type ID, given a scope that loads it
        - sync fn to find a _loading_ handler that renders a loading marker that fits the given type before we actually have data for it (optional; if not provided we could just use react suspense)
        - sync fn to find a _fallback_ handler that renders the data if it turns out that we can't find a handler for it
- Dispatch is keyed by a branded symbol
    - helper `declareDispatchType<T>(desc) -> DispatchKey<T>` where `type DispatchKey<T> = symbol & { readonly [__PHANTOM_DISPATCH_TYPE]: T }` for a declared unique symbol `__PHANTOM_DISPATCH_TYPE` only available at typecheck time
    - we'd also restrict `T` somewhat structurally, e.g. it needs to be a react component, it needs to at least take an entity prop?
    - then there would be a registry map type that allows you to collect implementations for various dispatch keys. the implementations need some way of determining if they support the value or not
- some convenience higher-order functions so that if you have a component that has the right interface and requires an EntityRef, we can convert that into a dispatch value that checks that codec and binds it if it matches, without a bunch of duplicate code

## MDX transform
See step2_mdx_transform.md. Will be used to build content for the reports

## Initial setup for report system

- Top-level report rendering package (@fxtr/reports/react, fxtr-reports-react)
    - Defines dispatch keys
        - Main view: How an entity should render if it is the main thing open on the page. Initially this would be e.g. the main report object, but you might want to activate, say, a figure, making it the main view.
            - Props:
                - NavState: An arbitrary JSON object that may be stored in a query parameter. Set by navigation. Can be used to attach a configuration to a view.
                - Annotations: A list of annotation entity IDs that refer to this entity and are enabled in the system. Could represent e.g. comments or feedback. (Rendered entity types are allowed to enforce some sort of expectations on what annotations they are provided, and should warn on ones that they don't support. Usually they should decode them with some codec to determine what they point to and alter that part of their rendering. The main view renderer might choose to show these annotations as a margin note below.)
        - Figure block: How an entity should render if it is a figure being presented as a full-width section of some other entity's main view.
            - No props; figure caption would actually be embedded in the entity itself.
        - Reference link: How an entity should render as an _inline_ element that is a rich link to this object. Useful for outgoing links to typed data inside the system that don't get put in a full figure.
            - Props: link text (optional)
        - Margin note: How an entity should render if shown as a margin note in the right sidebar. Can be used either for main-view-owned content, or for external annotations passed to the main view dynamically.
            - Props: Highlighted (bool)
        - Summary card: How an entity should render as a small card previewing its contents. Useful for showing a list of reports or a list of figures or similarly a list of things in some category.
            - No props.
    - Provides a context `useReportNavigation` which provides `{ navigateTo }`, a function that, when called, changes the main view to be a different FxtrID. Renderers for the dispatch keys above can use this to trigger navigation. `navigateTo` also takes the navstate that it should use. Navigation that only changes the navstate of the main view does not add a new breadcrumb but instead replaces the previous one.
    - Also provides a component `<AttachMarginNote>` which wraps an arbitrary react component, and portals it to be a block in the right sidebar
    - Exposes an entry point that
        - Shows either one of two type of view:
            - React component that takes a list of entities, and renders them in cards
            - React component that takes a single root entity and a list of annotations on it, and renders it as a main view
        - Intercepts and keeps track of the history of navigation via `useReportNavigation`, and uses it to show "breadcrumbs" above the main view of the history of things it has shown
        - Assumes it is being called in an environment where a fxtr scope and the appropriate dispatch handlers have already been configured

- Data model (@fxtr/reports/entities, fxtr-reports-entities)
    - Just contains bicodec entity definitions and type IDs.
    - RootReport: A top level report, primarily intended to be viewed at the top level as a main view.
        - Data:
            - "title": Title of the report. Used in reference link as default.
            - "summary": A short string describing the report, used in preview card
            - "contents": A component tree fragment, as produced by the MDX transform library. Represents the main content of the report. Uses a "RenderFigure" custom component with an entity prop, which will end up dispatching to <Render> with the figure-block render style. Also has a "RenderLink" custom component that turns into a reference link.
    - SubReport: A self-contained piece of a larger report, that can be focused to reveal more detail.
        - Data:
            - "overview": Component tree fragment this subreport should show in figure mode.
            - "detail": Component tree fragment this subreport should additionally show, below the overview, when it is focused as the main view.
    - SimpleComment: A basic comment, to show off annotation system
        - Data: author, comment, target (where target is the entityID of the thing that this is a comment on)
        - Eventually can extend this to target a specific range but for initial implementation does not have to.
    - LinkedSVGFigure: A simplified version of a "figure", which contains data for an SVG + a map from string to FxtrIDs that it can link to. SVG elements can have an additional "data-preview-target" attribute that indexes into this map. SVG representation is a component tree fragment but using svg node types.

- Handlers for data model (@fxtr/reports/react-standard-handlers)
    - RootReport
        - Main view
            - For now, take all annotations and render them as margin notes at the start of the doc. eventually we will want these to be able to float to a specific part of the doc
        - Reference link (minimal implementation)
        - Summary card
    - SubReport
        - Main view (similar to RootReport; shows both overview and detail)
        - Figure block (shows only overview + a "show details" link that navigates to the subreport as the main view)
        - Reference link (minimal implementation)
    - SimpleComment
        - Margin note: shows the author and comment in a small card
        - Main view: similar to margin note in terms of styling, but above it, includes a reference link to the thing it is a comment on, allows you to navigate to the thing that it is a comment on
        - Reference link (minimal implementation)
    - LinkedSVGFigure
        - Main view: show the SVG figure at the top. then, if navstate is set, and it is one of the keys in the map, render whatever was linked to as a figure card underneath, marked as "Selected:"
        - Figure block: just show the SVG figure
        - Reference link (minimal implementation)
        - In both main and figure block mode: wire it up so that, when an element with "data-preview-target" is clicked, we navigate to this figure as the main view, and set the nav state to the data preview target value. The idea is that the SVG figure can link to previews of the data that the figure is a summary of, and then you can follow that through to look at the data that generated the figure.

- Demo app (example-demo-report)
    - Uses the fxtr scope APIs, report data model, and MDX transforms to construct two report objects plus some comments
        - First report should use all features of the data model except comments
        - Second should just use a few, for the purpose of demoing what it would look like to have multiple reports. Second report should link to the first
        - Comments should be pinned at the top level, one on each report as well as one on some sub-report
    - Serves a frontend that is configured to show the reports
        - Lists all reports in the store, shows them as cards
        - Also lists all comments, and wires up the comments so that they show for the thing that they point to


## Additional extensions
- Embed breadcrumbs into URL via pushstate/replacestate
- Add view transitions via the View Transitions API
- Expand comments in data model to refer to specific selections of specific objects, with type-driven resolution of how each main view decides what the comment target is
- Expand SVG figure concept to use Vega instead
- Iterate on design system and styling (can start with something basic)
