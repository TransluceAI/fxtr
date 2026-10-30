# Stage 13: Output channels — followups

Items that came out of design review / implementation, deliberately
out of scope for stage 13. Tracked here so they don't get lost.

## Demo-server crash on session close with live wasm spawns (from part 3 e2e)

**Traced — see
[teardown_crash_investigation.md](teardown_crash_investigation.md)**
(three separable issues: a missing teardown-race tolerance on the RPC
push path, the uncontained per-spawn abort in
`fxtr_sandboxing/spawner.py`, and a mangled diagnostic in
`phase1_protocol_violation`). Original observations kept below.

Observed during the part-3 headless end-to-end run (2026-06-12, log
preserved at `/tmp/fxtr-stage13-demo-teardown-crash.log`): after
`close_session` on a session holding the spawned scheduler + two
channel-storage refs + two inbox subscriptions, the demo server
crashed (exit 1) instead of riding out the teardown. Sequence in the
log: hub delivery `LeaseBrokenError` → "dropping subscription"
(handled), workstation inbox unsubscribe `LeaseBrokenError` →
"tearing down anyway" (handled), then the wasm guest aborts its RPC
connection with `ImportedrantExportEntryExpr at id -69: no such
export` (a phase-1 protocol violation — the host referenced an
export the guest had already dropped), which is *unhandled* in
`spawner.py`'s `_spawn_runner_nix_wasm` task group and takes the
whole sandbox environment (and the server lifespan) down. Unclear
whether this is stage-13-specific (the extra channel-storage mounts
+ second subscription level widen the teardown race) or a
pre-existing close-session-with-wasm-subscriptions hazard — needs an
A/B against a pre-stage-13 scaffold. Reproduced identically in the
part-4 e2e run (streaming scheduler; second log at
`/tmp/fxtr-stage13-part4-teardown-crash.log`), so it's at least
deterministic for the channel-bearing schedulers. Also seen in the same log: 19×
`_encode_fxtr_id received an ID with a provenance parent; this is
not supposed to occur for arena-internal paths` warnings during the
runs (task outputs created in the wasm guest carry provenance
parents; possibly tripped by channel-retention copies, possibly
pre-existing demo noise).

## Status subscription (from part 2)

The channel-storage renderers fetch `status()` once per storage-slot
lifetime and never re-invoke, so a *preview* (link pill / hover
card) keeps showing the lifecycle state it fetched even after the
channel completes or aborts. (The FullPanel is unaffected — the
content card's own event subscription carries live state; only the
tag is taken from `status()`, and a tag never changes.)

We may want a way to *subscribe* to status changes so previews get
notified. Could be exposed as a second subscribe-style method on
`FxtrChannelStorage` (cheap status-only events, no message replay) —
but the workstation's `subscribe_to` callback-target invoke
hardcodes resolving the target as `EventSource` and calling its
`subscribe`, so a second method would need `subscribe_to` to grow a
way to parameterize its target method. Plumbing that through is the
real cost; punt until previews staying fresh actually matters.

## Display-style hint for `ChannelStatusPreview` (from part 2)

All three compact slots (BlockLink / InlineLink / HoverPreview) on a
channel-storage service re-dispatch the *same* `ChannelStatusPreview`
key with no indication of which display style invoked it. A custom
tag-type preview renderer therefore can't differentiate (e.g. a
denser inline form vs. a richer hover card). An optional
`style?: "block" | "inline" | "hover"` prop can be added compatibly
later — fallbacks and existing renderers would ignore it. (TODO
comment sits on the key declaration in
`fxtr-ui-rd-types-channel/src/keys.ts`.)
