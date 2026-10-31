# ts/ — the reusable TypeScript libraries

The TypeScript side of fxtr: the visualization system's shared library, the helpers shells build
on, and renderer packages. The core pnpm workspace rooted at the repo contains
`ts/packages/*` and `frontend`; `pnpm install` at the root installs these,
and `pnpm typecheck` / `pnpm test` run across them. Project renderer environments are independent
(see the root `CODE_OVERVIEW.md`, "Tooling"). Every package here
extends `ts/tsconfig.base.json`
(strict, plus `exactOptionalPropertyTypes` and `noUncheckedIndexedAccess`) and is consumed **as
source**: its `package.json` `exports` point at `src/`, so there is no build step; Vite and `tsc`
resolve the sources directly. React and jotai are peer dependencies of every package, so one
workspace shares one instance of each.

**Outside the checkout.** A renderer package outside the checkout has no `ts/packages/` to link,
and the packages are not on npm. The fxtr wheel carries the ones a project can use (`fxtr-view`,
`-kit`, `-bundle`, `-shell-util`, `-behaviors`: `PROJECT_PACKAGES` in fxtr's
`project/ts_packages.py`), with `tsconfig.base.json` and `css-modules.d.ts`, and
`fxtr views vendor` copies them into the project's renderer package, which links them from there
(see `fxtr/src/fxtr/project/CODE_OVERVIEW.md`). A package in that list must import only the others
in it and the registry packages it declares; a test checks this.

**How renderers reach a shell.** Two ways, by whose they are. The shell's own renderers (the
defaults, `fxtr-view-default-renderers`) are compiled into it from source. A *project's*
renderers are its renderer package *built* into a view bundle (`fxtr-view-bundle`, below), which a
shell loads at runtime, so a shell is never compiled with, or edited for, a project (the
frontend's loader is `frontend/src/views/registry.ts`).

## Layering

```
renderer code (a project's views, fxtr-view-cbor-tree, …)  ──imports──▶  fxtr-view (+ fxtr-view-kit)
a project's renderer package  ──built by fxtr-view-bundle into──▶  a view bundle
shell code (frontend/src/views/)  ──imports──▶  fxtr-view, fxtr-view-shell-util, the defaults
                                  ──loads at runtime──▶  the project's view bundle
```

- **Renderer code imports `fxtr-view`,** and optionally `fxtr-view-kit`'s building blocks. It
  declares components, exports them as a list of renderers, and talks to the world through
  `fxtr-view`'s hooks and components. The only other entity it renders is a link to one
  (`EntityLink`); an element of its own that stands for an entity (a chart's mark) opens it through
  `useEntityLinks`, as following the link would.
- **A shell** provides `fxtr-view`'s context (`ViewShellProvider`), including its own
  `EntityLinkImpl`, and dispatches through `fxtr-view-shell-util`: the registry (in a
  `RegistryProvider` beside the view shell) and the components rendering a slot through it. Shell
  wiring for the experiment viewer lives in `frontend/src/views/`.
- `fxtr-view` itself is **minimal and stateless**: it routes everything to the one context and only
  massages between the raw context value and the user-facing API. It owns no cache, no store, no
  registry, and no shell behavior.
- **A project's renderer package lives with the project.** Only the viewer's own renderers and
  shared renderer libraries are under `ts/packages/`; a project's package (its `views/`, and any
  packages it gathers) sits beside the experiments it renders, as a
  with its own dependency environment, and a project builds its package into its bundle.

## Packages

```
ts/
├── tsconfig.base.json
└── packages/
    ├── fxtr-view/             the shared library (see below)
    ├── fxtr-view-shell-util/  helpers for shells (see below)
    ├── fxtr-view-bundle/      the Vite preset a renderer package builds its view bundle with
    ├── fxtr-view-kit/         building blocks renderers use (see below)
    ├── fxtr-view-cbor-tree/   a renderer package: any entity as a data tree / a type+cid link
    ├── fxtr-view-array-table/ a renderer package: Array entities as a table / a dims-summary link
    ├── fxtr-view-behaviors/   shared views of behaviors' conversation entities (see below)
    └── fxtr-view-default-renderers/  the core renderers every shell starts from (see below)
```

### `fxtr-view`

```
packages/fxtr-view/src/
├── index.ts         the public surface: what renderer code and shells use from the modules below
│                    (module internals such as declareViewSlot and splitCid stay
│                    unexported)
├── errors.ts        FxtrViewError (the base every fxtr-view and shell-util fault extends),
│                    EntityDataError, ShellFault (no ViewShellProvider, or a member the shell
│                    leaves out)
├── ids.ts           EntityID<T> (a class: a cid, phantom data type), isEntityID,
│                    idToJson / idFromJson, idKey,
│                    splitCid / shortCid (display: the common "bafyrei" prefix dropped, the first
│                    8 characters kept, git style)
├── data.ts          DataValue / DataMap / EntityData (decoded DRISL with EntityID leaves),
│                    TYPE_KEY, isDataMap, isEntityData, typeNameOf
├── array_data.ts    the wire shape of a fxtr Array entity (ArrayData: dims as [name, kind]
│                    pairs, a SchemaWire tree, rows as [...keys, value]); ARRAY_TYPE, isArrayData,
│                    rowsOf, scalarValue / scalarEntityId (the one value of a zero-dim array),
│                    structFields, formatSchema, formatDims, fieldOf. Here rather than in the
│                    array renderer because every invocation input and output is an Array
├── invocation.ts    InvocationRef (a workflow by pathname or a step by cache key, within a job),
│                    invocationRefKey, Spawn (a ref with its key by dim name and its output),
│                    InvocationRecord (name, state, error, inputs and output as
│                    EntityID<ArrayData>, children: ChildRun with its Spawns)
├── slots.ts         ViewSlot<Props> (a symbol branded by props), declareViewSlot (internal:
│                    fxtr-view declares every slot);
│                    EntityViewProps (one id) and InvocationViewProps (spawns), the props
│                    contracts a slot's subjects imply
├── renderer.ts      ViewDecl and declareView (a view as links name it: slot, subject type, id,
│                    the params it reads, the view it is declared within), Renderer (slot,
│                    subjectType, id, Component, params, within) and renderer() (of a declaration,
│                    its component handed the params it declares: ViewProps, BoundParams; or of a
│                    slot, subject type and id, reading none), ANY_SUBJECT_TYPE (the subject type of a
│                    renderer for every subject of its slot); what a package exports, nothing that
│                    dispatches
├── shell.tsx        ViewShell (useEntity, the optional useInvocation, useKeys and useLink,
│                    EntityLinkImpl, the optional useEntityLinks),
│                    LoadResult (a pending result carries its completion promise), unwrap (the
│                    value, or suspend), the context with its faulting default,
│                    ViewShellProvider, useEntity, useInvocation
├── builtin_slots.ts EntityPanel (props: id + StorageAtom), EntityInlineLink (static content only:
│                    the shell wraps it in interactive chrome), EntityHoverPreview (props: id);
│                    InvocationPanel (props: invocations + StorageAtom + params); PagePanel
│                    (props: the run + params + StorageAtom), declarePage() and page()
├── link.tsx         EntityLink (renders the shell's EntityLinkImpl), LinkBase / useLinkPlace
│                    (where a value sits), useLink (a link to a declared view, a location relative
│                    to the view, or both: LinkTo, handed to the shell as a ViewLinkSpec; its
│                    params, LinkParams, must bind the view's), useEntityLinks (EntityLinks:
│                    open an entity as following its link would, and whether the shell shows it
│                    now; nothing opens under a shell without it)
├── cid.tsx          CidText: a full cid with the prefix / tail marked to recede and the short run
│                    marked to stand out (a shell styles the classes)
├── view_bundle.ts   the bundle contract (see "View bundles" below): VIEW_API_VERSION,
│                    ViewBundle, ViewBundleFactory, SHARED_MODULES / SharedModules,
│                    VIEW_BUNDLE_MANIFEST_FILENAME, ViewBundleManifest; also the subpath
│                    `fxtr-view/view_bundle`, importable with no React (the preset reads it at
│                    config time)
└── state.ts         subAtom, validatedAtom (+ JsonCompatible)
test/
└── *.test.ts(x)     one file per module (shell.test.tsx builds its own small ViewShell); dispatch
                     is tested in fxtr-view-shell-util
```

Concepts, briefly (the plan has the rationale):

- **Ids.** An `EntityID` is a cid string in a class of its own, so an id is never confused with
  a string or a map. `idKey` (the cid) is the identity for React keys, caches, and panel
  identity. `EntityID<T>` carries a phantom: the TypeScript type of
  the entity's data, asserted once by the renderer for a type and then flowing through typed
  data to `useEntity`.
- **Data.** An entity's decoded value tree, always a map with a `$type` tag, references already
  `EntityID`s. Renderers narrow it to their own types.
- **Surfaces, slots, subjects.** Terminology: a *surface* is a place in the UI where a renderer's
  output appears (the detail pane, a run of inline text, a hover card). A *subject* is what a
  renderer shows: one entity, or one invocation (or several). Subjects come in two
  *categories*, entity and invocation, and a *slot* (`ViewSlot<Props>`) is a surface for one
  category, with the props that category implies: an entity slot's components take the entity
  id, an invocation slot's take the spawns shown. One surface hosts several slots, one per
  category: the detail pane hosts `EntityPanel` and `InvocationPanel`. A *subject type* is the kind
  of subject, the string a slot dispatches on: an entity's type name, or a step / workflow's
  registered name (the `name=` of `@step` / `@workflow`; one shared namespace, so a name must be
  given explicitly to be targetable). Built-in entity slots: `EntityPanel`, `EntityInlineLink`, and `EntityHoverPreview`
  (a compact view a shell shows when a link is hovered; the preview is the card's main content,
  and the shell may add content of its own below it). `EntityPanel` is handed one entity with
  per-mount storage. The
  built-in invocation slot is `InvocationPanel` (the full view, with per-mount storage; the host
  hands it the spawns its selection picks out of a run: one, several, or all of them, or a
  workflow's one invocation, and a panel shows every count it is given).
- **Renderers.** A package declares each renderer with `renderer(slot, subjectType, id,
  Component)`, or, for a view that reads params or that other views link to, declares the view
  apart from its component (`declareView(slot, subjectType, name, { within, params })`) and
  registers `renderer(decl, Component)`, so a view's module links to others by their declarations
  without importing their components. The component of a declared view is handed every param it
  declares (`ViewProps<typeof DECL>`: the slot's props, with `params` binding each), since a host
  shows the view only once they are all bound; their values are the view's to check. A **detail view** (what a renderer opens for something picked in it)
  is a view that reads more params, optionally declared `within` the one view that links to it: its
  id is that view's, `/`, and its own name, and only that view may link to it. A **page**
  (`declarePage(name, { params, within })` and `page(decl, Component)`, the `PagePanel` slot) is a
  view of no subject: a host opens it by name and hands it the run, for it to fetch what it shows. A renderer's subject type may be `ANY_SUBJECT_TYPE`, a
  *wildcard* for every subject of its slot (the cbor-tree views of any entity), which a host offers
  after a subject type's own renderers, so it is the default only for a subject type with none.
  `id` is what a host remembers a choice by and shows in its switcher; it is unique per slot and
  subject type, and a wildcard's among all of its slot's. How renderers are gathered, chosen
  between, and rendered is the host's business (fxtr-view-shell-util's registry below).
- **Invocation renderers.** Every invocation renderer receives `invocations`: a list of `Spawn`s
  (each an `InvocationRef` with its key by dim name and its output id), in the run's order. A
  panel shows one invocation in full and several in a view of its own (an aggregate, a row per
  spawn), useful as soon as it is opened whatever the count. Where the view of several is just the
  one-invocation view per spawn, fxtr-view-kit's `eachInvocation(View, { Summary })` gives it a
  collapsible section per spawn. Its `singleInvocation(View)`, a renderer of exactly one that asks
  for a selection otherwise, is deprecated for that reason. The record, from
  `useInvocation(spawn.ref)`, holds the inputs by argument name and the output as
  `EntityID<ArrayData>`s loaded lazily with `useEntity`, and `children`, the runs the body made
  with their spawns, so a renderer walks its subtree by asking for a spawn's record in turn
  (nothing stops it building a ref to anywhere else). The invocation slots have no wildcard in
  the repo's registries: a name with no renderers offers nothing and the host keeps its own view.
  Degradation is uniform: a load a renderer `unwrap`s that fails, or a renderer that throws, is
  the host's error boundary's to show. Convention: never call `useInvocation` in a loop over
  spawns; give each spawn a component that reads its own `LoadResult`, so a view fills in
  progressively.
- **Loading in a renderer.** `useEntity(id)` returns a `LoadResult` to render something partial
  from; `unwrap(useEntity(id))` returns the value or suspends (throwing the pending promise, or the
  error), the one-liner for a renderer that cannot show anything without the data. Not a hook and
  no option: the hook keeps one return type, and suspending is a function over its result.
  Convention: a renderer `unwrap`s whatever it cannot render without, so waiting and failure are
  the host's to show (its dispatch fallbacks), and draws its own states only for partial
  content (a sub-part still loading while the rest shows).
- **The shell context.** One `ViewShell` value: a `useEntity` hook implementation, an optional
  `useInvocation` hook implementation (a shell that shows entities alone leaves it out, and
  `useInvocation` then yields an error result saying so), an `EntityLinkImpl` component (the
  entity's inline link view, as the shell chooses it, with the shell's link behavior), which
  fxtr-view's `EntityLink` renders, and optionally `useKeys`, `useLink`, and `useEntityLinks`
  (the same link behavior for an element a renderer draws itself, `open`, and whether the shell
  shows an entity now, `isOpen`, the state its link is lit in). Navigation is not a
  member: renderers link, and only the shell decides what following a link does. A renderer names
  a link's target by a view's declaration (`useLink(SCORE_FIT, params)`), a location relative to
  its own view in the host's location syntax (`useLink({ to: "../reply" }, params)`, or
  `EntityLink`'s `to`; the frontend's `panes/address.ts`), or both, and the params it is opened
  at; a link's params must bind the params its view declares, which the types check. An
  `EntityLink` either says where its entity sits in the value the view was handed (`at`, extending
  the enclosing `LinkBase`), the way to link to anything inside it, or names somewhere else (`to`,
  `view`, `params`: another step's output, an input); the types allow one or the other. A shell that
  opens no views yields null, and the renderer shows its detail in place. The default value faults
  on use.
- **State.** Plain jotai. A panel gets a JSON-only `StorageAtom`; `validatedAtom` narrows it to
  a versioned shape and `subAtom` carves child slots. Links get no state.

### View bundles: `fxtr-view-bundle`

A shell built without a project's renderers loads them at runtime from the project's *view
bundle*: a directory holding an ES module (`bundle.js`), an optional stylesheet (`bundle.css`),
and a manifest (`fxtr-view-bundle-manifest.json`: the contract version and the file names). The
module's default export is a `ViewBundleFactory`. The shell calls it with its own instances of the
modules renderer code must share with it (`SHARED_MODULES`: React, the JSX runtime, jotai, and
fxtr-view, which hold hook state, atoms, and the shell context) and gets back a `ViewBundle`, what
the bundle contributes: for now its `renderers`, which a shell adds after its defaults; later
contributions are new fields. Everything else a renderer package uses (the kit, another package's
components, a chart library) is inside its bundle.

**The contract is versioned as a whole.** `VIEW_API_VERSION` covers the factory and `ViewBundle`,
the shared module set, fxtr-view's renderer and slot declarations, the design tokens a renderer's
styles read, and fxtr-view's own class names. The build stamps it into the manifest, and a shell
loads only a bundle at exactly its own version, checking before it imports anything.

**The preset** (`ts/packages/fxtr-view-bundle`). A renderer package's `vite.config.ts` is one line,
`export default defineViewBundleConfig({ entry: "src/bundle.ts" })`, where the entry
default-exports the package's `ViewBundle`. The package's build script uses
`vite build --configLoader runner`: Vite transforms the preset's TypeScript imports, including
linked workspace packages, without relying on Node's native TypeScript support. API callers use
`configLoader: "runner"` too. The build writes the bundle into `dist/`, or
into the directory the `FXTR_VIEW_BUNDLE_OUT_DIR` environment variable (`VIEW_BUNDLE_OUT_DIR_ENV`)
names, which is how `fxtr views build` builds into a scratch directory before replacing `dist/`
(`outDir` overrides both). The config builds the entry as a library IIFE with the shared modules
external, mapped by `output.globals` to fixed identifiers, and JSX forced through the automatic
runtime (so it goes through the shared `react/jsx-runtime`). A post-phase plugin then wraps the
emitted code in the default-exported factory, which binds those identifiers from its `shared`
argument and returns the entry's default export, and writes the manifest last, naming the files
actually emitted (the stylesheet is whatever CSS the package imports, extracted by Vite). The
wrapping is done by the plugin rather than by Rollup's `banner` / `footer`, since Vite places a
library IIFE's banner inside the function. Output is not minified.

### `fxtr-view-kit`

Building blocks renderer authors use: components, shared layout, and helpers, not renderers.
Components: `Kind` (the kind label a panel, preview, or link starts with), `FieldList`,
`MessageList`, `MessageTurn` (one turn's role label and content, for a renderer laying out turns
its own way) and `FirstTurn` over `ChatMessage`s (anything with a `role` and a `content`),
`InvocationHead`, `InvocationOutcome`. `layout`, the class map of the looks renderers share
(`panel`, `card`, `link`, `preview`, `kindLine`, `title`, `subtitle`, `header`, `fieldsLine`,
`prose`, `muted`, `empty`; for tables and charts `table`, `num`, `axis`, `grid`, `chartLabel`, and
the pressable marks' rings `mark`, `cellMark`, `faded`). Helpers: `invocationInput`, `excerpt`,
`turnsLabel`. For charts and the views around them, each module with its own private CSS Module:
`chart.tsx` (`ChartFrame`, a chart's measured frame and its tooltip constrained to the frame's
width; `useWidth`; `pressable`, the
props of a mark pressed from the keyboard too; `Legend`, `ScaleLegend`), `colors.ts` (the series
colours in order and points along the sequential and diverging scales, as `var()` and
`color-mix()` over the shell's `--chart-*` tokens, and the ink legible on them), `format.ts`
(numbers as the viewer writes them), `controls.tsx` (`Stat`/`Stats`, `Segmented`, `Select`,
`DetailBox`), and `stored_state.ts` (`useStoredState`, UI state kept under a key of a panel's
storage, with an optional type guard for reading saved state). Invocation
adapters (`single_invocation.tsx`): `eachInvocation(View, { Summary })` wraps a view of one loaded
record as an invocation renderer of one or several, a collapsible section per spawn, headed by
`spawnKeyLabel` and the optional `Summary`, each loading and failing on its own (`sections.tsx`);
`singleInvocation(View)`, deprecated, renders one spawn's record and asks for a selection otherwise. `LoadEach` (`load_each.tsx`): every distinct id's entity loaded at
once, each by a component of its own (so any number of ids, changing between renders, keeps the
hook order), its body handed the parsed values in hand and how many are pending. `DataTable`
(`table.tsx`): rows a renderer has in memory, under columns the reader sorts, filters, and resizes,
with a header that stays in view while the rows scroll in a box of their own; each column says what
it sorts and filters by (`value`) and how a cell draws (`cell`), and `renderRow` hands each row's
element its props and selection (a view link's, say). Its rules are in its module comment. It is
separate from the frontend's step-card table (`frontend/src/components/TablePanel.tsx`), which
pages, sorts, and filters on the server. The rule for
which is which: a component when the kit owns structure or logic (several elements in a fixed
arrangement, or markup derived from data), a `layout` class when it is a look on one element the
caller writes anyway, of whatever tag and content. The components' own classes are private
(`kit.module.css`, `table.module.css`, `chart.module.css`, `controls.module.css`); `layout`
(`layout.module.css`) is the one shared map. Depends only on fxtr-view, with React and jotai as
peers. A package that uses the kit
bundles its own copy: the kit is not a shared module, so it changes without a new
`VIEW_API_VERSION`, and a bundle built against an older kit keeps working. A piece that settles into
something every renderer wants can move into fxtr-view, as a deliberate change to the contract.

### `fxtr-view-shell-util`

For shell authors only; renderer code never imports it. Depends on `fxtr-view`. Two entries: the
main one for shells (`src/`), and `fxtr-view-shell-util/testing` (`testing/index.ts`) with the
in-memory shell that renderer packages' tests mount, kept out of `src/` and off the main entry.

```
packages/fxtr-view-shell-util/src/
├── index.ts
├── errors.ts              DispatchFault (a FxtrViewError): a duplicate registration, an unknown
│                          renderer id, no RegistryProvider
├── placeholders.tsx       PlaceholderLoading, PlaceholderError, PlaceholderNoRenderer: the
│                          default dispatch fallbacks (class-named spans a shell styles)
├── registry.ts            DispatchRegistry (a flat renderer list) with
│                          dispatchRegistry(), which faults on ids a host would offer twice, and
│                          the lookup renderersFor(registry, slot, subjectType), a scan answering
│                          the specific renderers then the wildcards; chosenRenderer(renderers, remembered), a remembered choice
│                          read back against them (null, the default, once it is gone), and
│                          shownRenderer(renderers, chosen), the renderer a choice shows (the
│                          chosen one, else the first), which dispatch uses too
├── render.tsx             RegistryProvider (the registry in context) and useRegistry;
│                          RenderEntity (entity slots, from an id: load, read $type as the
│                          subject type, dispatch with the id threaded in), RenderInvocation
│                          (invocation slots, from the step / workflow name the host has), both
│                          over RenderSlot (slot + subject type + optional renderer id +
│                          props), which a host also uses for pages; DispatchFallbacks (the host's loading / error /
│                          no-renderer views, passed as `fallbacks` where it dispatches);
│                          useRenderers (a subject type's alternatives, for a host's switcher);
│                          every dispatch sits under an error boundary showing the error fallback
│                          and a Suspense boundary showing the loading one, keyed by the registry
│                          too, so a host replacing its registry remounts what it dispatched
├── cached_loader_hook.ts  createCachedLoaderHook(keyOf, load): a LoadResult hook over an async
│                          loader, one async jotai atom per key (read via loadable, the pending
│                          result carrying the atom's promise), so readers share a load and a
│                          loaded value renders without a loading tick; never evicts.
│                          createCachedEntityHook / createCachedInvocationHook are the
│                          ViewShell.useEntity / useInvocation specializations
└── drisl_source.ts        toDataValue (a decoded DRISL tree as a DataValue, CIDs becoming
                           EntityIDs), decodeEntityData (bytes to tagged EntityData), and
                           createBatchedDrislEntityLoader(entitiesUrl): the EntityLoader for a
                           backend serving many entities' raw bytes per POST (debug_web's
                           /api/entities), queueing loads into batches with a few requests in
                           flight at most
testing/                   test support only, never part of a production shell
├── index.ts               the ./testing entry: re-exports table_shell.tsx
└── table_shell.tsx        TableShellProvider: a whole shell (registry and view shell) over
                           in-memory entity and invocation tables, for tests and static
                           fixtures, and the `useEntityLinks` it is given, if any;
                           PassthroughEntityLinkImpl (the inline link view in a plain span, no
                           behavior)
test/
├── pending_shell.tsx      PendingShellProvider: the registry as given, every load pending
└── *.test.ts(x)
```

- **The registry.** A `DispatchRegistry` is the renderer lists of the packages a shell loads,
  concatenated: `dispatchRegistry(renderers)`, which faults on an id a host would offer twice.
  It holds renderers only; what shows in their place is the host's (below). The renderers of one slot and subject type, the specific ones then the wildcards,
  are the alternatives a host lets the user switch between, in list order, the first being the
  default.
- **Dispatch.** One component per kind of subject, each rendering the renderer with the id you
  name or else the first registered. `RenderEntity` takes an entity slot and an id: it loads the
  entity through the view shell, reads its type name as the subject type, and dispatches with the
  id threaded in (a reference is a bare cid, so the type is only in the data).
  `RenderInvocation` takes an invocation slot, the step / workflow `name` (which the host already
  has from its run data), and the spawns. Each accepts only its own kind of slot; both go through
  `RenderSlot`. `useRenderers(slot, subjectType)` lists the alternatives a host
  builds its switcher from, the same list dispatch picks the first of. A host keeping a choice
  reads it back with `chosenRenderer(renderers, remembered)`, which falls back to the default
  (null) once the remembered id is no longer offered, and a host that also needs the renderer
  itself (to describe what is on screen) gets it from `shownRenderer(renderers, chosen)`, the
  same resolution dispatch makes.
- **Fallbacks.** What a dispatch shows in place of a renderer is the host's, passed where it
  dispatches: `fallbacks={{ loading, error, noRenderer }}` on `RenderEntity` /
  `RenderInvocation` (a `DispatchFallbacks`), each left out being this package's placeholder.
  `loading` shows while `RenderEntity` loads the entity and while a renderer suspends; `error`
  gets what was thrown by a failed load, a renderer, or a renderer id nothing has (a
  `DispatchFault`); `noRenderer` gets the subject type that has none. A host writes each surface's
  fallbacks once, in the component that dispatches for it (a shell's `EntityLinkImpl`, its panel
  wrapper). Each dispatch sits in an error boundary and a Suspense boundary, keyed by slot,
  subject type, renderer, and what is shown (the entity id, or the spawns' refs), so a change
  remounts (and clears an error). The registry itself is part of the key: a host may replace its
  registry while it runs (the frontend does when a project's renderers are rebuilt), and everything
  dispatched through the old one remounts with the new components.
- **Mounting.** A shell mounts `RegistryProvider` and fxtr-view's `ViewShellProvider` side by
  side: the registry is how the host dispatches, the view shell what renderers see. Nothing
  renderer-facing reaches the registry except through the shell's `EntityLinkImpl`.
- **Links.** A renderer's `<EntityLink id>` renders the shell's `EntityLinkImpl`, which is the
  shell's link behavior (click, drag, hover card) around `<RenderEntity slot={EntityInlineLink} id>`, so
  a renderer's links follow the registry and a shell writes only its behavior.

### Renderer packages

Ordinary renderer code: each imports `fxtr-view` (and the kit, if it wants its building blocks) and
exports components plus one renderer list (`<package>Renderers`). The viewer's own packages are
concatenated into the defaults; a project's package puts its list in the `ViewBundle` its bundle's
entry exports, which a shell adds after the defaults. Their tests mount
`TableShellProvider` from `fxtr-view-shell-util/testing` (a dev dependency).

**A renderer package's styles travel with its code.** Its rules are CSS Modules
(`src/<name>.module.css`) imported by the components that use them (`className={styles.grid}`),
so their class names are scoped to the package and cannot collide, and wherever the code goes the
rules go: compiled into a shell (the defaults), extracted into a bundle's `bundle.css`, or loaded
in its tests. No shell stylesheet carries a rule for a renderer package, and no package names
another's class by string. The rules read the shell's *design tokens* (`--fg`, `--line`,
`--select-accent`, `--text-xs`, the chart colours `--chart-*`, …; `frontend/src/styles/tokens.css`)
and define none of their own, so a package inherits whatever theme the shell sets. The frontend's
colour tokens each carry a light and a dark value and switch with its theme, so a colour written as
a literal (in a rule, an inline style, or an SVG attribute) stays the same in both. The tokens and
`fxtr-view`'s own class names (`fxtr-view-cid`, `fxtr-view-fault`, …, styled by the shell) are the
shared part of styling, covered by `VIEW_API_VERSION` like the shared modules. The
documentation's Designing views guide (`docs/fxtr/guides/designing-views.mdx`) is the design guide
for renderer authors: what each token means, and how a view looks and behaves to fit the viewer;
its renderer reference (`docs/fxtr/reference/renderers.mdx`) documents the kit's API for them.

The conventions that follow from scoping:
- Shared looks come from `fxtr-view-kit`: its components, or its `layout` class map for a look on
  an element the caller writes (`className={layout.panel}`); see the kit above.
- A state or variant is a `data-*` attribute on the element, not a class built from a value:
  `data-role={m.role}` and `.turn[data-role="user"]`, `data-state={state}`.
- A package that adjusts a shared look for one of its own contexts puts a class of its own on the
  element beside the shared one and scopes the rule under its own container
  (`.dashReading .dashReadingProse`), since it cannot select another package's classes.
- A class with no rule does not exist in a module, so an element gets a class only for a look;
  the few rule-less classes kept (`layout.panel`, `layout.link`, `layout.preview`, cbor-tree's
  `link`) name an element for tests, and say so.
- Tests: `*.module.css` imports are typed by `ts/css-modules.d.ts` (in `tsconfig.base.json`'s
  `files`), and each package's `vitest.config.ts` processes CSS with
  `modules: { classNameStrategy: "non-scoped" }`, so a test selects a class by its local name
  (`.turn`, `.dashSignificant`); names local to different packages can coincide there, so a test
  picks its selectors with that in mind.

**`fxtr-view-cbor-tree`** — works for any type, so it registers as the built-in entity slots' wildcard renderers. Depends only on fxtr-view, with React and jotai as peers.

```
packages/fxtr-view-cbor-tree/src/
├── tree.tsx    DataTree: a collapsible tree over any DataValue (maps, lists, scalars, bytes,
│               EntityID leaves as inline EntityLinks; the root's rows flush, with no toggle);
│               the collapsed paths live in a JSON record atom the caller hands down;
│               ScalarValue, Container and Branch, its leaves and containers
├── views.tsx   CborTreePanel (type name, cid, the tree; collapsed state via
│               validatedAtom + subAtom on the panel's storage), CborTreeInlineLink (short type name + shortCid),
│               CborTreeHoverPreview (type, cid, the first PREVIEW_FIELD_LIMIT fields, values via
│               CompactValue: scalars, ids as links, containers as their size)
├── names.ts    shortName
├── cbor_tree.module.css  the views' styles
└── index.ts    the above, plus cborTreeRenderers: the tree views as wildcard renderers of the
                built-in entity slots, under the id ENTITY_DATA
                ("entity_data")
```

**`fxtr-view-array-table`** — for `org.transluce.fxtr.core.Array`. The Array wire shape itself
(`ArrayData` and its readers) is fxtr-view's `array_data.ts`; import it from there.

```
packages/fxtr-view-array-table/src/
├── views.tsx      ArrayTablePanel (one Array; header: dims, schema, row count; one column per dim,
│                  then one per struct field or a single value column; Cell renders scalars, ids
│                  as EntityLinks, nested values compactly), ArrayInlineLink (`Array[dims] · n rows`)
├── array_table.module.css  the views' styles
└── index.ts       the views and arrayTableRenderers (the panel as "table", the inline link)
```

### Authoring conventions

A project's renderer package lives with the project (its `views/`), and is described in that
project's code overview. The reference example is `example_projects/basic_project/views`
(`basic-project-views`): `packageManager` set, `vite.config.ts` the one-line preset call,
`src/bundle.ts` the bundle's entry (`{ renderers: demoRenderers }`), and a test
(`test/bundle.test.ts`) that runs the package's build script with native TypeScript loading
disabled where available and loads the bundle in Node
with the shared modules, checking the renderers come back and use the fxtr-view it was given. The
conventions it demonstrates:

- A renderer that cannot render without its data starts with one line,
  `const t = unwrap(useEntity(id as EntityID<TranscriptData>))`, casting its own id once and
  suspending onto the host's loading fallback until the data is there. The ids inside the typed data are
  already typed, so `useEntity` on them returns typed data with no further cast.
- References render as `EntityLink`s, whose labels the host chooses; a mark standing for an
  entity opens it with `useEntityLinks`.
- An entity panel shows one entity (`{ id }`). An invocation panel takes `invocations` and shows every count: its one-invocation view
  (written against the kit's `SingleInvocationProps`, wrapped with `singleInvocation` inside the panel),
  and for several either a view of their own that gives each spawn a row component loading for
  itself, or `eachInvocation`'s section per spawn. Either reads a scalar input with `scalarEntityId(unwrap(useEntity(array)))` and links
  it; shows the invocation's state and error when `output` is null (a run in progress, or a
  failure) instead of a broken view; and walks its subtree with one component per spawn, each
  calling `useInvocation(spawn.ref)` and rendering from the `LoadResult` directly.
- **Style rule: wherever a reader lands on a value, its kind comes first.** A panel, a hover
  preview, and an inline link all start with the type's short name ("Transcript", "Bio", …; a
  renderer knows its own type, so it writes it out; the cbor-tree wildcards derive it), so
  whether you open a value, hover it, or see it linked, you can tell what kind of thing it is
  before reading anything else.

A shell registers renderer packages by concatenating their lists, e.g.
`dispatchRegistry([...defaultRenderers, ...demoRenderers])`.

### `fxtr-view-default-renderers`

What every shell starts from: `defaultRenderers`, the array table's renderers (for fxtr's core
`Array` type) and the cbor-tree views as the built-in entity slots' wildcards. Renderers
only; depends on fxtr-view and the two renderer packages. Project renderers are not here: a shell
adds the renderers of the project's view bundle after these.

### `fxtr-view-behaviors`

`ts/packages/fxtr-view-behaviors` provides entity views for behaviors `ChatRollout`, `Transcript`,
`TranscriptGroup`, `AgentRun`, and session initialization. `entities.tsx` and `messages.tsx`
present conversations, metadata, usage, and failures. `types.ts` exports the current type tags
and wire shapes; rollouts, their turns, and their end records use `.v2`, and a rollout's
`initialization` is a tagged record or explicit null. Rollout and turn metadata arrives as a list
of tagged records, each shown under its type tag, and usage totals come from the `ModelResponse`
records models leave in their turns' metadata. `comparison.tsx` aligns pairs of rollouts;
its `behaviorsRenderers` registers only shared entity types. A behaviors project's own
`views/` package adds its experiment's invocation renderers and includes `behaviorsRenderers` in
its bundle, so stored model calls can be followed from its report
(`example_projects/behavior_rollouts/` is the maintained example).
