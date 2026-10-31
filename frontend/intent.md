* Left: minimap. show the currently open workflow, with vertical breadcrumbs showing it situated in the job
* Center: trace. detailed trace of the currently open workflow
* Right: detail area. detail panel for when you click to inspect an individual entity, or an array.

Use a light blue highlight in the center pane to indicate that something is opened in the right detail pane. In the shell UI, blue should be reserved to indicate selection. (Custom renderers can use whatever colors they want.)

Prefer to indicate progress/activity with motion, rather than color.

Hover preview cards should disappear when the cursor leaves the trigger even if the cursor is on the preview card, when possible (ie assuming there are no interactive elements in the card). This way, if the user is trying to hover something *behind* the preview card, the preview card will not get in the way.

When a user interaction causes a panel to fetch new data, the loading state should not change the size of the panel. It's better to e.g. gray out the stale data while loading (dramatically enough that it isn't mistaken for fresh), rather than remove all the stale data and just show "loading". This avoids having the panel boundaries jump around.

## General design principles (not hard and fast rules—there are trade-offs)
* The minimap/trace should help the user orient, show what they're looking at in the context of the experiment
* j/k keyboard shortcuts should select next/previous item in the currently focused list

## Navigation principles
* When the user clicks a link, they should be able to predict what will happen to the layout (ideally without the aid of the hover highlight)
* The detail view should manage the layout automatically by default (like Miller columns in Finder) for quick browsing.
* Let the user customize layout as desired (like panes in VS Code) so the user can e.g. compare specific things side by side
* Close a pane when it's no longer relevant, unless the user intentionally keeps it. It should be hard to accidentally create a bunch of panel clutter.
* Make it clear "what" exactly the user is looking at in any given pane—is this information about a step in general? an invocation of the step? output of an invocation?
