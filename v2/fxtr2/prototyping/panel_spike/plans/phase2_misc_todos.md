TODOs:

- fix up renderings
    - should be able to drag and drop a RefLink to create a new view even without holding option
    - DraggableID should not require a viewid as a prop, it should grab it from context. it just needs entity ID
    - DraggableID should be usable inside preview renders and should be used there; when inside a preview it should act like creating a new view (like dragging a RefLink) rather than duplicating a view
    - RefLink should not require the view registry, it should fetch the hook itself internally
    - instead of passing in `data` to the elements and then using `getData` to extract by entity ID, `data` should NOT be passed as input to any renderer. instead there should be a hook `useEntityDataQuery(entity_id)` which returns the data. internally this should use the data bundle context and do the projection itself
    - any entity that does not need its custom args does not even bother unpacking them. similarly any entity that does not have any custom args to pass should not pass them. no renderer should forward its custom args from itself to its children or internal reflinks, that's not necessary
    - the transcript renderer should pass a custom arg `showPreviousTurnLink: false`. the turn renderer should accept this as an optional arg; if exactly false then it should not show previous turn.
- new default view on application start: debug view, collections view, and simple collection entity view are all open and are all panels in the same group with simple collection panel open

- preview panel: special panel where clicked refs open, temporary. maybe shows with a dotted box around it. can drag entity out. has a message saying it is the preview panel and you should drag entities out if you want to keep them.
    - when you click a ref, it tries to find and show a panel where the entity is already open. if none exists, but there is a preview panel, then the entity opens replacing the preview panel. if no preview panel exists, we open a preview panel by splitting the active panel to the right
    - entity views can be owned by the preview panel, however, if the entity is moved from the preview panel, the panel does NOT close; instead it remains as an empty preview panel


- foldables and reservation system: if we have a reservation then a deleted slot can anchor to the reservation instead. callback to rerender the child if it is asked to be jumped to; error to console if it doesn't get shown after a short timeout. maybe the inportal node gets a state and if it is highlighted and becomes shown from hidden then it shows itself.
