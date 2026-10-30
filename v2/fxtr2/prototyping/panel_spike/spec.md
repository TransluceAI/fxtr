# Design goals for the panel spike

This directory is for prototyping a system of viewing a system of connected nodes in panels, as a
prototype of a larger system. This prototype focuses on the UI state and interactions, on top of
a simplified static set of objects. The full system will also involve querying those objects
dynamically from a richer data model and involve changing them over time.

This doc describes the intended functionality of this prototype.

# Data

The data for the spike has the following form.

There are a set of objects. Objects each have unique string IDs. These map to JSON objects, of a number of types (lets say there are three types). The type is in field "type" of the JSON object and is a string. Each type is associated with a schema of the kind of data it has. Objects can contain raw data but they can also contain things that are intended as ID references to other nodes. Additionally, each object is considered to either "own" or "refer to" each of the IDs it contains.

- "Transcript" has a topic string and a list of "turns" (owned)
- "Turn" has a role and a message, and refers to a previous turn (ref) or null
- "Judgement" refers to a "rubric", has a string explanation, a number score, and refers to a turn
- "Judgement set" owns a list of judgements
- "Collection" owns a list of arbitrary objects keyed by string, in practice it will own a few transcripts, a few judgement sets, and a few rubrics
- "Misconfigured Object" refers to some judgements (but will be misconfigured in the renderer as if it owns them) and also owns some turns (but will be misconfigured in the renderer as if it refers to them)

From this, we precompute a list of "owning parents" for each object. We will intentionally set it up so that some things have multiple owners. In particular we will have one collection that has a set of objects with only one owner (and no misconfigured objects), and then two "overlap collections" where they own overlapping sets of objects and also they have some judgement sets that overlap internally with themselves in each of them (and also these overlap collections will include a misconfigured object).

This is a simplified model of the larger dataset store that we will want to visualize. The goal is to set up a common case and also some edge cases for what the data might look like.


## Renderer registry
Each type has an entry in a _type renderer registry_. This registry determines how values of the type should be formatted in a variety of contexts. Each registry entry contains:

### Full renderer
This is a React component function that takes as props:
- the id and data for the value
- a boolean indicating whether it is at the top level
- an optional dict of custom args, where the renderer should ignore unknown args and also allow missing args

It is allowed (via a context) to use components in itself that
- show a child, given it's id (and possible custom args)
-  show a ref to a node given it's id (and possible custom args)
- wrap a component into a customized link to a node, given its id (similar to an <a> tag)

It then renders the content of the node, formatted nicely, usually as a 2d card. It is generally expected that the renderer renders full components for anything it owns, and refs for ones it refers to but doesn't own.

The main renderer can assume that there is exactly one copy of itself visible at a time, and this copy will be moved around via reverse portal. It is allowed to have react useState.

For the spike, the cards will be simple square boxes with solid outlines and dark backgrounds. They can all be styled similarly but with different colors. All data should be shown.

### Preview renderer
Like the main renderer, and shows similarly, but only shows a preview of the node contents.
This is a React component function that takes as props:
- the id and data for the value

It is allowed via a context to use components that show
- a ref given its id (and optional custom args)

It should render to a small card previewing the contents of the renderer. Children should be shown as refs. The level of detail should be kind of like what you get when you use intellisense/LSP in vscode, or peek a definition. Expect that someone wants a quick overview of the contents that they will scroll through to identify if they want to jump in.

For the spike, we can maybe truncate long strings in the middle with an ellipsis but otherwise show everything.

This component will show in a preview hover card pop-up and is usually transient.

### Ref renderer
This renderer renders a small, one line blob indicating a reference to a node, with block display mode

React component function that takes as props:
- the id and data of the value
- whether it is rendering an owned child or an unowned ref in the current context
- an optional dict of custom args

Should visually evoke the full card, but be only one line. Usually has the ID and also info about the type and possibly the truncated contents. (When using hash-based IDs, can truncate to the first 8 chars. For now can be full.)

Clicking this will open the linked element. Hovering it will show a preview card.

Generally, an owned ref should show with a drawn outline, and an unowned ref should show with an underline, but without a drawn outline.

### Inline ref renderer
This renderer is the same as the ref renderer except that it should render in inline display mode, and should include only minimal information (the ID and some styling that indicates the type).

## Node render manager
The render manager manages the state of all nodes being rendered. It sets up a context in which nodes can render themselves and each other.

To abstract away details of rendering and allow nodes to be moved around, each node is rendered into a reverse portal, with the in portal managed by the render manager. Each node is rendered in a context where the render manager provides info needed for handling the child nodes.

The render manager maintains a set of in-portaled copies of each node, corresponding to distinct places the node can be rendered. It also maintains state using hooks so that it can track which nodes have rendered which other nodes as children, and it controls whether or not the children actually show inline or whether instead they show as "[inline ref] open elsewhere" placeholders.

By default, when a newly opened node tries to render a child that is already open somewhere else, that child actually renders as a placeholder, and clicking the placeholder refocuses the one that is open. Additionally, clicking a ref will by default jump to an open copy.

However, if there is no open copy, the node is allowed to render inline. In this case the child node is actually rendered to a new inportal but then an outportal is attached to the parent. So the state is managed by the render manager still, but the node appears in the dom inside the parent that tried to render it.

It is also possible to use a right click on a rendered node or on a panel to "open a copy". In this case a new panel opens with a copy of the contents. This includes fresh copies of any nodes rendered inside the thing being copied, instead of applying the newly-opened detection logic.

Additionally, you can right click a placeholder to either *move* the rendered copy of this node to this location, or *duplicate* it here. The first case adopts the node from it's current position; if it was at the top level the top level panel closes, if it was in a different parent it gets replaced by a placeholder there. In this case the same inportal gets routed to a different outportal. The second case creates a new copy in this location, from a new inportal.

When a panel is closed, if any of the nodes in that panel are the last copy of the node and they have a placeholder in some other panel, those nodes move into that placeholder, replacing it. (If multiple, one gets chosen arbitrarily.)

The general rule is that, unless a user explicitly creates two copies of a node, whenever there are slots where a node should go, there should be one slot containing the actual rendering, and other slots point to that rendering. But when the user explicitly requests a copy, we can create one. If not explicitly requested this way, we create a node rendering only if none exists, and reparent one on close only if it is the last one.

In the UI and in the code these copies should be referred to as "views" e.g. "Open a new view". Moving them around should refer to "entities" e.g. "Move entity to new panel" or "Jump to entity". Idea is that the data is an entity and it can have one or more views.

The manager also determines how to pull the data for any node by ID. This is how the renderers get their data. It also tracks what node IDs are actually children of the currently-rendered node, and wraps the node in a dashed box with a message if it is rendered as a full rendering but is not actually a child of the node it is inside; similar for if it tries to render as a link-to-child but doesn't contain the child.

## Panel management
Dockview can be used to manage panels. For the spike we can have three types of panel:
- Entrypoint panel: Hardcoded panel that links to the top level entities
- Debug panel: Hardcoded panel that just dumps the current state of the render manager, in a raw form, for watching the state that it is maintaining
- Entity panel: A panel that shows a particular entity in full rendering mode. At most one panel can exist for each entity. The entity panel also shows some other info: above the node, a list of refs to all parents to the node; below the node, a list of backrefs to all nodes that have a ref to the node

When clicking a link or a ref, the entity being clicked on should either become focused (if it is already rendered somewhere) or open in a new panel (if not)

There is up to one panel that is the preview panel. The preview panel is an entity panel. Refs being clicked on open in the preview panel, replacing original contents. However, if you click on the preview panel, it stops being the preview panel and becomes a regular panel. The tab title of a preview panel is in italics. (Inspired by how vscode does preview panels)

## Hover previews
Hovering over a link should show a preview card for the node that will open when clicked.

Hover previews are allowed to contain links themselves. These can also be hovered to show new hover previews.
