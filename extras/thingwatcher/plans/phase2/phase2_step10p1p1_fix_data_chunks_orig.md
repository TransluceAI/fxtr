basically, the current detection of whether thing data changed is broken and doesn't work the way I wanted it to. the intended behavior I want:
- data representation:
    - data chunks are essentially connected in a backwards linked list like a git commit branch
    - the thingstate points to whatever data is currently active
    - you can append new data and grow the chain
- behavior when a ThingState is snapshotted
    - ThingState always has some data bytes
    - if the previous state for this object was a ThingState and the data bytes is exactly the same as the previous ThingState, we re-use the same data chunk
    - if the previous state was NOT a thingstate (if it was a thingdelta), or if the data mismatches, create a new chain containing only this data chunk
    - if we have the same data chunk and _also_ all other data about the thing is exactly the same including links, then we don't even create a new thingstate id, we use the existing one. but if links changed then we create a new thingstate also.
- behavior when a ThingDelta is snapshotted
    - ThingDelta may or may not have data bytes and may or may not want to replace
    - if it replaces, we create a new chain containing only this data chunk
    - if it says no difference, we reuse the old data chunk
    - if it says no difference and _also_ says there are no changes to the links, we don't even create a new thingstate id, we use the existing one.


this does not match the existing behavior for a number of reasons
- current state tracking uses data bytes for change detection but this doesn't work nicely if you have thing deltas
- current state tracking handles thing deltas with no-diff in a funny way
- thing deltas with link changes may not increment thingstate id currently
- we make many copies of the same payload data when links change instead of copying

I'd like to simplify this by
(a) moving the data chunks into a separate table with their own data chunk IDs, which have a nullable backpointer to a previous chunk
(b) separately tracking whether the last thing we snapshotted was a ThingState or not, and if it was not a ThingState but we see a ThingState after, don't bother comparing data and instead just create a new chain always
(c) updating all the other logic to properly do the above.
