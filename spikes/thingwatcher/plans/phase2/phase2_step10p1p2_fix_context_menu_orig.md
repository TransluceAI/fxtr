for ThingContextMenu, onOpenInPanel, and Panel IDs, I want to change them to not require this short-ID passing. as part of this I also want to massively simplify how ThingContextMenu works. I'd like to refactor it so that
- panel IDs in DockviewLayout use the string key, NOT the short ID
- openThingPanel thus only requires the string key
- ThingContextMenu is a container component, not just a floating root, and it takes the thing key that it is for as a prop, along with optional group id, but no other state.
- ThingContextMenu wraps its children in a baseui trigger, so that when they are right clicked it intercepts the context menu
- ThingContextMenu owns handleOpenInPanel inside itself, and builds it by using useDockviewApiFromContext() , so no users need to pass any other info in, and no users need to maintain state of the ThingContextMenu, it is simply a wrapper that they put and attach a key to it and then right-clicking triggers the context menu
