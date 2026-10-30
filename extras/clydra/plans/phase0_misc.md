Done
- interrupting at any time, via a separate thing in a separate anyio task that just waits in a loop to be triggered
- changing permission mode
- showing permission dialogs and allowing them to be accepted or rejected, supporting whatever the SDK allows
- add a model picker
- clear doesn't clear?
- add a user prompt submit tool response if the user prompt is exactly "/noop" which sets _continue=False, stop_reason="No-op requested, no response needed". (main purpose: load back in session content if we don't have it yet)
- allow folding of input and output messages
- raw json displays should show with a foldable tree view of the json where you can click a triangle to expand/collapse the tree
- tool call results should not show as "You" they should show as "Tool Result"
- use result streaming for tool calls
- collapsed boxes should always be on a single line rather than like a "Claude" header and then a collapse on the line below; so basically everything should be in the details and the header is part of the summary
- session id printout in the ui should be clickable and when clicked it should copy the path to the transcript
- allow canusetool to askuserquestion, see the ant docs
- syntax highlighting and diffs
- use streamdown for markdown: https://github.com/panphora/overtype
- codemirror 6 for inputs via @uiw/react-codemirror, formatted as markdown with no line numbers, trying to keep it simple
- using "@-mentions" via @uiw/codemirror-extensions-mentions and autocompleting to filenames in the dir clydra was started in
- using slash command mentions, detecting them via https://platform.claude.com/docs/en/agent-sdk/slash-commands, and autocompleting them as above
- detecting the special syntax `<tw-i>3</tw-i>` (with arbitrary number) and, if detected, transforming it into an atomic decoration that contains a corresponding InlineRef for the thing with that short ID (portaling it in so that react knows about it)
- `<tw-i>3</tw-i>` in input

TODO
- get running on fxtr:
```
right now clydra uses the claude agent SDK to manage its state. I want to move to a modified system where we instead own all of our state ourselves.

for data storage, we are going to use the fxtr2 system, from /Users/daniel/fxtr-worktrees/fxtr-bg1/v2 . we don't need to worry about the lowest-level details but can instead focus on the user-facing API with normal "data" level nodes, ignoring all the provenance stuff. at a high level this means
- use the mashumaro-based type definitions from entity_serdes and declare each class to have its own unique type ID
- build up state as a sort of IPLD graph / git commit graph object, where each object has a typed pointer to any previous objects
- save the current state into a sqlite arena, for a namespace owned by clydra, essentially with a git-branch like model where we have a named pointer to an object, and we bump it to point to new objects as we update state
    - in particular we will store a mapping from a UUID session id to the current state of the session

the mental model of how this should work for clydra specifically:
- we should take ownership of all state. this means storing all messages ourselves backed by our own storage. every message becomes a fxtr entity
- however, we should preserve exactly the state that the claude agent sdk maintains, and ensure that we can always reconstruct the claude agent sdk state from our own
- we do this by: every time we receive a message from the agent sdk, we read the transcript and find the message in the jsonl, then we compare to the jsonl for the previous message, and we store exactly the sequence of new messages.
- using this, we can exactly restore a message to its state at any of our own entity checkpoints by resetting the session jsonl to our saved state
```
- context graph
    - minimal graph that shows under the input box which has turns on the x axis and context on the y axis and is normalized so that the y axis is as high as max context for the model. use the usage data that comes back from the messages to figure out usage, based on the read/cacheread tokens and the write. also color code by type of token with cache read on the bottom, then new read cached, then new uncached, then output tokens. each turn can be 2ch wide, and it should scroll if it runs out of space.
- separate thing per turn
    - streaming messages go into a new streaming thing
    - use new pop functionality
    - thing turns link to previous turn
    - thing turns show a ref to their previous turn if they are top level, but not otherwise
- save permission mode grants?
