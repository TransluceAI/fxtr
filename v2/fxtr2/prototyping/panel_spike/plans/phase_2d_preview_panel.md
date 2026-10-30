OUTDATED - DO NOT USE

# Goal
- preview panel: special panel where clicked refs open, temporary. maybe shows with a dotted box around it. can drag entity out. has a message saying it is the preview panel and you should drag entities out if you want to keep them.
    - when you click a ref, it tries to find and show a panel where the entity is already open. if none exists, but there is a preview panel, then the entity opens replacing the preview panel. if no usable preview panel exists, we open a preview panel by splitting the active panel to the right.
    - as a constraint, we never put a clicked ref entity into a preview panel that is in the same group as the ref that triggered opening it. if the only available preview panel is in the same group, we instead open a new preview panel to the right. this makes sure that the new view doesn't replace the old one.
    - entity views can be owned by a preview panel, however, if the entity is moved from the preview panel, the panel does NOT close; instead it remains as an empty preview panel
