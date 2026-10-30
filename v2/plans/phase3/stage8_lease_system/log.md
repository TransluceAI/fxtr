# Implementation log

## (2026-04-13) Encountered fundamental issues with v1 RPC system, began redesign

Sketched out lease-based system with conditional grants: https://docs.google.com/document/d/1PjDOy-wPuSpvsI2zkQ76Q8BFDO8B1EdA4ppMKgPD_04/edit?tab=t.5ggbpbpqz6dj

## (2026-04-15) Redesigned system to simplify away conditional grants

Again sketched out in https://docs.google.com/document/d/1PjDOy-wPuSpvsI2zkQ76Q8BFDO8B1EdA4ppMKgPD_04/edit?tab=t.5ggbpbpqz6dj

## (2026-04-16) Finished initial sketch of lease + capability IR

Major simplifications:
- Futures do not need to be explicitly unwrappable as handles or other values; instead we can push
    it into the pipelining layer and just allow directly dispatching on futures. This still gives
    you the ability to pipeline handle operations but has less type boilerplate. The assumption is
    that basically any such pipelined operation is just a message to whoever owns the future that
    they should perform the operation instead of you; you yourself can't actually do anything
    robust with it until you pull the result, and if there was something you misspecified, it's
    cleaner for it to surface just like any other dispatched computation rather than being an
    internal error on some handle/token/lease you think you have.
- Can avoid coupling serialization with lowering by using intermediate message types.

## (2026-04-16) TODO
