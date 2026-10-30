/**
 * `WorkstationSession`: the full read+write view of one workstation
 * session subscription. Decodes the wire envelope into a
 * `userState` atom, owns its FSM, re-subscribes across socket
 * drops, ships user writes as `MutateSessionRequest`s through the
 * mutation reducer, and surfaces terminal close paths.
 *
 * Constructed via `connector.createSession()` (async — awaits the
 * server-allocated `session_id`) or `connector.attachSession(id)`
 * (sync — subscribes immediately to an existing session). Both
 * paths run through the same constructor and subscribe in the
 * constructor; consumers see the session in `connecting` state
 * until the first snapshot arrives.
 *
 * Finite state machine
 * --------------------
 *
 *   States:
 *     connecting  - SubscribeSessionRequest in flight; no snapshot
 *                   yet. `userState` is `null`.
 *     connected   - snapshot received; applying live frames.
 *                   `userState` is the current committed state.
 *     stale       - had a snapshot, socket dropped; re-registered
 *                   and waiting for the next snapshot. `userState`
 *                   retains its last value (UI continuity).
 *     closed      - terminal: detach()-ed, connector_closed,
 *                   terminate-commit, or server-side close
 *                   (state_update.closed=true). The session was
 *                   successfully attached at some point.
 *     unavailable - terminal: the subscription itself failed
 *                   (onClose("subscribe_failed")) — server doesn't
 *                   know this session_id. Distinct from `closed`
 *                   so the panel-wrapper can render a
 *                   "session no longer available" body.
 *
 *   Transitions:
 *     (init)      -> connecting    constructor sends subscribe
 *     connecting  -> connected     first onFrame (snapshot) arrives
 *     connecting  -> connecting    onClose("reset") — re-register
 *     connecting  -> unavailable   onClose("subscribe_failed")
 *     connecting  -> closed        onClose("connector_closed")
 *                                  or terminal state_update
 *                                  or detach() drain finished
 *     connected   -> stale         onClose("reset") — re-register,
 *                                  keep last userState
 *     connected   -> closed        terminal state_update
 *                                  or onClose("connector_closed")
 *                                  or detach() drain finished
 *                                  or terminate commit
 *     stale       -> connected     next snapshot arrives
 *     stale       -> stale         onClose("reset") (chained drops)
 *     stale       -> unavailable   resubscribe gets
 *                                  onClose("subscribe_failed")
 *                                  (server lost the session
 *                                  during the drop)
 *     stale       -> closed        onClose("connector_closed")
 *                                  or detach() drain finished
 *
 * Re-registration on reset happens immediately — the connection's
 * outbound queue holds the subscribe across the reconnect, so the
 * session never has to watch `connectionStatus`. Same pattern as
 * `ReactiveList`.
 *
 * Mutation reducer
 * ----------------
 *
 * Two private envelope atoms back the reducer:
 *
 *   committedAtom - last server-authoritative envelope we've seen
 *                   (or `{}` until the first snapshot).
 *   localAtom     - what user code sees. May have edits past
 *                   `inflight` (whose envelope lives in the
 *                   `inflight` field). The public `userState` is a
 *                   derived read over `extractUser(local)`.
 *
 * The `inflight` field (a non-atom struct, since nothing reactive
 * needs to observe it directly) holds the bookkeeping for an
 * outstanding `MutateSessionRequest`: the envelope, update_id,
 * the `sendRequest` promise, the rollback callbacks. `null` when
 * nothing is in flight.
 *
 * Writes resolve into `localAtom`, set the `dirty` flag, and
 * schedule a microtask flush. The flush is one cohesive pass:
 *
 *   - `flushWalk` walks `local.user` to substitute cross-session
 *     stubs with the dead-sentinel, classify each slot
 *     (`new-with-spec` / `legitimate` / broken), and reconcile
 *     `loadDedup`.
 *   - `computePatch` produces a JSON Patch over
 *     `wirePart(committed)` vs `wirePart(local)` (the synthetic
 *     `terminated?: true` is stripped; `loadDedup` is wire-
 *     visible alongside `user`).
 *   - `computeOps` derives Track/Untrack from the user-state stub
 *     diff, `LoadEntityOp` from `new-with-spec` slots, preserve/
 *     unpreserve diff against the server's `committedPreservedSeqs`
 *     mirror, and `CloseOp` for terminate.
 *   - `pendingSlotSpecs` is wiped at the end of the flush.
 *   - The request ships; commit / reject handlers attach.
 *
 * Coalescing: a flush microtask is at most once-per-tick; writes
 * accumulate into `localAtom` between flushes. While a mutation is
 * in flight, the flush is a no-op; post-commit reschedules if
 * still dirty. `pendingFlushPromise` is the explicit awaitable
 * `detach()` uses to drain pending flushes.
 *
 * Rollback: each write may register an `onRollback(info)` callback.
 * On reject (or transport error mid-flight), `localAtom` reverts
 * to `committedAtom`, all accumulated callbacks fire in
 * registration order with a `RollbackInfo` of
 * `kind: "retryable" | "permanent"`. `terminate()`'s returned
 * promise also rejects with `MutationRolledBackError` carrying the
 * same info. `pendingSlotSpecs` is wiped on reject so post-flush
 * allocates don't survive.
 *
 * Slot bookkeeping invariants
 * ----------------------------
 *
 * The session maintains the following invariants across every
 * snapshot apply, foreign state-update absorb, fill-event handler,
 * commit, and rollback. `reindexCommittedState` re-derives the
 * committed-state-indexed parts on each committed change.
 *
 * **`slotFills` retention.** For every slot `s` in `slotFills`:
 *   - `slotFills[s].seq <= committedAckSeq` ⇒ `slotFills[s].seq ∈
 *     committedPreservedSeqs` (the server is preserving it past the
 *     retention horizon).
 *   - `slotFills[s].seq > committedAckSeq` ⇒ event still in the
 *     server's unacked log; no preserve op needed yet.
 *
 *   Entries that fall below the ack horizon without being preserved
 *   are purged by `reindexCommittedState`.
 *
 * **Dedup authority.** `local.{loadDedup, entityMetadataDedup,
 *   serviceMetadataDedup}` and their `committed.*` counterparts are
 *   wire-visible (per `stage2_post1_wire_load_dedup.md` for the load
 *   case; the metadata cases follow the same pattern, added in
 *   stage 7-pre-1). The unified `committedSlotDedupInfo` map is the
 *   slot→(kind, shortId) reverse index across all three and is
 *   rebuilt by `reindexCommittedState`. The forward maps cover every
 *   slot in the session — ours, foreign mutators', and inherited
 *   from prior session instances on attach.
 *
 * **Inbox event log derivation.** For every slot `s` in
 *   `inboxLogs` (chunk 3 onwards):
 *   - `inboxLogs[s].firstIndex == min(stub.offset for inbox stubs
 *     in committed user state for s)`.
 *   - `inboxLogs[s].events` corresponds 1:1 (in seq order) with the
 *     `CallbackEvent`s the server is preserving for slot `s` —
 *     i.e. seqs in `committedPreservedSeqs` whose originating event
 *     was a `CallbackEvent(slot=s)`.
 *
 *   `reindexCommittedState` advances `firstIndex` and drops
 *   front-events when committed `min(offset)` advances; drops the
 *   entire `inboxLogs[s]` entry when no inbox stub for `s` remains.
 *
 * **Slot monotonicity.** `localSlotCounter` ≥ every slot we've ever
 *   seen (locally allocated, observed on the wire, or in a
 *   snapshot's `slot_counter`). `lookupOpResult` and
 *   `handleFillEvent` throw on `slot > localSlotCounter` — that's
 *   an ordering bug (server announced a slot we haven't seen the
 *   state-update for).
 *
 * `lastProcessedSeq` invariant
 * ----------------------------
 *
 * The session tracks the highest `seq` it has *fully processed*.
 * Sent as `ack_seq` on outgoing mutations.
 *
 * Snapshots are *baselines* — they set `lastProcessedSeq =
 * snapshot.last_state.seq` unconditionally (a re-snapshot after
 * reset may legitimately equal the current value if no foreign
 * mutations happened during the gap). Delta events
 * (`state_update` and the non-state events we drop) are
 * *increments* — strict `>` check; stale duplicates dropped with
 * a warn. The strict-> check is the tripwire for "we acked past
 * something we haven't processed" bugs.
 */

import {
	CallbackHandleStub,
	CallbackInboxStub,
	type EntityMetadata,
	EntityRefStub,
	type FxtrSessionAtom,
	type FxtrSessionUpdateCtx,
	type FxtrUpdate,
	type InvokeArgValue,
	type MethodArgs,
	type MethodKwargs,
	type MethodNames,
	type MethodReturns,
	MutationRolledBackError,
	OpResultStub,
	type ResultValue,
	type RollbackInfo,
	type ServiceMetadata,
	ServiceRefStub,
	type Session,
	type SessionLifecycleState,
	type StateValue,
	TAG_BROKEN_OP_RESULT,
	TAG_OP_RESULT,
	type UserState,
	type UserStateWriteOptions,
	WireEscapeError,
	type WireValue,
	escapeInvokeArgs,
	unescapeWire,
} from "@fxtr-ui/core";
import {
	type Atom,
	type PrimitiveAtom,
	type WritableAtom,
	atom,
	type getDefaultStore,
} from "jotai";
import {
	ConnectionClosedError,
	ConnectionLostError,
	ProtocolError,
	type SubscriptionCloseReason,
	type SubscriptionFrame,
	type SubscriptionHandle,
	type SubscriptionHandler,
	type WorkstationServerConnection,
} from "./connection";
import type {
	CallbackEvent,
	CallbackSubscriptionEvent,
	EntityMetadataErrorEvent,
	EntityMetadataResultEvent,
	InvokeErrorEvent,
	InvokeResultEvent,
	LoadErrorEvent,
	LoadResultEvent,
	MutateSessionRequest,
	MutationCommittedResponse,
	MutationOp,
	MutationRejectReason,
	MutationRejectedResponse,
	ServiceMetadataErrorEvent,
	ServiceMetadataResultEvent,
	SessionEventFrame,
	SessionSnapshotEvent,
	StateUpdateEvent,
} from "./protocol";
import { type JsonPatchOp, collectStubsInState, diffStateToPatch } from "./state";
import {
	type FillRecord,
	type InboxEntry,
	type InboxLog,
	type InboxSubscriptionState,
	WorkstationCallbackHandleStub,
	WorkstationCallbackInboxStub,
	type WorkstationEntityRefStub,
	WorkstationOpResultStub,
	type WorkstationServiceRefStub,
	type WorkstationStubSession,
} from "./stubs";

type JotaiStore = ReturnType<typeof getDefaultStore>;
type AtomGetter = <T>(a: Atom<T>) => T;
type AtomSetter = <Value, Args extends unknown[], Result>(
	a: WritableAtom<Value, Args, Result>,
	...args: Args
) => Result;

/**
 * The full session-state envelope as held by the local atoms. The
 * wire form is `{user: ...}`; we augment with a synthetic
 * `terminated?: true` field that the reducer translates to a
 * `CloseOp` on the wire (never serialized into the patch).
 *
 * `committed.terminated` is always undefined (server never writes
 * it). `inflight.envelope.terminated` reflects whatever was sent;
 * if we sent a terminate, it's `true`. `local.terminated`
 * reflects the user's current intent.
 */
export interface LocalEnvelope {
	readonly user?: UserState;
	readonly terminated?: true;
	/**
	 * Wire-visible dedup state — `Record<DedupKind, Map<shortId,
	 * slot>>` (see `DedupForwards`). Lives in the envelope so
	 * rollback (`local = committed`) drops speculative allocations
	 * automatically. Maintained at two points:
	 *
	 *   - At flush time, the walk rebuilds each bucket from the
	 *     current local user state. The reconciled record gets
	 *     shipped in the JSON Patch alongside the user-state diff
	 *     (as a single nested `dedup` value).
	 *   - On `absorbState` (snapshot apply or foreign
	 *     state_update), read straight from the wire-decoded
	 *     state's `dedup` field — the server's broadcast is
	 *     authoritative, so foreign-mutator allocations and prior-
	 *     session allocations (post-attach) appear here too.
	 *
	 * `allocate{Load,GetEntityMetadata,GetServiceMetadata}` do NOT
	 * write to `dedup` synchronously — same-tick dedup goes through
	 * a `pendingSlotSpecs` scan instead, and the wire write lands at
	 * flush time alongside the corresponding `LoadEntityOp` /
	 * `GetEntityMetadataOp` / `GetServiceMetadataOp`. See
	 * `stage2_post1_wire_load_dedup.md` (load wire-visibility
	 * design) and `stage7pre1_metadata_load.md` (extension to the
	 * two metadata kinds).
	 */
	readonly dedup: DedupForwards;
}

/**
 * Internal construction options. Consumers go through
 * `connector.createSession` / `connector.attachSession`, which
 * fill these in.
 */
export interface WorkstationSessionOptions {
	readonly connection: WorkstationServerConnection;
	readonly sessionId: string;
	/**
	 * The jotai store the session's atoms (`localAtom`, `committedAtom`,
	 * slot-state atoms, …) bind to. The connector picks the default
	 * (its own `store`) when the consumer doesn't override it via
	 * `createSession` / `attachSession`'s `store?` option; consumers
	 * who want per-panel isolation (e.g. two `attachSession` calls on
	 * the same `session_id` whose UI state shouldn't intermix) pass a
	 * fresh `createStore()` per session.
	 */
	readonly store: JotaiStore;
	readonly lookupEntity: (shortId: string) => WorkstationEntityRefStub;
	readonly lookupService: (shortId: string) => WorkstationServiceRefStub;
	/**
	 * Mark a service short_id broken connector-wide. Called when a
	 * `ServiceBrokenNotification` arrives on this session's
	 * subscription. The session forwards it to the connector (the owner
	 * of the connector-level broken-services set) rather than tracking
	 * brokenness per-session — a broken service is a global fact.
	 */
	readonly markServiceBroken: (shortId: string) => void;
}

/**
 * Outstanding-mutation bookkeeping. Either everything in here is
 * set (a mutation is in flight) or `inflight` is `null` (nothing
 * in flight). Grouped so that the multiple fields move together
 * and a future stage adding more fields (preserve obligations,
 * etc.) just extends this struct.
 */
interface Inflight {
	readonly envelope: LocalEnvelope;
	readonly updateId: number;
	/**
	 * The `ack_seq` we sent with this mutation. On commit, the server's
	 * `last_state_ack_seq` becomes this value; we then mirror it into
	 * `committedAckSeq` so the preserve-set re-derive can tell which
	 * `slotFills` entries are server-preserved vs still in event_log.
	 */
	readonly ackSeq: number;
	readonly sendPromise: Promise<MutationCommittedResponse | MutationRejectedResponse>;
	readonly rollbacks: ReadonlyArray<(info: RollbackInfo) => void>;
}

/**
 * Tracking for `terminate()`'s returned promise. The promise
 * resolves on the mutation's commit, rejects with a
 * `MutationRolledBackError` on its rollback.
 */
interface PendingTerminate {
	readonly resolve: () => void;
	readonly reject: (err: Error) => void;
}

/**
 * Output of the pre-flush walk over `local.user`. The walk does
 * cross-session-stub substitution, slot classification, and
 * loadDedup reconciliation in a single pass.
 *
 * # Two ways a stub can be broken
 *
 * The reducer distinguishes two structurally different failure modes
 * for stubs that appear in local user state but shouldn't be there
 * as-is:
 *
 * **Cross-session stubs.** An `OpResultStub` whose `session` backref
 * points at a different session than ours — typically the user
 * copied a stub out of one `WorkstationSession`'s state and into
 * another's. We can't make the stub's atoms report errored because
 * we don't own it. During the walk we **substitute** the stub leaf
 * in local user state with our session's dead-sentinel (`slot = 0`,
 * atoms hardwired to errored). The substituted user is written back
 * to the local atom; the wire patch carries
 * `["broken_op_result", 0]`.
 *
 * **Our-but-broken slots.** The stub belongs to this session but
 * the slot is in trouble: either (a) it's referenced in local user
 * state without a matching `pendingSlotSpec` and isn't already in
 * committed (the slot was foreign-removed while we still held a
 * reference, OR the user manually constructed a stub bypassing
 * `allocateLoad`), or (b) the slot integer exceeds
 * `localSlotCounter` (encode-side variant of the `lookupOpResult`
 * tripwire). We *don't* substitute — the stub stays at its path in
 * local state; atoms read from `brokenSlots` and report errored.
 * The wire patch carries `["broken_op_result", slot]` with the
 * real slot integer.
 *
 * Both cases share the `broken_op_result` wire tag, distinguished
 * by `slot === 0` (substituted cross-session) vs `slot > 0` (our
 * own broken slot). See "Broken stubs" in
 * `stage2_entity_loading.md` and `WorkstationSession.flushWalk`.
 */
interface FlushWalkResult {
	/**
	 * Walked user state, with cross-session stubs substituted by
	 * the session's dead-sentinel. `null` iff
	 * `local.user === undefined`.
	 */
	readonly substitutedUser: UserState | null;
	/**
	 * `true` iff at least one cross-session substitution occurred
	 * — the caller writes `substitutedUser` back to the local
	 * atom so subsequent reads see the dead-sentinel rather than
	 * the cross-session stub.
	 */
	readonly substituted: boolean;
	/**
	 * Reconciled `DedupKind → (shortId → slot)` forward record for
	 * the diff's inflight envelope. Populated by the walk from
	 * `pendingSlotSpecs[slot].kind` for fresh slots and from
	 * `committedSlotDedupInfo[slot].kind` for slots already in
	 * committed. Shipped at flush time as the `state.dedup`
	 * wire-visible field. See `DedupForwards`.
	 */
	readonly forwards: DedupForwards;
	/** Op-result slot integers referenced anywhere in local user state (excludes the dead-sentinel). */
	readonly slotsInLocal: ReadonlySet<number>;
	/**
	 * Slots whose op (`LoadEntityOp` / `InvokeOp` / `CreateCallbackOp`)
	 * should be emitted this flush — i.e. slots that appeared in
	 * local user state with a matching `pendingSlotSpec`. The spec's
	 * `kind` dispatches the op in `computeOps`.
	 */
	readonly newWithSpecSlots: ReadonlySet<number>;
	/**
	 * Our-but-broken op-result slots (this session, in local but not
	 * in committed AND no `pendingSlotSpec`, or
	 * `slot > localSlotCounter`). The flush adds these to the
	 * session's `brokenSlots` so the `encodeOpResult` callback emits
	 * `["broken_op_result", slot]` instead of `["op_result", slot]`.
	 * Cross-session stubs aren't listed here — they were already
	 * substituted in-place with the dead-sentinel.
	 */
	readonly brokenSlots: ReadonlySet<number>;
	/**
	 * Callback inbox slots referenced anywhere in local user state,
	 * mapped to the *minimum* offset observed across all stubs for
	 * that slot. Excludes the inbox dead-sentinel (`slot === 0`).
	 *
	 * Used by `computeOps`:
	 *   - keyset diff against `committedInboxSlotSet` →
	 *     `CloseCallbackOp` for slots removed from local.
	 *   - per-slot min-offset feeds the chunk-3 preserve diff for
	 *     callback events.
	 */
	readonly inboxSlotsInLocal: ReadonlyMap<number, number>;
}

const EMPTY_SHORTID_SLOT_MAP: ReadonlyMap<string, number> = Object.freeze(
	new Map<string, number>(),
);
const EMPTY_DEDUP_FORWARDS: DedupForwards = Object.freeze({
	load: EMPTY_SHORTID_SLOT_MAP,
	get_entity_metadata: EMPTY_SHORTID_SLOT_MAP,
	get_service_metadata: EMPTY_SHORTID_SLOT_MAP,
});
const EMPTY_ENVELOPE: LocalEnvelope = Object.freeze({
	dedup: EMPTY_DEDUP_FORWARDS,
});

/**
 * Discriminated union describing what op the reducer should emit
 * for a slot whose stub was newly written into local user state.
 *
 * The spec lives in the session's `pendingSlotSpecs` map from
 * `allocate*` time until the flush walk consumes it (emitting the
 * matching `LoadEntityOp` / `InvokeOp` / `CreateCallbackOp`) or the
 * next reject path wipes it.
 */
export type OpSpec =
	| LoadOpSpec
	| InvokeOpSpec
	| CallbackInvokeOpSpec
	| CallbackOpSpec
	| GetEntityMetadataOpSpec
	| GetServiceMetadataOpSpec;

export interface LoadOpSpec {
	readonly kind: "load";
	readonly shortId: string;
}

/**
 * Spec for an `allocateGetEntityMetadata` call. The shortId is the
 * target entity's workstation short_id; the flush walk emits
 * `GetEntityMetadataOp(slot, shortId)` and writes the
 * `shortId → slot` entry into `entityMetadataDedup` for wire-
 * visible dedup.
 */
export interface GetEntityMetadataOpSpec {
	readonly kind: "get_entity_metadata";
	readonly shortId: string;
}

/**
 * Spec for an `allocateGetServiceMetadata` call. Symmetric to
 * `GetEntityMetadataOpSpec` but for service refs.
 */
export interface GetServiceMetadataOpSpec {
	readonly kind: "get_service_metadata";
	readonly shortId: string;
}

/**
 * The subset of `OpSpec.kind` values that participate in wire-
 * visible `(shortId → slot)` dedup. These are the deterministic /
 * cacheable op kinds: load, get_entity_metadata, get_service_metadata.
 * Invoke and callback don't dedup (every call mints a fresh slot).
 *
 * Used as the key type for the unified `LocalEnvelope.dedup` /
 * `FlushWalkResult.forwards` records and as the discriminator in
 * `CommittedSlotDedupInfo`. Strings match `OpSpec.kind` exactly so
 * the same value flows from spec → forward write → committed inverse
 * → flush-walk dispatch without any mapping table.
 */
export type DedupKind = "load" | "get_entity_metadata" | "get_service_metadata";

/**
 * Iteration order for `DedupKind`. Used by code that needs to walk
 * the three buckets (wire encode/decode, `flush()` reconciliation).
 */
export const DEDUP_KINDS: readonly DedupKind[] = [
	"load",
	"get_entity_metadata",
	"get_service_metadata",
];

const DEDUP_KIND_SET: ReadonlySet<string> = new Set<string>(DEDUP_KINDS);

/**
 * The subset of `OpSpec` arms whose `kind` is a `DedupKind`. These
 * share the `{ kind, shortId }` shape (the three dedup-participating
 * specs `LoadOpSpec`, `GetEntityMetadataOpSpec`,
 * `GetServiceMetadataOpSpec`). Used in the flush walk's narrowed
 * dedup-write branch.
 */
type DedupSpec = LoadOpSpec | GetEntityMetadataOpSpec | GetServiceMetadataOpSpec;

/**
 * Type predicate narrowing an `OpSpec` to a `DedupSpec` (the three
 * cacheable kinds). Used by the flush walk to gate dedup-bucket
 * writes — invoke and callback specs flow through this predicate as
 * `false` and skip the dedup write.
 */
function isDedupSpec(spec: OpSpec): spec is DedupSpec {
	return DEDUP_KIND_SET.has(spec.kind);
}

/**
 * Tagged record describing the dedup namespace + shortId for a
 * slot referenced in *committed* user state. Populated by
 * `reindexCommittedState` from the unified `committed.dedup`
 * forward record; consumed by the flush walk to dispatch back
 * into the right forward bucket for slots that are in committed
 * and no longer have a `pendingSlotSpec`.
 *
 * The forward buckets remain separate (entity / service / load
 * shortIds inhabit distinct namespaces and don't collapse
 * naturally); only the reverse is keyed by slot integer alone,
 * which has a single answer per slot by the single-counter
 * allocation invariant.
 *
 * `kind` matches `OpSpec.kind` exactly — same value flows through.
 */
export interface CommittedSlotDedupInfo {
	readonly kind: DedupKind;
	readonly shortId: string;
}

/**
 * The wire-visible dedup state — three forward `shortId → slot`
 * maps grouped under one parent record keyed by `DedupKind`. On
 * the wire this serializes as a single `dedup` field with three
 * nested buckets:
 *
 * ```jsonc
 * state.dedup: {
 *   load:                  { "<shortId>": <slot>, ... },
 *   get_entity_metadata:   { "<shortId>": <slot>, ... },
 *   get_service_metadata:  { "<shortId>": <slot>, ... },
 * }
 * ```
 *
 * Empty buckets (and the whole `dedup` field, if all three are
 * empty) are omitted by `wirePart` to keep wire-state lean.
 *
 * Per-kind buckets are intentionally separate maps: entity /
 * service / load shortIds inhabit distinct key spaces, and keeping
 * them as separate maps avoids string-key interpolation while
 * still letting code that wants regularity iterate over
 * `DEDUP_KINDS`.
 */
export type DedupForwards = Readonly<Record<DedupKind, ReadonlyMap<string, number>>>;

/**
 * Spec for an `allocateInvoke` call. Args / kwargs are already
 * wire-encoded at allocate time (see `escapeInvokeArgs`) — the
 * walk frees them from the caller's mutable structured values, and
 * the flush walk just hands the wire form to `InvokeOp` verbatim.
 */
export interface InvokeOpSpec {
	readonly kind: "invoke";
	readonly serviceShortId: string;
	readonly methodName: string;
	readonly args: readonly WireValue[];
	readonly kwargs: { readonly [key: string]: WireValue };
}

/**
 * Spec for an `allocateInvoke` call whose *target* is one of this
 * session's own callbacks (`inbox.handle`). Mirrors the wire-level
 * `InvokeOp` / `CallbackInvokeOp` split — a separate kind rather
 * than an optional target field. The handle's slot integer is known
 * at allocate time (even same-tick: `allocateCallback` assigns it
 * immediately), so no later resolution is needed; the server
 * resolves `callback_slot` against its open callbacks including any
 * `CreateCallbackOp` earlier in the same mutation.
 */
export interface CallbackInvokeOpSpec {
	readonly kind: "callback_invoke";
	readonly callbackSlot: number;
	readonly methodName: string;
	readonly args: readonly WireValue[];
	readonly kwargs: { readonly [key: string]: WireValue };
}

/**
 * Spec for an `allocateCallback` call. Carries only the kind tag —
 * the slot integer lives as the map key in `pendingSlotSpecs`, and
 * `CreateCallbackOp` needs nothing else. The flush walk wires this
 * arm in chunk 2.
 */
export interface CallbackOpSpec {
	readonly kind: "callback";
}

export class WorkstationSession implements Session, WorkstationStubSession {
	readonly sessionId: string;
	readonly lifecycle: Atom<SessionLifecycleState>;
	readonly userState: FxtrSessionAtom<UserState | null>;

	/**
	 * Constant-per-session ctx object passed as the second arg to
	 * function-form `userState` updaters. Delegates the slot-
	 * allocation methods to the private `allocateLoad` / `allocateInvoke`
	 * / `allocateCallback` impls. Built once in the constructor —
	 * stable identity across writes so consumers could compare ctx
	 * objects if they wanted (though there's no documented reason to).
	 */
	private readonly updateCtx: FxtrSessionUpdateCtx;

	// --- WorkstationStubSession surface -----------------------------
	//
	// Public read-view atoms that the workstation-side stub classes
	// (`WorkstationOpResultStub`, `WorkstationCallbackInboxStub`)
	// read from. Populated by the reducer:
	//
	//   - `slotFills` / `committedSlotSet` / `brokenSlots`: stage 2,
	//     via `handleFillEvent` (load/invoke result+error events),
	//     the flush walk, and `reindexCommittedState`.
	//   - `inboxLogs` / `committedInboxSlotSet`: stage 6 chunks 2-3.
	//     Initialized empty here so the inbox stub atoms can read
	//     them without needing the chunk-2/3 behavior to be present.

	readonly slotFillsAtom: Atom<ReadonlyMap<number, FillRecord>>;
	readonly committedSlotSetAtom: Atom<ReadonlySet<number>>;
	readonly brokenSlotsAtom: Atom<ReadonlySet<number>>;
	readonly inboxLogsAtom: Atom<ReadonlyMap<number, InboxLog>>;
	readonly committedInboxSlotSetAtom: Atom<ReadonlySet<number>>;
	readonly inboxSubscriptionStateAtom: Atom<ReadonlyMap<number, InboxSubscriptionState>>;

	private readonly slotFillsPrivateAtom: PrimitiveAtom<ReadonlyMap<number, FillRecord>>;
	private readonly committedSlotSetPrivateAtom: PrimitiveAtom<ReadonlySet<number>>;
	private readonly brokenSlotsPrivateAtom: PrimitiveAtom<ReadonlySet<number>>;
	private readonly inboxLogsPrivateAtom: PrimitiveAtom<ReadonlyMap<number, InboxLog>>;
	private readonly committedInboxSlotSetPrivateAtom: PrimitiveAtom<ReadonlySet<number>>;
	private readonly inboxSubscriptionStatePrivateAtom: PrimitiveAtom<
		ReadonlyMap<number, InboxSubscriptionState>
	>;

	/**
	 * Mirror of the server's `preserved_events` sidecar — the set of
	 * fill-event seq numbers the server is holding past the retention
	 * horizon for this session. **Re-derived** on every committed
	 * state change (snapshot apply, foreign state_update absorb, our
	 * own commit) from the new committed user state + `slotFills` +
	 * `committedAckSeq`. See "Preserve-set invariant (no-drift design)"
	 * in `stage2_entity_loading.md`.
	 */
	private readonly committedPreservedSeqsPrivateAtom: PrimitiveAtom<ReadonlySet<number>>;

	/**
	 * Per-session interning of op-result stubs (the receive side of
	 * loads / invokes / metadata ops). Append-only on mint: slot
	 * stubs are stateless wrappers around `(slot, session)`, so a
	 * stale entry is observationally indistinguishable from a fresh
	 * mint at the same slot. See `stage2_entity_loading.md`.
	 *
	 * Kept distinct from a future
	 * `callbackInboxStubs: Map<number, WorkstationCallbackInboxStub>` —
	 * slot integers are session-wide unique, but a single map
	 * mixing kinds would force `allocateLoad`'s dedup lookup to
	 * widen its return type past `OpResultStub<T>`, and a lookup
	 * that hit the wrong kind would be a real bug rather than a
	 * type-only nuisance.
	 */
	private readonly opResultStubs = new Map<number, WorkstationOpResultStub>();

	/**
	 * Specs for slots whose op the reducer might still need to
	 * emit. Added by `allocateLoad` / `allocateInvoke` /
	 * `allocateCallback`; consumed by the flush walk to emit
	 * `LoadEntityOp` (etc.) for slots that became referenced in
	 * local user state.
	 *
	 * **Lifetime is one microtask.** Wiped wholesale at the end of
	 * every flush — emitted specs are now in flight, and any specs
	 * still in the map belong to slots the user allocated but
	 * didn't write into state in the same tick (no longer
	 * salvageable; the next allocate-and-write cycle should mint a
	 * fresh slot). Also wiped on reject (post-flush allocates that
	 * piled up during the in-flight window). This is the
	 * mechanism that enforces the
	 * `Session.allocateLoad`-must-be-written-same-tick contract.
	 */
	private readonly pendingSlotSpecs = new Map<number, OpSpec>();

	/**
	 * The session's op-result dead-sentinel stub (`slot === 0`). Used
	 * as the substitution target when the flush walk encounters
	 * cross-session-smuggled `OpResultStub`s and as the resolution
	 * target for incoming `["broken_op_result", 0]` wire forms.
	 * Minted once at construction.
	 */
	private readonly deadSentinel: WorkstationOpResultStub;

	/**
	 * The session's inbox dead-sentinel stub (`slot === 0`,
	 * `offset === 0`). Parallels `deadSentinel` — used as the
	 * substitution target when the flush walk encounters a
	 * cross-session-smuggled `CallbackInboxStub` and as the resolution
	 * target for incoming `["broken_inbox", 0]` wire forms. Atoms are
	 * hardwired to a closed state (see the
	 * `WorkstationCallbackInboxStub` constructor's `slot === 0`
	 * branch). Minted once at construction.
	 */
	private readonly deadInboxSentinel: WorkstationCallbackInboxStub;

	/**
	 * Monotonic slot allocator. Tracks `max(slots we've ever
	 * allocated, slot_counter observed on the wire)`. Advanced via
	 * `+1` on every `allocateX`; bumped via `max()` on every
	 * `state_update.slot_counter` and `snapshot.last_state.slot_counter`
	 * to stay above any foreign-allocated slots.
	 */
	private localSlotCounter = 0;

	/**
	 * Mirror of the server's `last_state_ack_seq` — the seq value
	 * the server's most recent committed mutation acknowledged.
	 * Events with `seq <= committedAckSeq` are either preserved in
	 * the server's sidecar (and tracked via `committedPreservedSeqs`)
	 * or have been dropped from retention. Events with
	 * `seq > committedAckSeq` are still in `event_log`.
	 *
	 * Updated on snapshot (from `last_state.ack_seq`), foreign
	 * state_update (from `event.ack_seq`), and our own commit
	 * (from the inflight mutation's `ackSeq`).
	 */
	private committedAckSeq = 0;

	/**
	 * Unified `slot → CommittedSlotDedupInfo` reverse index, covering
	 * every slot referenced from any of the three wire-visible
	 * `*Dedup` maps in committed state. Rebuilt by
	 * `reindexCommittedState`. Used by the flush walk to recover a
	 * slot's dedup-kind + shortId when the slot is already in
	 * committed and the user-allocated `pendingSlotSpec` is gone.
	 *
	 * Replaces three parallel `Map<number, string>` reverse indexes
	 * (one per dedup namespace) — the kind discriminator on the
	 * value answers "which forward dedup map owns this slot" with a
	 * single lookup. The forward maps stay separate (the short-id
	 * key spaces are genuinely distinct).
	 *
	 * Single-match-per-slot invariant: a slot integer is minted from
	 * one monotonic counter shared across all op kinds, so at most
	 * one dedup map ever holds it. Foreign mutators sync via
	 * `slot_counter`. This means populating the unified inverse map
	 * never needs to merge conflicting entries.
	 */
	private committedSlotDedupInfo: ReadonlyMap<number, CommittedSlotDedupInfo> = new Map();

	private readonly connection: WorkstationServerConnection;
	/**
	 * The jotai store the session's atoms live in. By default this is
	 * the session's parent connector's store (`connector.store`) —
	 * `createSession` / `attachSession` pass the connector's store
	 * down unless the consumer overrides via the `{ store }` option.
	 * Two `attachSession` calls against the same `session_id` with no
	 * override therefore share the connector's store; component-
	 * internal atoms via `useMemo(() => atom(...))` are still
	 * naturally isolated per component instance, but module-level
	 * atoms intermix. Pass a fresh `createStore()` per session when
	 * stricter isolation is needed.
	 *
	 * The React layer's `<FxtrSessionProvider>` plugs this into a
	 * `<JotaiProvider store={session.store}>` for the panel's scope.
	 */
	readonly store: JotaiStore;
	private readonly lookupEntity: (shortId: string) => WorkstationEntityRefStub;
	private readonly lookupService: (shortId: string) => WorkstationServiceRefStub;
	private readonly markServiceBroken: (shortId: string) => void;

	private readonly lifecycleAtom: PrimitiveAtom<SessionLifecycleState>;
	private readonly committedAtom: PrimitiveAtom<LocalEnvelope>;
	private readonly localAtom: PrimitiveAtom<LocalEnvelope>;

	private handle: SubscriptionHandle | null = null;
	private nextUpdateId = 1;
	private lastProcessedSeq = 0;
	private inflight: Inflight | null = null;
	private pendingTerminate: PendingTerminate | null = null;
	private pendingRollbacks: Array<(info: RollbackInfo) => void> = [];
	private pendingFlushPromise: Promise<void> | null = null;
	private dirty = false;
	private detaching = false;

	constructor(opts: WorkstationSessionOptions) {
		this.connection = opts.connection;
		this.store = opts.store;
		this.sessionId = opts.sessionId;
		this.lookupEntity = opts.lookupEntity;
		this.lookupService = opts.lookupService;
		this.markServiceBroken = opts.markServiceBroken;

		this.lifecycleAtom = atom<SessionLifecycleState>("connecting");
		this.committedAtom = atom<LocalEnvelope>(EMPTY_ENVELOPE);
		this.localAtom = atom<LocalEnvelope>(EMPTY_ENVELOPE);

		// Slot-state atoms populated by the reducer on fill events,
		// state_update absorbs, and flush walks.
		this.slotFillsPrivateAtom = atom<ReadonlyMap<number, FillRecord>>(
			new Map<number, FillRecord>(),
		);
		this.committedSlotSetPrivateAtom = atom<ReadonlySet<number>>(new Set<number>());
		this.brokenSlotsPrivateAtom = atom<ReadonlySet<number>>(new Set<number>());
		this.committedPreservedSeqsPrivateAtom = atom<ReadonlySet<number>>(new Set<number>());
		// Inbox state (chunks 2-3 populate; empty in chunk 1).
		this.inboxLogsPrivateAtom = atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>());
		this.committedInboxSlotSetPrivateAtom = atom<ReadonlySet<number>>(new Set<number>());
		this.inboxSubscriptionStatePrivateAtom = atom<ReadonlyMap<number, InboxSubscriptionState>>(
			new Map<number, InboxSubscriptionState>(),
		);
		// Public read-only views — same pattern as `lifecycle`.
		this.slotFillsAtom = atom((get) => get(this.slotFillsPrivateAtom));
		this.committedSlotSetAtom = atom((get) => get(this.committedSlotSetPrivateAtom));
		this.brokenSlotsAtom = atom((get) => get(this.brokenSlotsPrivateAtom));
		this.inboxLogsAtom = atom((get) => get(this.inboxLogsPrivateAtom));
		this.committedInboxSlotSetAtom = atom((get) => get(this.committedInboxSlotSetPrivateAtom));
		this.inboxSubscriptionStateAtom = atom((get) => get(this.inboxSubscriptionStatePrivateAtom));

		// Public lifecycle is a derived read-only view — jotai
		// rejects writes against a derived atom that has no write fn
		// at runtime, so consumers can't `store.set(session.lifecycle,
		// ...)` even if they smuggle the atom past TypeScript.
		this.lifecycle = atom((get) => get(this.lifecycleAtom));

		// Public userState is a custom writable atom: reads project
		// `.user` out of the local envelope; writes route through the
		// reducer.
		this.userState = atom(
			(get) => extractUser(get(this.localAtom)),
			(get, set, value: FxtrUpdate<UserState | null>, options?: UserStateWriteOptions) => {
				this.handleWrite(get, set as AtomSetter, value, options);
			},
		);

		// Build the per-session ctx once. Arrow-fn wrappers preserve
		// `this` binding through the private allocate methods. The
		// generic call-signatures match `FxtrSessionUpdateCtx` exactly;
		// implementation calls forward into the typed-overload + loose-
		// impl-signature variants the class declares for `allocateInvoke`.
		this.updateCtx = {
			allocateLoad: <T>(ref: EntityRefStub<T>): OpResultStub<T> => this.allocateLoad(ref),
			// `allocateInvoke` carries two public overloads (typed
			// service-ref target / loose callback-handle target); the
			// single forwarding arrow is cast to the overloaded shape.
			allocateInvoke: ((
				svc: ServiceRefStub<unknown> | CallbackHandleStub<unknown>,
				method: string,
				args: readonly InvokeArgValue[],
				kwargs?: { readonly [key: string]: InvokeArgValue },
			): OpResultStub<unknown> =>
				this.allocateInvoke(
					svc as ServiceRefStub<unknown>,
					method,
					args,
					kwargs,
				)) as FxtrSessionUpdateCtx["allocateInvoke"],
			allocateGetEntityMetadata: (ref: EntityRefStub): OpResultStub<EntityMetadata> =>
				this.allocateGetEntityMetadata(ref),
			allocateGetServiceMetadata: (svc: ServiceRefStub<unknown>): OpResultStub<ServiceMetadata> =>
				this.allocateGetServiceMetadata(svc),
			allocateCallback: <R>(): CallbackInboxStub<R> => this.allocateCallback<R>(),
			session: this,
		};

		// Mint and intern the dead-sentinel. Constructed after the
		// session-level atoms above are set up (the stub's
		// constructor reads them by reference, not value).
		this.deadSentinel = new WorkstationOpResultStub(0, this);
		this.opResultStubs.set(0, this.deadSentinel);

		// Inbox dead-sentinel: minted after the inbox-related atoms
		// are in place. Atoms are hardwired to a closed state — see
		// `WorkstationCallbackInboxStub`'s `slot === 0` branch.
		this.deadInboxSentinel = new WorkstationCallbackInboxStub(0, 0, this);

		this.register();
	}

	/**
	 * Resolve a slot integer to its interned `WorkstationOpResultStub`.
	 * Threaded into `unescapeWire` as the `lookupOpResult` callback.
	 *
	 * For `slot === 0` (dead-sentinel slot): always returns the
	 * session's dead-sentinel regardless of `broken`. `["op_result", 0]`
	 * is already rejected by the codec, so the only legitimate caller
	 * here is `["broken_op_result", 0]`.
	 *
	 * For `slot > 0`: looks up in `opResultStubs`; mints (and
	 * interns) a fresh stub if absent. When `broken === true`,
	 * also adds the slot to `brokenSlots` so the stub's `result`
	 * atom reports errored.
	 */
	lookupOpResult(slot: number, broken: boolean): WorkstationOpResultStub {
		if (slot === 0) return this.deadSentinel;
		// Tripwire: under correct protocol behavior we should never
		// see a slot above our local counter — the introducing
		// state_update (which sets the server's slot_counter) always
		// arrives before any reference to its slots. A hit here means
		// out-of-order delivery, a missed counter advance, or a buggy
		// server. Throw loudly rather than silently mint.
		if (slot > this.localSlotCounter) {
			throw new Error(
				`WorkstationSession(${this.sessionId}): wire-decoded slot ${slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation (server announced a slot we haven't seen the state-update for).`,
			);
		}
		let stub = this.opResultStubs.get(slot);
		if (stub === undefined) {
			stub = new WorkstationOpResultStub(slot, this);
			this.opResultStubs.set(slot, stub);
		}
		if (broken) {
			// Decoded `["broken_op_result", slot]` (slot > 0):
			// propagate the brokenness side effect locally so the
			// stub's `result` atom reports errored. Brokenness is
			// permanent — once a slot is in `brokenSlots`, it stays.
			const oldBroken = this.store.get(this.brokenSlotsPrivateAtom);
			if (!oldBroken.has(slot)) {
				const newBroken = new Set(oldBroken);
				newBroken.add(slot);
				this.store.set(this.brokenSlotsPrivateAtom, newBroken);
			}
		}
		return stub;
	}

	/**
	 * Encode an `OpResultStub` to its wire form. Threaded into
	 * `escapeWire` as the `encodeOpResult` callback at flush time.
	 *
	 * Rules:
	 *   - `slot === 0` (the dead-sentinel — used in place of a
	 *     cross-session smuggled stub; see the broken-stub doc on
	 *     `FlushWalkResult`) → `["broken_op_result", 0]`.
	 *   - `slot > 0` AND `slot ∈ brokenSlots` (our slot, flagged
	 *     unhealthy by the flush walk) → `["broken_op_result", slot]`.
	 *   - else → `["op_result", slot]`.
	 *
	 * The flush walk populates `brokenSlots` BEFORE the diff calls
	 * back into `encodeOpResult`, so the brokenSlots check here is
	 * current.
	 */
	encodeOpResult(stub: OpResultStub<unknown>): WireValue {
		if (!(stub instanceof WorkstationOpResultStub)) {
			// Should be unreachable in a well-formed session: the
			// flush walk substitutes cross-session and foreign-
			// subclass stubs with the dead-sentinel before the diff
			// runs. Defensive: emit dead-sentinel on the wire rather
			// than throw, so a single misbehaving stub doesn't tear
			// down an otherwise-valid mutation.
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) encodeOpResult got a non-Workstation OpResultStub (${stub.constructor.name}); emitting dead-sentinel.`,
			);
			return [TAG_BROKEN_OP_RESULT, 0];
		}
		if (stub.slot === 0) return [TAG_BROKEN_OP_RESULT, 0];
		// Our slot brokenness is signaled via `brokenSlots`. The
		// flush walk populates this set BEFORE the diff calls back
		// into `encodeOpResult`, so the check here is current.
		if (this.store.get(this.brokenSlotsPrivateAtom).has(stub.slot)) {
			return [TAG_BROKEN_OP_RESULT, stub.slot];
		}
		return [TAG_OP_RESULT, stub.slot];
	}

	// --- public surface ----------------------------------------------

	/**
	 * Drain pending mutations, then unsubscribe from the session and
	 * mark lifecycle `closed`. The drain waits for any in-flight
	 * `MutateSessionRequest` to commit or reject (so each registered
	 * `onRollback` fires with a definite outcome). After detach
	 * resolves, the session is fully closed. Idempotent.
	 */
	async detach(): Promise<void> {
		if (this.detaching || this.isTerminal()) return;
		this.detaching = true;
		// Drain: keep awaiting until nothing's in flight and no flush
		// is pending. The `detaching` flag blocks new user writes, so
		// the only ways `dirty` becomes true mid-drain are (a) it was
		// already true when detach started, or (b) a post-commit
		// re-flush of accumulated writes. Both converge.
		while (this.inflight !== null || this.pendingFlushPromise !== null) {
			if (this.inflight !== null) {
				// .catch is no-op here; the regular response handlers
				// (.then/.catch attached at flush time) run the
				// commit/reject paths. We just wait for resolution.
				await this.inflight.sendPromise.catch(() => {});
			} else if (this.pendingFlushPromise !== null) {
				await this.pendingFlushPromise;
			}
		}
		this.handle?.close();
		// handle.close() fires onClose("unsubscribed"), which flips
		// lifecycle to `closed`. If the handle was already null
		// (e.g. mid-reset), no unsubscribe is sent, but lifecycle
		// will still need closing.
		if (!this.isTerminal()) this.markClosed();
	}

	// --- Session surface: slot allocation ----------------------------

	/**
	 * Allocate a fresh load slot for `entityRef`, or return an
	 * already-allocated stub for the same `shortId` from this
	 * session's dedup index. The returned stub doesn't fire any
	 * wire op on its own — the reducer derives `LoadEntityOp`
	 * from the diff when the stub first appears in
	 * committed-vs-local user state.
	 *
	 * Thin wrapper over `allocateDeduped("load", …)`. See the
	 * helper's docstring for the two-step dedup + cold-mint logic.
	 */
	private allocateLoad<T>(entityRef: EntityRefStub<T>): OpResultStub<T> {
		return this.allocateDeduped<T>("allocateLoad", "load", entityRef.shortId);
	}

	/**
	 * Allocate a fresh invoke slot. The returned stub doesn't fire
	 * any wire op on its own — when the user writes the stub into
	 * `userState` in the same tick, the reducer's flush walk picks
	 * up `pendingSlotSpecs[slot]` and emits the matching `InvokeOp`.
	 *
	 * Args / kwargs are validated and wire-encoded **at allocate
	 * time** via `escapeInvokeArgs`. This freezes the call's
	 * arguments — later mutations to the original `args` array or
	 * `kwargs` dict don't affect what gets sent, and any forbidden
	 * stub kinds (`OpResultStub`, `CallbackInboxStub`) surface as a
	 * `WireEscapeError` here rather than from a delayed flush.
	 *
	 * Unlike `allocateLoad`, invokes have **no dedup**: every call
	 * mints a fresh slot. Each invoke is a separate nondeterministic
	 * call on the target service.
	 *
	 * Throws if the session isn't writable, if `lifecycle !==
	 * "connected"`, or if `escapeInvokeArgs` rejects.
	 *
	 * Signature mirrors the abstract `Session.allocateInvoke` (typed
	 * protocols via `Method<…>` declarations). The class-level
	 * declaration is required: TS resolves method signatures from
	 * the concrete type when callers hold a `WorkstationSession`
	 * reference rather than a `Session` one, so the runtime
	 * implementation signature would otherwise shadow the typed one.
	 */
	private allocateInvoke<P, M extends MethodNames<P>>(
		svc: ServiceRefStub<P>,
		method: M,
		args: MethodArgs<P, M>,
		kwargs?: MethodKwargs<P, M>,
	): OpResultStub<MethodReturns<P, M, ResultValue>>;
	private allocateInvoke<R>(
		target: CallbackHandleStub<unknown>,
		method: string,
		args: readonly InvokeArgValue[],
		kwargs?: { readonly [key: string]: InvokeArgValue },
	): OpResultStub<R>;
	private allocateInvoke<R>(
		svc: ServiceRefStub<unknown> | CallbackHandleStub<unknown>,
		method: string,
		args: readonly InvokeArgValue[],
		kwargs?: { readonly [key: string]: InvokeArgValue },
	): OpResultStub<R> {
		this.assertWritable("allocateInvoke");
		// Callback-handle target: validate before any slot is burned
		// (mirrors the arg-side `encodeCallbackHandle` checks). The
		// spec branch below emits a `CallbackInvokeOp` instead of an
		// `InvokeOp`.
		if (svc instanceof CallbackHandleStub) {
			if (!(svc instanceof WorkstationCallbackHandleStub)) {
				throw new WireEscapeError(
					`allocateInvoke got a non-Workstation CallbackHandleStub target (${svc.constructor.name}); cannot invoke on a foreign send-side handle.`,
				);
			}
			if (svc.session !== this) {
				throw new WireEscapeError(
					`CallbackHandleStub target belongs to a different session (got ${svc.session.sessionId}, expected ${this.sessionId}); cannot invoke on a cross-session handle.`,
				);
			}
		}
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (lifecycle !== "connected") {
			throw new Error(
				`WorkstationSession(${this.sessionId}): allocateInvoke requires lifecycle "connected" (got "${lifecycle}"). Wait for the session's first snapshot before allocating.`,
			);
		}
		// Encode args + kwargs up front. `escapeInvokeArgs` walks the
		// values, rejects `OpResultStub` / `CallbackInboxStub` with a
		// path-pointed error, and produces frozen-from-here-on
		// `WireValue[]` / `{[k]: WireValue}`. Counter increment + stub
		// mint only happen after encoding succeeds, so a rejection
		// doesn't burn a slot.
		const encoded = escapeInvokeArgs(args, kwargs, {
			encodeEntity: (s) => s.shortId,
			encodeService: (s) => s.shortId,
			encodeOpResult: () => {
				// Defensive — `escapeInvokeArgs` already rejected
				// `OpResultStub` in the pre-walk. Reaching here would
				// indicate a bug in that pre-walk, not legitimate user
				// input.
				throw new WireEscapeError(
					"encodeOpResult unexpectedly reached during invoke arg encoding (escapeInvokeArgs should have rejected up front)",
				);
			},
			encodeCallbackHandle: (stub) => {
				// Cross-session smuggling check: the handle's session
				// backref must match us, else the callee would end up
				// notifying into a different session's slot space.
				// Throw with a clear error rather than silently encoding
				// the stale slot.
				if (!(stub instanceof WorkstationCallbackHandleStub)) {
					throw new WireEscapeError(
						`encodeCallbackHandle got a non-Workstation CallbackHandleStub (${stub.constructor.name}); cannot encode as a workstation send-side handle.`,
					);
				}
				if (stub.session !== this) {
					throw new WireEscapeError(
						`CallbackHandleStub belongs to a different session (got ${stub.session.sessionId}, expected ${this.sessionId}); cannot pass a cross-session handle as an invoke arg.`,
					);
				}
				return ["callback", stub.slot];
			},
		});
		this.localSlotCounter += 1;
		const slot = this.localSlotCounter;
		const stub = new WorkstationOpResultStub<R>(slot, this);
		this.opResultStubs.set(slot, stub);
		if (svc instanceof CallbackHandleStub) {
			// Validated above to be this session's own
			// WorkstationCallbackHandleStub.
			const handle = svc as WorkstationCallbackHandleStub<unknown>;
			this.pendingSlotSpecs.set(slot, {
				kind: "callback_invoke",
				callbackSlot: handle.slot,
				methodName: method,
				args: encoded.args,
				kwargs: encoded.kwargs,
			});
		} else {
			this.pendingSlotSpecs.set(slot, {
				kind: "invoke",
				serviceShortId: svc.shortId,
				methodName: method,
				args: encoded.args,
				kwargs: encoded.kwargs,
			});
		}
		return stub;
	}

	/**
	 * Allocate the receive side of a workstation callback. Returns a
	 * fresh `WorkstationCallbackInboxStub<R>` at offset=0. When the
	 * user writes the stub into `userState` in the same tick, the
	 * reducer's flush walk picks up `pendingSlotSpecs[slot]` and
	 * emits the matching `CreateCallbackOp` (chunk 2 wires the flush
	 * walk; until then this method mints the stub and records a spec
	 * that the flush silently discards).
	 *
	 * Throws if the session isn't writable or `lifecycle !==
	 * "connected"` — same rule as `allocateLoad` / `allocateInvoke`.
	 */
	private allocateCallback<R>(): CallbackInboxStub<R> {
		this.assertWritable("allocateCallback");
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (lifecycle !== "connected") {
			throw new Error(
				`WorkstationSession(${this.sessionId}): allocateCallback requires lifecycle "connected" (got "${lifecycle}"). Wait for the session's first snapshot before allocating.`,
			);
		}
		this.localSlotCounter += 1;
		const slot = this.localSlotCounter;
		const stub = new WorkstationCallbackInboxStub<R>(slot, 0, this);
		this.pendingSlotSpecs.set(slot, { kind: "callback" });
		return stub;
	}

	/**
	 * Allocate a slot for a `GetEntityMetadataOp` against
	 * `entityRef`, or return an already-allocated stub for the same
	 * shortId from this session's metadata dedup index. The returned
	 * stub doesn't fire any wire op on its own — the reducer
	 * derives `GetEntityMetadataOp` from the diff when the stub
	 * first appears in committed-vs-local user state.
	 *
	 * Dedup mirrors `allocateLoad`: scan `pendingSlotSpecs` for a
	 * same-tick prior allocation, then read
	 * `local.entityMetadataDedup[shortId]` (wire-visible so foreign-
	 * mutator and attach-session metadata calls participate).
	 * Distinct slot pool from `loadDedup`: a single entity yields
	 * different slots for load vs. metadata since the result shapes
	 * differ.
	 *
	 * Throws if the session isn't writable or `lifecycle !==
	 * "connected"` — same rule as `allocateLoad`.
	 */
	private allocateGetEntityMetadata(entityRef: EntityRefStub): OpResultStub<EntityMetadata> {
		return this.allocateDeduped<EntityMetadata>(
			"allocateGetEntityMetadata",
			"get_entity_metadata",
			entityRef.shortId,
		);
	}

	/**
	 * Allocate a slot for a `GetServiceMetadataOp` against `svc`,
	 * or return an already-allocated stub from this session's
	 * service-metadata dedup index. Symmetric to
	 * `allocateGetEntityMetadata` but for service refs.
	 */
	private allocateGetServiceMetadata(svc: ServiceRefStub<unknown>): OpResultStub<ServiceMetadata> {
		return this.allocateDeduped<ServiceMetadata>(
			"allocateGetServiceMetadata",
			"get_service_metadata",
			svc.shortId,
		);
	}

	/**
	 * Shared body for all three cacheable allocators (`allocateLoad`,
	 * `allocateGetEntityMetadata`, `allocateGetServiceMetadata`).
	 *
	 * Captures the lifecycle gate + two-step dedup + cold-mint
	 * pattern, parameterized only on which `DedupKind` bucket to
	 * consult and which spec to record. The bucket lookup is direct
	 * (`local.dedup[kind]`) — no callback needed, since the bucket
	 * key is the same string as the spec kind.
	 *
	 * Dedup is two-step:
	 *   1. Scan `pendingSlotSpecs` for a same-tick prior allocation
	 *      of `(kind, shortId)`.
	 *   2. Read `local.dedup[kind][shortId]` — wire-visible, so the
	 *      hit covers our past commits, foreign mutators' commits,
	 *      and the snapshot we received on `attachSession`. Foreign-
	 *      allocated slots get a stub minted lazily.
	 *
	 * On miss, mints a fresh slot via `++localSlotCounter`, interns
	 * the new stub in `opResultStubs`, and records the spec in
	 * `pendingSlotSpecs`. The flush walk derives the
	 * `dedup[kind][shortId] = slot` entry from local user state, so
	 * allocate does not touch `local.dedup` synchronously.
	 *
	 * Throws if the session isn't writable (closed, terminating,
	 * detaching) or if `lifecycle !== "connected"` (allocating
	 * before the first snapshot could mint slots below the server's
	 * eventual `slot_counter`).
	 */
	private allocateDeduped<T>(operation: string, kind: DedupKind, shortId: string): OpResultStub<T> {
		this.assertWritable(operation);
		// Slots are monotonic against the server's slot_counter,
		// which we only know after the first snapshot. Allocating
		// before then could mint slots below the eventual server
		// counter, and the resulting wire op would fail validation.
		// `"stale"` is similarly unsafe: a fresh snapshot after
		// reconnect can advance the counter past any slots we minted
		// while stale. Restrict to `"connected"`.
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (lifecycle !== "connected") {
			throw new Error(
				`WorkstationSession(${this.sessionId}): ${operation} requires lifecycle "connected" (got "${lifecycle}"). Wait for the session's first snapshot before allocating.`,
			);
		}
		// Same-tick dedup: a prior allocate in this tick recorded a
		// spec but the flush walk that would have written
		// `local.dedup[kind][shortId] = slot` hasn't run yet.
		// Scanning `pendingSlotSpecs` keeps tight-loop allocates
		// correct.
		for (const [slot, spec] of this.pendingSlotSpecs) {
			if (spec.kind === kind && spec.shortId === shortId) {
				const stub = this.opResultStubs.get(slot);
				if (stub !== undefined) {
					return stub as OpResultStub<unknown> as OpResultStub<T>;
				}
			}
		}
		// Cross-tick dedup against the wire-visible map. Foreign-
		// allocated slots get a stub minted lazily here (same pattern
		// as `lookupOpResult` uses for wire-decoded slot tags).
		const local = this.store.get(this.localAtom);
		const existingSlot = local.dedup[kind].get(shortId);
		if (existingSlot !== undefined) {
			let stub = this.opResultStubs.get(existingSlot);
			if (stub === undefined) {
				stub = new WorkstationOpResultStub<T>(existingSlot, this);
				this.opResultStubs.set(existingSlot, stub);
			}
			return stub as OpResultStub<unknown> as OpResultStub<T>;
		}
		// Cold path: fresh mint. The flush walk will derive the
		// dedup wire entry from local user state at flush time.
		this.localSlotCounter += 1;
		const slot = this.localSlotCounter;
		const stub = new WorkstationOpResultStub<T>(slot, this);
		this.opResultStubs.set(slot, stub);
		this.pendingSlotSpecs.set(slot, { kind, shortId });
		return stub;
	}

	/**
	 * Destroy the server-side session via a `CloseOp` mutation.
	 * Rides the reducer: the synthetic `local.terminated = true`
	 * field is translated to a `CloseOp` in the next flush; commit
	 * flips lifecycle to `closed`.
	 *
	 * Optional `finalState` writes a parting user-state change in
	 * the same mutation (e.g. a "closed because..." annotation).
	 * Optional `onRollback` fires if the close mutation rejects
	 * (session stays alive, user can re-issue). The returned
	 * promise resolves on commit; rejects with
	 * `MutationRolledBackError` (carrying the `RollbackInfo`) on
	 * rollback.
	 *
	 * Repeated terminate calls (while a terminate is already in
	 * progress, or after `close()`) reject with an error — this is
	 * a caller bug, not a transient condition.
	 */
	terminate(finalState?: UserState, options?: UserStateWriteOptions): Promise<void> {
		// All error paths produce a rejected promise rather than
		// mixing sync throws with async rejections. `assertWritable`
		// catches "already terminating" (via `local.terminated`),
		// "already closed", and "already detaching" — repeat calls
		// of any kind reject.
		return new Promise<void>((resolve, reject) => {
			this.assertWritable("terminate");
			this.pendingTerminate = { resolve, reject };
			const local = this.store.get(this.localAtom);
			// `null` is in `UserState` already (see `handleWrite`'s
			// comment) — terminating without a final user state ships
			// `user: null` alongside `CloseOp` rather than omitting
			// the key, which is harmless on the server.
			this.store.set(this.localAtom, {
				...local,
				user: finalState ?? local.user ?? null,
				terminated: true,
			});
			if (options?.onRollback) this.pendingRollbacks.push(options.onRollback);
			this.dirty = true;
			this.scheduleFlush();
		});
	}

	// --- write API implementation ------------------------------------

	private handleWrite(
		get: AtomGetter,
		set: AtomSetter,
		value: FxtrUpdate<UserState | null>,
		options: UserStateWriteOptions | undefined,
	): void {
		this.assertWritable("write");
		const local = get(this.localAtom);
		// Function-form updaters get `(prev, ctx)`. The ctx exposes
		// per-session slot-allocation so the updater can mint stubs
		// inline (same-tick contract enforced by construction). Static
		// values pass through unchanged.
		const resolvedUser =
			typeof value === "function" ? value(extractUser(local), this.updateCtx) : value;
		// `UserState = StateValue` already includes `null`, so a
		// `set(userState, null)` writes through as `user: null` rather
		// than "clearing" the field. The wire form may end up with
		// `user: null` instead of omitting the key — harmless
		// server-side, and avoids the two-branch envelope dance.
		const next: LocalEnvelope = { ...local, user: resolvedUser };
		set(this.localAtom, next);
		if (options?.onRollback) this.pendingRollbacks.push(options.onRollback);
		this.dirty = true;
		this.scheduleFlush();
	}

	/**
	 * Guard for state-mutating public surface (write, terminate).
	 * Throws on closed / terminating / detaching sessions — those
	 * are all caller bugs (the session has signaled "no more
	 * writes please").
	 */
	private assertWritable(operation: string): void {
		const lifecycle = this.store.get(this.lifecycleAtom);
		if (lifecycle === "closed" || lifecycle === "unavailable") {
			throw new Error(`Cannot ${operation} on a ${lifecycle} session`);
		}
		const local = this.store.get(this.localAtom);
		if (local.terminated === true) {
			throw new Error(`Cannot ${operation} on a terminating session`);
		}
		if (this.detaching) {
			throw new Error(`Cannot ${operation} on a detaching session`);
		}
	}

	// --- flush microtask ---------------------------------------------

	private scheduleFlush(): void {
		if (this.pendingFlushPromise !== null) return;
		// The promise resolves when the scheduled microtask runs to
		// completion (the flush body either sends a mutation or
		// short-circuits). `detach()`'s drain awaits this directly
		// rather than guessing about microtask ordering with
		// `await Promise.resolve()`.
		let resolve!: () => void;
		this.pendingFlushPromise = new Promise<void>((r) => {
			resolve = r;
		});
		queueMicrotask(() => {
			this.pendingFlushPromise = null;
			this.flush();
			resolve();
		});
	}

	private flush(): void {
		if (this.inflight !== null) {
			// A mutation is already in flight. The post-commit handler
			// will reschedule a flush if `dirty` is still set then.
			return;
		}
		if (!this.dirty) return;

		const committed = this.store.get(this.committedAtom);
		let local = this.store.get(this.localAtom);

		// Pre-flush walk: substitute cross-session stubs, classify
		// slots, reconcile loadDedup. Mutates `brokenSlots` as a
		// side effect (via the writes below).
		const walked = this.flushWalk(local);

		// 1. Write back substituted local if any cross-session
		//    stubs were encountered — local atom now matches what
		//    we'll send.
		if (walked.substituted) {
			const substitutedLocal: LocalEnvelope = {
				...local,
				...(walked.substitutedUser !== null ? { user: walked.substitutedUser } : {}),
			};
			this.store.set(this.localAtom, substitutedLocal);
			local = substitutedLocal;
		}

		// 2. Flag our-but-broken slots BEFORE the diff calls
		//    `encodeOpResult` — the encoder reads `brokenSlots` and
		//    emits `["broken_op_result", slot]` for these.
		if (walked.brokenSlots.size > 0) {
			const oldBroken = this.store.get(this.brokenSlotsPrivateAtom);
			let added = false;
			const newBroken = new Set(oldBroken);
			for (const slot of walked.brokenSlots) {
				if (!oldBroken.has(slot)) {
					newBroken.add(slot);
					added = true;
				}
			}
			if (added) this.store.set(this.brokenSlotsPrivateAtom, newBroken);
		}

		// 3. Reconcile the unified `dedup` record into local — drops
		//    stale entries for slots no longer in user state. Loop
		//    over `DEDUP_KINDS` to detect any per-bucket change; if
		//    any bucket changed, install `walked.forwards` wholesale
		//    so subscribers see one atom update per flush.
		{
			let dedupChanged = false;
			for (const kind of DEDUP_KINDS) {
				if (!mapEqual(local.dedup[kind], walked.forwards[kind])) {
					dedupChanged = true;
					break;
				}
			}
			if (dedupChanged) {
				local = { ...local, dedup: walked.forwards };
				this.store.set(this.localAtom, local);
			}
		}

		// 4. Compute patch + ops.
		const patch = this.computePatch(committed, local);
		const ackSeq = this.lastProcessedSeq;
		const ops = this.computeOps(committed, local, walked, ackSeq);

		// 5. End-of-flush `pendingSlotSpecs` wipe. Per
		//    `stage2_entity_loading.md`: emitted specs are now in
		//    flight; non-emitted specs belong to allocate-without-
		//    write entries that won't be re-flushed (rollback wipes
		//    everything; commit wipes too — see handleCommit /
		//    handleReject). Either way, post-flush the map is dead.
		this.pendingSlotSpecs.clear();

		if (patch.length === 0 && ops.length === 0) {
			// Wrote then undid within the same tick — net no-op. Don't
			// ship empty mutations.
			this.dirty = false;
			return;
		}

		const subscription_id = this.handle?.subscriptionId;
		if (subscription_id === undefined || subscription_id < 0) {
			// Handle is gone (after reset, before re-register) or is
			// the pre-rejected sentinel from a closed connection. We
			// can't ship the mutation. Treat as connection-lost.
			this.deliverConnectionError(new ConnectionLostError("session subscription not active"));
			return;
		}

		const updateId = this.nextUpdateId++;
		const request: Omit<MutateSessionRequest, "request_id"> = {
			op: "mutate_session",
			subscription_id,
			ack_seq: ackSeq,
			update_id: updateId,
			patch: patch as readonly MutateSessionRequest["patch"][number][],
			ops,
		};
		const sendPromise = this.connection.sendRequest(request);
		const rollbacks = this.pendingRollbacks;
		this.pendingRollbacks = [];
		this.dirty = false;
		this.inflight = {
			envelope: local,
			updateId,
			ackSeq,
			sendPromise,
			rollbacks,
		};

		sendPromise.then(
			(response) => this.handleMutationResponse(response, updateId),
			(err: unknown) => this.handleSendRejection(err, updateId),
		);
	}

	private computePatch(committed: LocalEnvelope, local: LocalEnvelope): JsonPatchOp[] {
		// Project to the wire-visible shape — strip the synthetic
		// `terminated` field. `diffStateToPatch` diffs whatever you
		// give it; this produces patches naturally rooted at `/user`
		// (or `add /user` when committed has no user yet).
		const before = wirePart(committed);
		const after = wirePart(local);
		const escapeOpts = {
			encodeEntity: (stub: EntityRefStub<unknown>) => stub.shortId,
			encodeService: (stub: ServiceRefStub<unknown>) => stub.shortId,
			encodeOpResult: (stub: OpResultStub<unknown>) => this.encodeOpResult(stub),
			encodeInbox: (stub: CallbackInboxStub<unknown>): WireValue => {
				// `["inbox", slot, offset]` for live cursors;
				// `["broken_inbox", 0]` for the dead-inbox-sentinel
				// (`slot === 0`, used as the substitution target for
				// cross-session smuggled stubs in the flush walk).
				// `WorkstationCallbackInboxStub` carries the slot /
				// offset on the instance; foreign-subclass stubs
				// shouldn't appear in this connector's state.
				if (!(stub instanceof WorkstationCallbackInboxStub)) {
					console.error(
						`[fxtr-ui] WorkstationSession(${this.sessionId}) encodeInbox got a non-Workstation CallbackInboxStub (${stub.constructor.name}); emitting dead-inbox-sentinel.`,
					);
					return ["broken_inbox", 0];
				}
				if (stub.slot === 0) return ["broken_inbox", 0];
				return ["inbox", stub.slot, stub.offset];
			},
			// `encodeCallbackHandle` not needed in this path —
			// `CallbackHandleStub` doesn't appear in `userState` (it's
			// an `InvokeArgValue`-only kind, not a `StateValue` kind),
			// so the diff walk shouldn't encounter one. If it does
			// (defensive bypass via type assertions), `escapeWire`
			// throws with the "encodeCallbackHandle not supplied"
			// message — surfaces the misuse rather than silently
			// emitting a bogus tag.
		};
		return diffStateToPatch(before, after, escapeOpts);
	}

	private computeOps(
		committed: LocalEnvelope,
		local: LocalEnvelope,
		walked: FlushWalkResult,
		ackSeq: number,
	): MutationOp[] {
		const ops: MutationOp[] = [];
		// Track/Untrack derivation. The flush-walk has already done a
		// pass over local, but we still need entity/service refs from
		// committed for the untrack side; reuse `collectStubsInState`
		// for both for simplicity.
		const committedStubs = collectStubsInState(committed.user ?? null);
		const localStubs = collectStubsInState(local.user ?? null);

		for (const shortId of localStubs.entityShortIds) {
			if (!committedStubs.entityShortIds.has(shortId)) {
				ops.push({ kind: "track_entity", short_id: shortId });
			}
		}
		for (const shortId of localStubs.serviceShortIds) {
			if (!committedStubs.serviceShortIds.has(shortId)) {
				ops.push({ kind: "track_service", short_id: shortId });
			}
		}
		for (const shortId of committedStubs.entityShortIds) {
			if (!localStubs.entityShortIds.has(shortId)) {
				ops.push({ kind: "untrack_entity", short_id: shortId });
			}
		}
		for (const shortId of committedStubs.serviceShortIds) {
			if (!localStubs.serviceShortIds.has(shortId)) {
				ops.push({ kind: "untrack_service", short_id: shortId });
			}
		}

		// `LoadEntityOp` for slots newly introduced this mutation
		// that have a load spec. The flush walk identified these and
		// `pendingSlotSpecs` still holds the specs (cleared at the
		// end of `flush`, after we've consumed them here).
		//
		// Emit in slot order: the workstation's server-side
		// validator enforces strict slot monotonicity within a
		// single mutation. The walk populates `newWithSpecSlots` in
		// the order it encountered each slot stub in the user-state
		// tree, which doesn't necessarily match allocation order
		// (e.g. a slot allocated FIRST may live at a JSON path that
		// the walk visits LAST). Sorting here keeps the wire op
		// sequence monotonic regardless of where slot stubs land in
		// the JSON.
		const sortedNewWithSpecSlots = Array.from(walked.newWithSpecSlots).sort((a, b) => a - b);
		for (const slot of sortedNewWithSpecSlots) {
			const spec = this.pendingSlotSpecs.get(slot);
			if (spec === undefined) {
				// Walk classified as new-with-spec but the spec is
				// gone — shouldn't happen; the walk reads the same
				// `pendingSlotSpecs` map. Log and skip rather than
				// crash the mutation.
				console.error(
					`[fxtr-ui] WorkstationSession(${this.sessionId}) computeOps: new-with-spec slot ${slot} has no pendingSlotSpec; flagging broken.`,
				);
				continue;
			}
			if (spec.kind === "load") {
				ops.push({ kind: "load_entity", slot, short_id: spec.shortId });
			} else if (spec.kind === "invoke") {
				ops.push({
					kind: "invoke",
					slot,
					service_short_id: spec.serviceShortId,
					method_name: spec.methodName,
					args: spec.args,
					kwargs: spec.kwargs,
				});
			} else if (spec.kind === "callback_invoke") {
				ops.push({
					kind: "callback_invoke",
					slot,
					callback_slot: spec.callbackSlot,
					method_name: spec.methodName,
					args: spec.args,
					kwargs: spec.kwargs,
				});
			} else if (spec.kind === "callback") {
				ops.push({ kind: "create_callback", slot });
			} else if (spec.kind === "get_entity_metadata") {
				ops.push({ kind: "get_entity_metadata", slot, short_id: spec.shortId });
			} else if (spec.kind === "get_service_metadata") {
				ops.push({ kind: "get_service_metadata", slot, short_id: spec.shortId });
			}
		}

		// Close-callback diff: every slot in `committedInboxSlotSet`
		// that no longer has an inbox stub in local user state was
		// removed by the user; emit `CloseCallbackOp`. The reverse
		// direction (new in local, not in committed) is handled by
		// the new-with-spec loop above via the `callback` spec arm.
		const committedInboxSlots = this.store.get(this.committedInboxSlotSetPrivateAtom);
		for (const slot of committedInboxSlots) {
			if (!walked.inboxSlotsInLocal.has(slot)) {
				ops.push({ kind: "close_callback", slot });
			}
		}

		// Preserve / unpreserve diff. The unified preserve set spans
		// both load/invoke fills (one seq per slot) and callback
		// events (many seqs per slot, from the cursor's offset
		// onward).
		const slotFills = this.store.get(this.slotFillsPrivateAtom);
		const inboxLogs = this.store.get(this.inboxLogsPrivateAtom);
		const committedPreservedSeqs = this.store.get(this.committedPreservedSeqsPrivateAtom);
		const desiredPreservedSeqs = new Set<number>();
		for (const slot of walked.slotsInLocal) {
			if (walked.brokenSlots.has(slot)) continue;
			const fill = slotFills.get(slot);
			if (fill !== undefined && fill.seq <= ackSeq) {
				desiredPreservedSeqs.add(fill.seq);
			}
		}
		for (const [slot, minOffset] of walked.inboxSlotsInLocal) {
			const log = inboxLogs.get(slot);
			if (log === undefined) continue;
			const startIdx = minOffset - log.firstIndex;
			if (startIdx < 0) continue; // local cursor behind firstIndex — invariant violation, skip
			for (let i = startIdx; i < log.events.length; i++) {
				const evt = log.events[i];
				if (evt === undefined) continue;
				if (evt.seq <= ackSeq) {
					desiredPreservedSeqs.add(evt.seq);
				}
			}
		}
		const toPreserve: number[] = [];
		for (const seq of desiredPreservedSeqs) {
			if (!committedPreservedSeqs.has(seq)) toPreserve.push(seq);
		}
		const toUnpreserve: number[] = [];
		for (const seq of committedPreservedSeqs) {
			if (!desiredPreservedSeqs.has(seq)) toUnpreserve.push(seq);
		}
		if (toPreserve.length > 0) {
			toPreserve.sort((a, b) => a - b);
			ops.push({ kind: "preserve_events", seqs: toPreserve });
		}
		if (toUnpreserve.length > 0) {
			toUnpreserve.sort((a, b) => a - b);
			ops.push({ kind: "unpreserve_events", seqs: toUnpreserve });
		}

		if (local.terminated === true && committed.terminated !== true) {
			ops.push({ kind: "close" });
		}
		if (committed.terminated === true) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) flush while committed.terminated=true; this should be unreachable.`,
			);
		}

		return ops;
	}

	// --- response handling -------------------------------------------

	private handleMutationResponse(
		response: MutationCommittedResponse | MutationRejectedResponse,
		ownUpdateId: number,
	): void {
		if (this.inflight?.updateId !== ownUpdateId) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) ignoring stale mutation response (update_id ${ownUpdateId}; current inflight ${this.inflight?.updateId ?? null})`,
			);
			return;
		}
		if (response.type === "mutation_committed") {
			this.handleCommit(response);
		} else {
			this.handleReject(response);
		}
	}

	private handleSendRejection(err: unknown, ownUpdateId: number): void {
		if (this.inflight?.updateId !== ownUpdateId) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) ignoring stale send rejection (update_id ${ownUpdateId})`,
			);
			return;
		}
		const error = err instanceof Error ? err : new Error(String(err));
		this.deliverConnectionError(error);
	}

	private handleCommit(response: MutationCommittedResponse): void {
		if (this.lastProcessedSeq >= response.seq) {
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) commit seq ${response.seq} not strictly greater than lastProcessedSeq ${this.lastProcessedSeq} — dropping. This indicates an ordering bug.`,
			);
			return;
		}
		const inflight = this.inflight;
		if (inflight === null) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) commit response with no inflight envelope — dropping.`,
			);
			return;
		}
		this.store.set(this.committedAtom, inflight.envelope);
		this.lastProcessedSeq = response.seq;
		// Server's `last_state_ack_seq` is now what we sent. Update
		// the local mirror BEFORE reindexing so the preserve-set
		// re-derive sees the right horizon.
		this.committedAckSeq = inflight.ackSeq;
		this.inflight = null;
		this.reindexCommittedState();

		const wasTerminate = inflight.envelope.terminated === true;
		const pendingTerminate = this.pendingTerminate;
		this.pendingTerminate = null;

		if (wasTerminate) {
			// Server-side session is now gone. Lifecycle goes terminal.
			this.markClosed();
			pendingTerminate?.resolve();
			return;
		}

		if (this.dirty) {
			// Accumulated writes during the in-flight; flush again.
			this.scheduleFlush();
		}
	}

	private handleReject(response: MutationRejectedResponse): void {
		// `validation_failed` indicates the server rejected a
		// well-formed mutation for a semantic reason (unknown
		// short_id, non-monotonic slot, preserve-set mismatch from
		// multi-mutator drift, etc.). The reducer can't recover
		// programmatically — log loudly so the cause surfaces.
		if (response.reason === "validation_failed") {
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) mutation rejected as validation_failed: ${response.message}`,
			);
		}
		const info: RollbackInfo = {
			kind: classifyReject(response.reason),
			message: response.message,
		};
		this.rollbackInflight(info);
		if (response.reason === "session_gone") {
			// Server says the session is gone. Treat as terminal.
			this.markClosed();
		}
	}

	private deliverConnectionError(error: Error): void {
		const info: RollbackInfo = {
			kind: classifyTransportError(error),
			message: error.message,
		};
		this.rollbackInflight(info);
	}

	private rollbackInflight(info: RollbackInfo): void {
		const inflight = this.inflight;
		this.inflight = null;
		const committed = this.store.get(this.committedAtom);
		this.store.set(this.localAtom, committed);
		this.dirty = false;
		// Wipe any pendingSlotSpecs accumulated between
		// flush-time-and-now. Their slots' stubs were referenced
		// from local user state that just rolled back, so the slots
		// are dead. `committedSlotDedupInfo` (rebuilt on every
		// committed change), `opResultStubs`
		// (append-only interning), and `localSlotCounter` (never
		// decreases) all stay put. See `stage2_entity_loading.md`
		// "Lifecycle".
		this.pendingSlotSpecs.clear();

		// Forward order: inflight callbacks were registered before
		// pending ones by construction.
		const callbacks = [...(inflight?.rollbacks ?? []), ...this.pendingRollbacks];
		this.pendingRollbacks = [];
		for (const cb of callbacks) {
			try {
				cb(info);
			} catch (err) {
				console.warn(`[fxtr-ui] WorkstationSession(${this.sessionId}) onRollback threw:`, err);
			}
		}

		// If the rolled-back mutation included a terminate, reject
		// the terminate() promise.
		if (inflight?.envelope.terminated === true && this.pendingTerminate !== null) {
			const reject = this.pendingTerminate.reject;
			this.pendingTerminate = null;
			reject(new MutationRolledBackError(info));
		}
	}

	// --- subscription wiring -----------------------------------------

	private register(): void {
		const handler: SubscriptionHandler = {
			onFrame: (frame) => this.onFrame(frame),
			onClose: (reason, error) => this.onClose(reason, error),
		};
		this.handle = this.connection.registerSubscription(
			{ kind: "session", session_id: this.sessionId },
			handler,
		);
	}

	private onFrame(frame: SubscriptionFrame): void {
		if (frame.type === "session_snapshot") {
			this.applySnapshot(frame);
			return;
		}
		if (frame.type === "session_event") {
			this.applyEvent(frame);
			return;
		}
		if (frame.type === "service_broken") {
			// Out-of-band side channel — NOT a reducer event. Forward to
			// the connector's coalesced broken-services set; the interned
			// service-ref stub's `state` flips reactively. Touches none
			// of the committed / local / slot machinery.
			this.markServiceBroken(frame.service_short_id);
			return;
		}
		// Resource frame on a session subscription = routing bug in
		// the connection layer. Log and ignore.
		console.warn(
			`[fxtr-ui] WorkstationSession(${this.sessionId}) got unexpected frame type ${frame.type}`,
		);
	}

	private onClose(reason: SubscriptionCloseReason, error?: Error): void {
		this.handle = null;
		if (reason === "reset") {
			const current = this.store.get(this.lifecycleAtom);
			if (current === "connected") {
				this.store.set(this.lifecycleAtom, "stale");
			}
			this.register();
			return;
		}
		if (reason === "subscribe_failed") {
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) subscribe failed:`,
				error ?? "(no error)",
			);
			this.markUnavailable();
			return;
		}
		// "unsubscribed" | "connector_closed" are terminal-closed.
		this.markClosed();
	}

	private applySnapshot(frame: SessionSnapshotEvent): void {
		// Snapshots are baselines — they set `lastProcessedSeq`
		// unconditionally (a re-snapshot after reset with no foreign
		// mutations during the gap legitimately equals our current
		// value; a strict `>` check would wrongly reject it).
		const last = frame.last_state;
		if (last.seq < this.lastProcessedSeq) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) snapshot seq ${last.seq} less than lastProcessedSeq ${this.lastProcessedSeq}; using it anyway (server is authoritative).`,
			);
		}
		// Advance the slot counter *before* the decode so the
		// `lookupOpResult` tripwire (slot > localSlotCounter) sees
		// the correct upper bound for any slot stubs in this
		// snapshot's state.
		this.advanceSlotCounter(last.slot_counter);
		// Rebuild `slotFills` from the snapshot's authoritative
		// buckets BEFORE absorbing the new state —
		// `reindexCommittedState` reads `slotFills` to compute the
		// preserve mirror.
		this.rebuildSlotFillsFromSnapshot(frame);
		this.absorbState(last.state);
		this.lastProcessedSeq = last.seq;
		// `last_state.ack_seq` is the snapshot's authoritative
		// horizon. Pre-snapshot we may have a stale value; the
		// snapshot is the source of truth.
		this.committedAckSeq = last.ack_seq;
		// Rebuild `inboxLogs` from the snapshot's CallbackEvents,
		// using the just-absorbed committed state's per-slot
		// min-offsets to set each log's `firstIndex`. Runs AFTER
		// `absorbState` (needs the new committed user state) and
		// BEFORE `reindexCommittedState` (which reads `inboxLogs`
		// for the preserve-set re-derive). See the canonical
		// "Inbox event log derivation" invariant on this class's
		// docstring.
		this.rebuildInboxLogsFromSnapshot(frame);
		// Re-derive per-slot subscription connection state from the
		// snapshot's transition events (latest-wins). A transition acked
		// past the retention horizon is absent here — accepted (same
		// retention characteristic as callback messages).
		this.rebuildInboxSubscriptionStatesFromSnapshot(frame);
		this.reindexCommittedState();
		// Advance `lastProcessedSeq` past every event in the snapshot
		// so our next mutation's `ack_seq` covers them all (the
		// snapshot is fully processed by this point — slotFills and
		// inboxLogs are populated). We compute the max directly
		// rather than calling `bumpProcessedSeq` per event: the
		// preserved bucket's seqs are all `<= last.ack_seq <= last.seq`
		// by construction and would each trip the stale-seq warning
		// in `bumpProcessedSeq`, which is meant for catching
		// delta-event ordering bugs in `applyEvent`, not snapshot
		// replay. Subsequent events with `seq > last.seq` (events
		// that arrived between snapshot capture and our receive) are
		// the only ones that actually advance the seq here.
		let maxSeq = last.seq;
		for (const ev of frame.preserved_events) {
			if (ev.seq > maxSeq) maxSeq = ev.seq;
		}
		for (const ev of frame.subsequent_events) {
			if (ev.seq > maxSeq) maxSeq = ev.seq;
		}
		this.lastProcessedSeq = maxSeq;
		this.setLifecycleIfNotTerminal("connected");
	}

	private applyEvent(frame: SessionEventFrame): void {
		const event = frame.event;
		if (event.kind === "state_update") {
			this.applyStateUpdateEvent(event);
			if (event.closed) {
				// Server-side terminal close (could be ours via
				// CloseOp, or a foreign client's). Mark closed.
				this.markClosed();
			}
			return;
		}
		// Non-state event. Load / invoke / metadata result + error
		// events all land in `slotFills` (they're all `OpResultStub`-
		// backed); callback events append to the per-slot `inboxLogs`
		// entry. The four metadata kinds share `handleFillEvent` with
		// load + invoke since they all back the same stub class — the
		// decode path forks based on event kind to read `payload`
		// (load / invoke) vs. `metadata` (the two metadata-result
		// kinds).
		if (
			event.kind === "load_result" ||
			event.kind === "load_error" ||
			event.kind === "invoke_result" ||
			event.kind === "invoke_error" ||
			event.kind === "entity_metadata_result" ||
			event.kind === "entity_metadata_error" ||
			event.kind === "service_metadata_result" ||
			event.kind === "service_metadata_error"
		) {
			this.handleFillEvent(event);
		} else if (event.kind === "callback") {
			this.handleCallbackEvent(event);
		} else if (event.kind === "callback_subscription") {
			this.handleCallbackSubscriptionEvent(event);
		}
		// Bump `lastProcessedSeq` regardless — we've made a decision
		// (either "stored fill" / "appended event" or "dropped, not
		// our concern yet") about this event and the server can
		// advance its retention horizon past it on our next mutation's
		// `ack_seq`.
		this.bumpProcessedSeq(event.seq);
	}

	/**
	 * Record a fill (`load_result` / `invoke_result` /
	 * `entity_metadata_result` / `service_metadata_result`) or an
	 * error (`load_error` / `invoke_error` /
	 * `entity_metadata_error` / `service_metadata_error`) into
	 * `slotFills`. Result / error events for all op kinds share a
	 * common shape — load and invoke use `payload: WireValue`,
	 * metadata uses `metadata: { [k]: WireValue }` — and back the
	 * same `WorkstationOpResultStub` class, so one code path
	 * handles them all. The resulting `FillRecord` doesn't carry
	 * the originating op kind.
	 */
	private handleFillEvent(
		event:
			| LoadResultEvent
			| LoadErrorEvent
			| InvokeResultEvent
			| InvokeErrorEvent
			| EntityMetadataResultEvent
			| EntityMetadataErrorEvent
			| ServiceMetadataResultEvent
			| ServiceMetadataErrorEvent,
	): void {
		if (event.slot > this.localSlotCounter) {
			// Server announced a fill for a slot we haven't seen
			// allocated. Same invariant as the wire-decode tripwire
			// in `lookupOpResult` — throw loudly rather than silently
			// store an orphan.
			throw new Error(
				`WorkstationSession(${this.sessionId}): fill event slot ${event.slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation (server announced a slot we haven't seen the state-update for).`,
			);
		}
		const record: FillRecord = this.decodeFillEvent(event);
		const oldFills = this.store.get(this.slotFillsPrivateAtom);
		const newFills = new Map(oldFills);
		newFills.set(event.slot, record);
		this.store.set(this.slotFillsPrivateAtom, newFills);
	}

	/**
	 * Decode a single fill event into a `FillRecord`. Dispatches:
	 *
	 *   - `load_result` / `invoke_result` → `decodeResultPayload`
	 *     (wraps a `WireValue` payload).
	 *   - `entity_metadata_result` / `service_metadata_result` →
	 *     `decodeMetadataPayload` (wraps a wire-encoded metadata
	 *     dict; produces `EntityMetadata` / `ServiceMetadata`-
	 *     shaped values, but the static type is just `ResultValue`
	 *     since `FillRecord` is op-kind-agnostic; consumers narrow
	 *     via the stub's generic `T`).
	 *   - any `*_error` variant → errored `FillRecord`.
	 */
	private decodeFillEvent(
		event:
			| LoadResultEvent
			| LoadErrorEvent
			| InvokeResultEvent
			| InvokeErrorEvent
			| EntityMetadataResultEvent
			| EntityMetadataErrorEvent
			| ServiceMetadataResultEvent
			| ServiceMetadataErrorEvent,
	): FillRecord {
		if (event.kind === "load_result" || event.kind === "invoke_result") {
			return this.decodeResultPayload(event);
		}
		if (event.kind === "entity_metadata_result" || event.kind === "service_metadata_result") {
			return this.decodeMetadataPayload(event);
		}
		// All `*_error` variants share `message: string`.
		return { kind: "errored", seq: event.seq, message: event.message };
	}

	/**
	 * Append a `CallbackEvent` to the per-slot `inboxLogs` entry.
	 *
	 * **Invariant: the slot must be in `committedInboxSlotSet`
	 * when this fires.** The workstation guarantees this:
	 *   - `CreateCallback` is processed in async pre-commit and
	 *     the bridged service isn't live until commit, so external
	 *     `notify` calls (the only source of `CallbackEvent`s)
	 *     can't run until after the `state_update` adding the slot
	 *     to committed has been sent.
	 *   - Wire seqs are monotonic, so we process that `state_update`
	 *     before any `CallbackEvent` for the slot.
	 *   - `CloseCallback` symmetrically prevents post-close
	 *     emissions (`impl._closed = True` short-circuits `notify`).
	 *
	 * A `CallbackEvent` for a slot not in `committedInboxSlotSet`
	 * indicates a protocol bug (server-side, our reducer, or
	 * out-of-order delivery); we throw to surface it.
	 *
	 * On the first event for a slot, creates the log with
	 * `firstIndex = 0` — events arrive at the server's
	 * event-log-index 0, regardless of the cursor's current
	 * offset. (In the unusual case where the committed cursor is
	 * already past 0, the below-cursor events still get stored
	 * locally but stay invisible to any cursor — `events.atom`'s
	 * slice from `offset - firstIndex` skips them — and the next
	 * `reindex`'s trim advances `firstIndex` to
	 * `committed_min_offset` and drops the below-cursor events.)
	 *
	 * Payload decode goes through result-mode `unescapeWire` (no
	 * `lookupOpResult` / `lookupInbox` — Python services can't
	 * produce client-only stubs in a callback notification).
	 * Decode failures log + skip the event; future events for the
	 * slot still flow normally. The slot-counter tripwire mirrors
	 * `handleFillEvent` and `lookupOpResult`.
	 */
	private handleCallbackEvent(event: CallbackEvent): void {
		if (event.slot > this.localSlotCounter) {
			throw new Error(
				`WorkstationSession(${this.sessionId}): callback event slot ${event.slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation (server announced a slot we haven't seen the state-update for).`,
			);
		}
		if (!this.store.get(this.committedInboxSlotSetPrivateAtom).has(event.slot)) {
			throw new Error(
				`WorkstationSession(${this.sessionId}): callback event slot ${event.slot} is not in committedInboxSlotSet; invariant violation (events should only arrive for slots whose CreateCallback has committed and whose CloseCallback has not yet committed).`,
			);
		}
		let payload: ResultValue;
		try {
			payload = unescapeWire(event.payload, {
				lookupEntity: this.lookupEntity,
				lookupService: this.lookupService,
			});
		} catch (err) {
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) callback event decode failed for slot ${event.slot}:`,
				err,
			);
			return;
		}
		const oldLogs = this.store.get(this.inboxLogsPrivateAtom);
		const existing = oldLogs.get(event.slot);
		const entry: InboxEntry = { seq: event.seq, payload };
		const newLog: InboxLog =
			existing === undefined
				? { firstIndex: 0, events: [entry] }
				: { firstIndex: existing.firstIndex, events: [...existing.events, entry] };
		const newLogs = new Map(oldLogs);
		newLogs.set(event.slot, newLog);
		this.store.set(this.inboxLogsPrivateAtom, newLogs);
	}

	/**
	 * Reduce a `CallbackSubscriptionEvent` into the per-slot
	 * `inboxSubscriptionStates` map (latest-wins): `connected=true`
	 * sets `{state: "connected", reason: null}`, `connected=false`
	 * sets `{state: "broken", reason}`. A slot absent from the map is
	 * "idle".
	 *
	 * Unlike `handleCallbackEvent`, this does **not** require the slot
	 * to be in `committedInboxSlotSet` — a break can race an inbox
	 * close, and connection state is purely informational, so we record
	 * it by slot and let the (possibly already-gone) stub read it. The
	 * slot-counter tripwire still fires for a genuinely out-of-range
	 * slot, mirroring `handleCallbackEvent` / `handleFillEvent`.
	 */
	private handleCallbackSubscriptionEvent(event: CallbackSubscriptionEvent): void {
		if (event.slot > this.localSlotCounter) {
			throw new Error(
				`WorkstationSession(${this.sessionId}): callback_subscription event slot ${event.slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation (server announced a slot we haven't seen the state-update for).`,
			);
		}
		const oldStates = this.store.get(this.inboxSubscriptionStatePrivateAtom);
		const newStates = new Map(oldStates);
		newStates.set(event.slot, {
			state: event.connected ? "connected" : "broken",
			reason: event.connected ? null : (event.reason ?? null),
		});
		this.store.set(this.inboxSubscriptionStatePrivateAtom, newStates);
	}

	/**
	 * Decode a result-event payload (load or invoke) to a structured
	 * value via `unescapeWire`. Payloads are `ResultValue` — Python
	 * services can't produce client-only stubs — so we use the
	 * result-mode opts (no `lookupOpResult` / `lookupInbox`); the
	 * codec will throw on encountering any client-stub tag, which
	 * surfaces as the `errored` `FillRecord` below.
	 *
	 * Decode failures don't throw out of the reducer — instead we
	 * synthesize an errored `FillRecord` so the consumer's `result`
	 * atom transitions to `errored` rather than hanging at `pending`.
	 * The decode error itself is logged.
	 */
	private decodeResultPayload(event: LoadResultEvent | InvokeResultEvent): FillRecord {
		try {
			const value = unescapeWire(event.payload, {
				lookupEntity: this.lookupEntity,
				lookupService: this.lookupService,
			});
			return { kind: "filled", seq: event.seq, value };
		} catch (err) {
			const message = err instanceof Error ? err.message : String(err);
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) decode failed for ${event.kind} on slot ${event.slot}:`,
				err,
			);
			return { kind: "errored", seq: event.seq, message: `decode failed: ${message}` };
		}
	}

	/**
	 * Decode a metadata result event's `metadata` dict via
	 * `unescapeWire`. Same result-mode opts as
	 * `decodeResultPayload` (no `lookupOpResult` / `lookupInbox`
	 * — metadata payloads can't carry client-only stubs). The
	 * decoded value is a `Record<string, ResultValue>` whose ref-
	 * typed fields are `EntityRefStub` instances; consumers
	 * narrow via the `OpResultStub<EntityMetadata | ServiceMetadata>`
	 * generic that `allocateGet*Metadata` returns.
	 *
	 * Decode-failure handling mirrors `decodeResultPayload`:
	 * synthesize an errored `FillRecord` and log.
	 */
	private decodeMetadataPayload(
		event: EntityMetadataResultEvent | ServiceMetadataResultEvent,
	): FillRecord {
		try {
			// The metadata dict is `{[k]: WireValue}` on the wire;
			// `unescapeWire` walks it like any other dict-shaped
			// value and produces a `ResultValue`-shaped dict with
			// stub instances at the tagged-list leaves.
			const value = unescapeWire(event.metadata, {
				lookupEntity: this.lookupEntity,
				lookupService: this.lookupService,
			});
			return { kind: "filled", seq: event.seq, value };
		} catch (err) {
			const message = err instanceof Error ? err.message : String(err);
			console.error(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) metadata decode failed for ${event.kind} on slot ${event.slot}:`,
				err,
			);
			return { kind: "errored", seq: event.seq, message: `decode failed: ${message}` };
		}
	}

	/**
	 * Rebuild `slotFills` from a snapshot's authoritative event
	 * buckets. Replaces the local `slotFills` entirely — the
	 * snapshot is the source of truth for what the server retains
	 * (preserved sidecar + unacked event_log).
	 *
	 * All `OpResultStub`-backed event kinds are surfaced into
	 * `slotFills` — load, invoke, and (entity/service) metadata
	 * result + error variants. Callback events are handled by
	 * `rebuildInboxLogsFromSnapshot` separately.
	 */
	private rebuildSlotFillsFromSnapshot(frame: SessionSnapshotEvent): void {
		const newFills = new Map<number, FillRecord>();
		const events = [...frame.preserved_events, ...frame.subsequent_events];
		for (const ev of events) {
			if (
				ev.kind !== "load_result" &&
				ev.kind !== "load_error" &&
				ev.kind !== "invoke_result" &&
				ev.kind !== "invoke_error" &&
				ev.kind !== "entity_metadata_result" &&
				ev.kind !== "entity_metadata_error" &&
				ev.kind !== "service_metadata_result" &&
				ev.kind !== "service_metadata_error"
			) {
				continue;
			}
			if (ev.slot > this.localSlotCounter) {
				throw new Error(
					`WorkstationSession(${this.sessionId}): snapshot event slot ${ev.slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation.`,
				);
			}
			newFills.set(ev.slot, this.decodeFillEvent(ev));
		}
		this.store.set(this.slotFillsPrivateAtom, newFills);
	}

	/**
	 * Rebuild `inboxLogs` from a snapshot's `CallbackEvent`s. Reads
	 * the absorbed committed user state's per-slot inbox min-offsets
	 * to seed `firstIndex` for each slot — the server preserves
	 * events from `min_offset` onward, so the first preserved
	 * CallbackEvent's absolute index equals the committed
	 * `min_offset`.
	 *
	 * Replaces `inboxLogs` entirely (the snapshot is authoritative).
	 * Events for the same slot are sorted by `seq` to guarantee
	 * delivery order matches the workstation's event log.
	 *
	 * Decode failures log + skip the offending event; the
	 * surrounding events for the slot still populate the log.
	 *
	 * Called AFTER `absorbState` (needs the new committed user
	 * state) and BEFORE `reindexCommittedState` (which reads
	 * `inboxLogs` for the preserve-set re-derive). See the
	 * "Inbox event log derivation" invariant on the class doc.
	 */
	private rebuildInboxLogsFromSnapshot(frame: SessionSnapshotEvent): void {
		const committed = this.store.get(this.committedAtom);
		const stubs = collectStubsInState(committed.user ?? null);
		const committedInboxSlotOffsets = stubs.inboxSlotOffsets;

		const newInboxLogs = new Map<number, { firstIndex: number; events: InboxEntry[] }>();
		const events = [...frame.preserved_events, ...frame.subsequent_events];
		for (const ev of events) {
			if (ev.kind !== "callback") continue;
			if (ev.slot > this.localSlotCounter) {
				throw new Error(
					`WorkstationSession(${this.sessionId}): snapshot callback event slot ${ev.slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation.`,
				);
			}
			let payload: ResultValue;
			try {
				payload = unescapeWire(ev.payload, {
					lookupEntity: this.lookupEntity,
					lookupService: this.lookupService,
				});
			} catch (err) {
				console.error(
					`[fxtr-ui] WorkstationSession(${this.sessionId}) snapshot callback decode failed for slot ${ev.slot}:`,
					err,
				);
				continue;
			}
			let log = newInboxLogs.get(ev.slot);
			if (log === undefined) {
				// First event for this slot in the snapshot. Seed
				// `firstIndex` from committed min-offset; default to
				// 0 for the edge case of an event arriving for a
				// slot not yet in committed (shouldn't happen — the
				// server only preserves events for live slots — but
				// defensive).
				const firstIndex = committedInboxSlotOffsets.get(ev.slot) ?? 0;
				log = { firstIndex, events: [] };
				newInboxLogs.set(ev.slot, log);
			}
			log.events.push({ seq: ev.seq, payload });
		}
		// Ensure each slot's events are in seq order. preserved and
		// subsequent buckets are individually sorted by the server
		// but concatenated may interleave at the boundary.
		for (const log of newInboxLogs.values()) {
			log.events.sort((a, b) => a.seq - b.seq);
		}
		this.store.set(this.inboxLogsPrivateAtom, newInboxLogs);
	}

	/**
	 * Re-derive the per-slot `subscribe_to` connection state from a
	 * snapshot's preserved + subsequent `callback_subscription` events.
	 * Latest-wins per slot (events sorted by seq). Replaces the map
	 * wholesale, so this also serves as the reset on reconnect.
	 */
	private rebuildInboxSubscriptionStatesFromSnapshot(frame: SessionSnapshotEvent): void {
		const events = [...frame.preserved_events, ...frame.subsequent_events]
			.filter((ev): ev is CallbackSubscriptionEvent => ev.kind === "callback_subscription")
			.sort((a, b) => a.seq - b.seq);
		const newStates = new Map<number, InboxSubscriptionState>();
		for (const ev of events) {
			if (ev.slot > this.localSlotCounter) {
				throw new Error(
					`WorkstationSession(${this.sessionId}): snapshot callback_subscription event slot ${ev.slot} exceeds localSlotCounter ${this.localSlotCounter}; invariant violation.`,
				);
			}
			newStates.set(ev.slot, {
				state: ev.connected ? "connected" : "broken",
				reason: ev.connected ? null : (ev.reason ?? null),
			});
		}
		this.store.set(this.inboxSubscriptionStatePrivateAtom, newStates);
	}

	private applyStateUpdateEvent(event: StateUpdateEvent): void {
		// Delta events strictly increment the seq.
		if (event.seq <= this.lastProcessedSeq) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) stale state_update seq ${event.seq} (lastProcessedSeq=${this.lastProcessedSeq}); dropping.`,
			);
			return;
		}
		// Advance counter before decode — see comment in
		// `applySnapshot`.
		this.advanceSlotCounter(event.slot_counter);
		this.absorbState(event.state);
		this.lastProcessedSeq = event.seq;
		// Server's `last_state_ack_seq` is now the mutator's ack —
		// regardless of whether the mutator was us (suppressed) or a
		// foreign client. Update before reindex.
		this.committedAckSeq = event.ack_seq;
		this.reindexCommittedState();
	}

	/**
	 * Bump `localSlotCounter` up to (but never down past) `observed`.
	 * Called from snapshot / state_update paths to stay above any
	 * server-observed slot allocations from foreign mutators or
	 * prior committed mutations.
	 */
	private advanceSlotCounter(observed: number): void {
		if (observed > this.localSlotCounter) {
			this.localSlotCounter = observed;
		}
	}

	/**
	 * Pre-flush walk over `local.user`. Single pass that does four
	 * things at once. See `FlushWalkResult` for the broken-stub
	 * decision tree.
	 *
	 *  1. Substitutes cross-session stubs (`OpResultStub` instances
	 *     whose `session` is not us, or foreign-subclass instances
	 *     we don't own) with the session's dead-sentinel. The
	 *     resulting user state is returned in `substitutedUser` and
	 *     the caller writes it back into the local atom if
	 *     `substituted` is true.
	 *  2. Reconciles `loadDedup` from the walked tree —
	 *     `shortId → slot` for every legitimately-referenced load
	 *     slot.
	 *  3. Classifies each slot integer as `legitimate` /
	 *     `newWithSpec` / `broken`. The flush emits `LoadEntityOp`
	 *     for `newWithSpec` slots and adds `broken` slots to
	 *     `brokenSlots`.
	 *  4. Collects the set of slots referenced in local user state,
	 *     used downstream to compute the desired preserve set.
	 *
	 * Walked recursively through arrays / objects; primitives and
	 * `Uint8Array` pass through. Entity / service refs pass
	 * through unchanged (they're not slot-shaped). The dead-sentinel
	 * (`slot === 0`) is treated as a no-op — it carries no slot
	 * identity.
	 */
	private flushWalk(local: LocalEnvelope): FlushWalkResult {
		const committedSlots = this.store.get(this.committedSlotSetPrivateAtom);
		// One mutable bucket per DedupKind; sealed into a
		// `DedupForwards` value at the end of the walk. The walk
		// writes into `forwards[kind]` based on the spec or the
		// committed reverse-index lookup.
		const forwards: Record<DedupKind, Map<string, number>> = {
			load: new Map<string, number>(),
			get_entity_metadata: new Map<string, number>(),
			get_service_metadata: new Map<string, number>(),
		};
		const slotsInLocal = new Set<number>();
		const newWithSpecSlots = new Set<number>();
		const brokenSlots = new Set<number>();
		const inboxSlotsInLocal = new Map<number, number>();
		let substituted = false;

		const walk = (value: StateValue): StateValue => {
			if (value === null || typeof value !== "object") return value;
			if (value instanceof Uint8Array) return value;
			if (value instanceof EntityRefStub) return value;
			if (value instanceof ServiceRefStub) return value;
			if (value instanceof WorkstationOpResultStub) {
				if (value.session !== this) {
					// Cross-session smuggled stub. Substitute with
					// our dead-sentinel.
					substituted = true;
					return this.deadSentinel;
				}
				const slot = value.slot;
				if (slot === 0) {
					// Our dead-sentinel; pass through. Doesn't
					// participate in any slot bookkeeping.
					return value;
				}
				slotsInLocal.add(slot);
				if (slot > this.localSlotCounter) {
					// Encode-side variant of the `lookupOpResult`
					// tripwire — slot beyond what we've allocated
					// shouldn't exist. Flag broken.
					brokenSlots.add(slot);
					return value;
				}
				const spec = this.pendingSlotSpecs.get(slot);
				if (spec !== undefined) {
					newWithSpecSlots.add(slot);
					// Dispatch on spec kind to route the dedup write
					// into the right bucket. The three DedupKind specs
					// all land in `forwards[spec.kind]`; invoke /
					// callback specs don't participate in dedup and
					// fall through here with no write.
					if (isDedupSpec(spec)) {
						forwards[spec.kind].set(spec.shortId, slot);
					}
					return value;
				}
				if (committedSlots.has(slot)) {
					// Legitimate, already in committed. Recover the
					// dedup-kind + shortId from the unified reverse
					// index (`committedSlotDedupInfo`) and write into
					// the matching forward bucket. Invoke / callback
					// slots aren't in the reverse index (they don't
					// participate in wire-visible dedup) and fall
					// through to a no-op — correct.
					const info = this.committedSlotDedupInfo.get(slot);
					if (info !== undefined) {
						forwards[info.kind].set(info.shortId, slot);
					}
					return value;
				}
				// Slot in local but not in committed and no spec —
				// broken (foreign mutator removed it OR user
				// manually constructed a stub bypassing
				// `allocateLoad`).
				brokenSlots.add(slot);
				return value;
			}
			if (value instanceof WorkstationCallbackInboxStub) {
				if (value.session !== this) {
					// Cross-session smuggled inbox stub. Substitute with
					// our dead-inbox-sentinel; the wire patch emits
					// `["broken_inbox", 0]` (a frontend convention — the
					// server stores wire state opaquely, so the tag is
					// purely for client-side decode of cross-session
					// hazards). Mirrors the op-result substitution
					// above.
					substituted = true;
					return this.deadInboxSentinel;
				}
				const slot = value.slot;
				if (slot === 0) {
					// Inbox dead-sentinel; pass through. No slot
					// bookkeeping (matches op-result sentinel).
					return value;
				}
				// Track per-slot min offset for the close diff +
				// chunk-3 preserve calc. Multiple inbox stubs for
				// the same slot are legal (independent readers).
				const prev = inboxSlotsInLocal.get(slot);
				if (prev === undefined || value.offset < prev) {
					inboxSlotsInLocal.set(slot, value.offset);
				}
				// New-with-spec classification for callback slots:
				// if the slot has a `{kind: "callback"}` pending
				// spec, this is a freshly-allocated callback. The
				// shared `newWithSpecSlots` set carries it through to
				// `computeOps`, which dispatches on spec.kind to
				// emit `CreateCallbackOp`. (Slots that are
				// foreign-allocated or already in committed go
				// through this branch with `spec === undefined` and
				// no op is emitted.)
				const spec = this.pendingSlotSpecs.get(slot);
				if (spec !== undefined && spec.kind === "callback") {
					newWithSpecSlots.add(slot);
				}
				return value;
			}
			if (value instanceof OpResultStub) {
				// Foreign-subclass OpResultStub (not ours). Same
				// substitution as a cross-session stub.
				substituted = true;
				return this.deadSentinel;
			}
			if (value instanceof CallbackInboxStub) {
				// Foreign-subclass CallbackInboxStub (not ours). Same
				// substitution as a cross-session stub — emit the
				// dead-inbox-sentinel.
				substituted = true;
				return this.deadInboxSentinel;
			}
			if (Array.isArray(value)) {
				let changed = false;
				const out: StateValue[] = [];
				for (const item of value) {
					const walked = walk(item);
					if (walked !== item) changed = true;
					out.push(walked);
				}
				return changed ? out : value;
			}
			const obj = value as { readonly [key: string]: StateValue };
			let changed = false;
			const out: Record<string, StateValue> = {};
			for (const key of Object.keys(obj)) {
				const walked = walk(obj[key] as StateValue);
				if (walked !== obj[key]) changed = true;
				out[key] = walked;
			}
			return changed ? out : value;
		};

		const substitutedUser = local.user !== undefined ? walk(local.user) : null;

		return {
			substitutedUser: substitutedUser as UserState | null,
			substituted,
			forwards,
			slotsInLocal,
			newWithSpecSlots,
			brokenSlots,
			inboxSlotsInLocal,
		};
	}

	/**
	 * Re-derive the committed-state-indexed maps
	 * (`committedSlotSet`, `committedPreservedSeqs`) from the
	 * current committed user state, and purge `slotFills` entries
	 * the server has dropped from retention.
	 *
	 * Called after every committed state change (our commit /
	 * foreign state_update absorb / snapshot apply).
	 * `committedAckSeq` must already be up to date when this runs.
	 *
	 * Re-derive rule (see "Preserve-set invariant" in
	 * `stage2_entity_loading.md`):
	 *
	 *     committedPreservedSeqs = {
	 *         slotFills[s].seq
	 *         for s in (slots in new committed user state)
	 *         if slotFills[s] exists AND
	 *            slotFills[s].seq <= committedAckSeq
	 *     }
	 *
	 * Purge rule: drop `slotFills[s]` where
	 *   `seq <= committedAckSeq && seq ∉ committedPreservedSeqs`
	 * — the server has dropped these from its event_log and the
	 * sidecar; we no longer need the local copy.
	 *
	 * Entries with `seq > committedAckSeq` stay (still in the
	 * server's event_log; future preserve op may move them into
	 * the sidecar).
	 */
	private reindexCommittedState(): void {
		const committed = this.store.get(this.committedAtom);
		const stubs = collectStubsInState(committed.user ?? null);
		const newSlotSet = new Set<number>(stubs.opResultSlots);
		const newInboxSlotOffsets = stubs.inboxSlotOffsets;
		const newInboxSlotSet = new Set<number>(newInboxSlotOffsets.keys());

		// Inbox trim-on-commit: walk old `inboxLogs`, drop entries
		// for slots no longer in committed, advance `firstIndex` /
		// slice off front events for slots whose committed min-offset
		// advanced.
		const oldInboxLogs = this.store.get(this.inboxLogsPrivateAtom);
		const newInboxLogs = new Map<number, InboxLog>();
		let inboxLogsChanged = false;
		for (const [slot, log] of oldInboxLogs) {
			const newCommittedMinOffset = newInboxSlotOffsets.get(slot);
			if (newCommittedMinOffset === undefined) {
				// Slot no longer in committed state — drop the log.
				inboxLogsChanged = true;
				continue;
			}
			if (newCommittedMinOffset > log.firstIndex) {
				const dropCount = newCommittedMinOffset - log.firstIndex;
				const newEvents = log.events.slice(dropCount);
				newInboxLogs.set(slot, { firstIndex: newCommittedMinOffset, events: newEvents });
				inboxLogsChanged = true;
			} else if (newCommittedMinOffset < log.firstIndex) {
				// Cursor regressed below firstIndex — would be an
				// invariant violation (cursors are monotonic). Keep
				// the log unchanged; the offending stub's `events`
				// atom will throw on read.
				console.error(
					`[fxtr-ui] WorkstationSession(${this.sessionId}) reindex: committed_min_offset=${newCommittedMinOffset} regressed below inboxLogs[${slot}].firstIndex=${log.firstIndex}; ignoring.`,
				);
				newInboxLogs.set(slot, log);
			} else {
				// No change.
				newInboxLogs.set(slot, log);
			}
		}

		const oldFills = this.store.get(this.slotFillsPrivateAtom);
		const newPreservedSeqs = new Set<number>();
		for (const slot of newSlotSet) {
			const fill = oldFills.get(slot);
			if (fill !== undefined && fill.seq <= this.committedAckSeq) {
				newPreservedSeqs.add(fill.seq);
			}
		}
		// Inbox contribution to the preserve set: for each inbox slot
		// in committed, every event in the (just-trimmed) log at
		// index >= committed_min_offset with seq <= committedAckSeq is
		// what the server is preserving for us. The trim above
		// guarantees `log.firstIndex == newCommittedMinOffset`, so
		// every event in `log.events` is at-or-past min-offset.
		for (const [slot, log] of newInboxLogs) {
			void slot;
			for (const evt of log.events) {
				if (evt.seq <= this.committedAckSeq) {
					newPreservedSeqs.add(evt.seq);
				}
			}
		}

		const newFills = new Map<number, FillRecord>();
		let purged = false;
		for (const [slot, fill] of oldFills) {
			if (fill.seq > this.committedAckSeq || newPreservedSeqs.has(fill.seq)) {
				newFills.set(slot, fill);
			} else {
				purged = true;
			}
		}

		// Rebuild the unified `slot → CommittedSlotDedupInfo` reverse
		// index by walking each `DedupKind` bucket in
		// `committed.dedup`. Used by the flush walk to recover both
		// the kind discriminator and the shortId for slots already
		// in committed user state. At most one bucket holds any
		// given slot (single monotonic counter across op kinds).
		const newCommittedSlotDedupInfo = new Map<number, CommittedSlotDedupInfo>();
		for (const kind of DEDUP_KINDS) {
			for (const [shortId, slot] of committed.dedup[kind]) {
				newCommittedSlotDedupInfo.set(slot, { kind, shortId });
			}
		}

		this.store.set(this.committedSlotSetPrivateAtom, newSlotSet);
		this.store.set(this.committedInboxSlotSetPrivateAtom, newInboxSlotSet);
		this.store.set(this.committedPreservedSeqsPrivateAtom, newPreservedSeqs);
		if (inboxLogsChanged) {
			this.store.set(this.inboxLogsPrivateAtom, newInboxLogs);
		}
		this.committedSlotDedupInfo = newCommittedSlotDedupInfo;
		if (purged) {
			this.store.set(this.slotFillsPrivateAtom, newFills);
		}
	}

	/**
	 * Decode a wire state envelope and install it as the new
	 * committed. Shared between snapshot and delta paths. Does
	 * *not* touch `lastProcessedSeq` — callers handle that with
	 * snapshot/delta-specific rules.
	 */
	private absorbState(rawState: StateUpdateEvent["state"]): void {
		let decoded: StateValue;
		try {
			decoded = unescapeWire(rawState, {
				lookupEntity: this.lookupEntity,
				lookupService: this.lookupService,
				lookupOpResult: (slot: number, broken: boolean) => this.lookupOpResult(slot, broken),
				// No interning for inbox stubs (cursor-shaped values; see
				// the stage-6 design's "Don't intern inbox/handle stubs"
				// decision). Each decode mints a fresh instance carrying
				// `(slot, offset, this)`. For the `broken_inbox` tag
				// (`broken === true`, slot is conventionally 0 for the
				// dead sentinel), route to the per-session
				// `deadInboxSentinel` so all decoded broken-inbox stubs
				// share identity.
				lookupInbox: (slot: number, offset: number, broken: boolean) => {
					if (broken && slot === 0) return this.deadInboxSentinel;
					return new WorkstationCallbackInboxStub(slot, offset, this);
				},
			});
		} catch (err) {
			console.error(`[fxtr-ui] WorkstationSession(${this.sessionId}) wire decode failed:`, err);
			return;
		}

		// committed never carries `terminated` — `state_update.closed`
		// is signaled via the event's own `closed` field, not via the
		// envelope.
		//
		// The unified `dedup` record is read straight from the wire.
		// Its three buckets are the authoritative `shortId → slot`
		// mappings for every load / entity-metadata / service-metadata
		// op in the session (ours, foreign's, and from prior session
		// instances we may have attached to). See
		// `stage2_post1_wire_load_dedup.md` (load wire-visibility) and
		// `stage7pre1_metadata_load.md` (metadata extension).
		const decodedEnv = decodedAsEnvelope(decoded, this.sessionId);
		const newCommitted: LocalEnvelope =
			decodedEnv.user !== undefined
				? { user: decodedEnv.user, dedup: decodedEnv.dedup }
				: { dedup: decodedEnv.dedup };
		this.store.set(this.committedAtom, newCommitted);

		// If we have no inflight and no local edits, bring the local
		// view along to the new committed.
		if (this.inflight === null && !this.dirty) {
			this.store.set(this.localAtom, newCommitted);
		}
	}

	private bumpProcessedSeq(seq: number): void {
		if (seq <= this.lastProcessedSeq) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${this.sessionId}) stale event seq ${seq} (lastProcessedSeq=${this.lastProcessedSeq}); not bumping.`,
			);
			return;
		}
		this.lastProcessedSeq = seq;
	}

	// --- terminal-state helpers --------------------------------------

	private markClosed(): void {
		if (this.isTerminal()) return;
		this.store.set(this.lifecycleAtom, "closed");
	}

	private markUnavailable(): void {
		if (this.isTerminal()) return;
		this.store.set(this.lifecycleAtom, "unavailable");
	}

	private setLifecycleIfNotTerminal(next: SessionLifecycleState): void {
		if (this.isTerminal()) return;
		this.store.set(this.lifecycleAtom, next);
	}

	private isTerminal(): boolean {
		const v = this.store.get(this.lifecycleAtom);
		return v === "closed" || v === "unavailable";
	}
}

// --- helpers ---------------------------------------------------------

/**
 * Shallow `Map`-equality for any `K → V` pair where `V` admits
 * referential equality. Used to detect "did anything actually
 * change?" for the three wire-visible `shortId → slot` dedup maps
 * on `LocalEnvelope`. Generic so the helper isn't tied to one
 * specific field's type.
 */
function mapEqual<K, V>(a: ReadonlyMap<K, V>, b: ReadonlyMap<K, V>): boolean {
	if (a === b) return true;
	if (a.size !== b.size) return false;
	for (const [k, v] of a) {
		if (b.get(k) !== v) return false;
	}
	return true;
}

/** Reads the user-state field out of a `LocalEnvelope`. */
function extractUser(env: LocalEnvelope): UserState | null {
	return env.user ?? null;
}

/**
 * Project a `LocalEnvelope` to its wire-visible part (strips
 * `terminated`; nests the dedup forwards under a single `dedup`
 * field). The diff helper operates on this projection so the
 * synthetic `terminated` field never appears in the patch.
 *
 * `dedup` is wire-visible (per
 * `stage2_post1_wire_load_dedup.md` for load; extended to the two
 * metadata kinds in `stage7pre1_metadata_load.md`) so foreign
 * mutators can read our `shortId → slot` mappings and dedup their
 * own allocations. Wire shape:
 *
 *   `dedup: { load: {...}, get_entity_metadata: {...}, get_service_metadata: {...} }`
 *
 * Empty per-kind buckets are omitted; the whole `dedup` field is
 * omitted if all buckets are empty — keeps wire-state lean for
 * fresh sessions. Bucket values are plain integers; the codec
 * walks them as ordinary JSON.
 */
function wirePart(env: LocalEnvelope): { readonly [key: string]: StateValue } {
	const out: { [key: string]: StateValue } = {};
	if (env.user !== undefined) out.user = env.user;
	const dedup: Record<string, StateValue> = {};
	let anyDedup = false;
	for (const kind of DEDUP_KINDS) {
		const bucket = env.dedup[kind];
		if (bucket.size > 0) {
			dedup[kind] = Object.fromEntries(bucket);
			anyDedup = true;
		}
	}
	if (anyDedup) out.dedup = dedup;
	return out;
}

/**
 * Coerce a freshly-decoded wire envelope into the
 * `{user?, dedup: DedupForwards}` shape the local envelope
 * consumes.
 *
 * The unified `dedup` field is read straight from the wire — its
 * three buckets are the source of truth for `shortId → slot`
 * mappings (cross-mutator, cross-session-attach). Malformed
 * values are warned + skipped individually; a malformed root or
 * missing `dedup` defaults to empty buckets.
 *
 * Used during snapshot / state_update application.
 */
function decodedAsEnvelope(
	decoded: StateValue,
	sessionId: string,
): {
	readonly user?: UserState;
	readonly dedup: DedupForwards;
} {
	if (typeof decoded !== "object" || decoded === null) {
		console.warn(
			`[fxtr-ui] WorkstationSession(${sessionId}) wire state root is not a dict; treating as empty.`,
		);
		return { dedup: EMPTY_DEDUP_FORWARDS };
	}
	if (Array.isArray(decoded)) {
		console.warn(
			`[fxtr-ui] WorkstationSession(${sessionId}) wire state root is an array; treating as empty.`,
		);
		return { dedup: EMPTY_DEDUP_FORWARDS };
	}
	if (decoded instanceof Uint8Array) {
		console.warn(
			`[fxtr-ui] WorkstationSession(${sessionId}) wire state root is bytes; treating as empty.`,
		);
		return { dedup: EMPTY_DEDUP_FORWARDS };
	}
	const obj = decoded as { readonly [key: string]: StateValue };
	const user = obj.user;
	const dedup = decodeDedupForwards(obj.dedup, sessionId);
	return user === undefined ? { dedup } : { user, dedup };
}

/**
 * Parse a wire-decoded `dedup` value into a `DedupForwards`. The
 * wire shape is a dict keyed by `DedupKind` whose values are
 * `shortId → slot` dicts. Unknown keys and malformed buckets are
 * warned + skipped individually; missing or non-dict `dedup`
 * defaults to all-empty buckets.
 */
function decodeDedupForwards(raw: StateValue | undefined, sessionId: string): DedupForwards {
	if (raw === undefined) return EMPTY_DEDUP_FORWARDS;
	if (typeof raw !== "object" || raw === null || Array.isArray(raw) || raw instanceof Uint8Array) {
		console.warn(
			`[fxtr-ui] WorkstationSession(${sessionId}) wire state.dedup is not a dict; treating as empty.`,
		);
		return EMPTY_DEDUP_FORWARDS;
	}
	const obj = raw as { readonly [key: string]: StateValue };
	const out: Record<DedupKind, ReadonlyMap<string, number>> = {
		load: EMPTY_SHORTID_SLOT_MAP,
		get_entity_metadata: EMPTY_SHORTID_SLOT_MAP,
		get_service_metadata: EMPTY_SHORTID_SLOT_MAP,
	};
	for (const kind of DEDUP_KINDS) {
		out[kind] = decodeShortIdSlotMap(obj[kind], sessionId, `dedup.${kind}`);
	}
	// Warn on unrecognized top-level dedup keys — useful diagnostic if
	// a new dedup kind gets added server-side without a client update.
	for (const key of Object.keys(obj)) {
		if (!DEDUP_KIND_SET.has(key)) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${sessionId}) wire state.dedup.${JSON.stringify(key)} is not a known DedupKind; ignoring.`,
			);
		}
	}
	return out;
}

/**
 * Parse a wire-decoded `shortId → slot` map value into a
 * `Map<string, number>`. Shared by all three dedup fields
 * (`loadDedup`, `entityMetadataDedup`, `serviceMetadataDedup`).
 * The wire form is a plain object whose keys are short_ids and
 * whose values are slot integers. Bad entries (non-integer
 * values, etc.) are skipped with a warn rather than crashing the
 * absorb path — a single garbage entry shouldn't take down dedup
 * for the rest of the session.
 */
function decodeShortIdSlotMap(
	raw: StateValue | undefined,
	sessionId: string,
	fieldName: string,
): ReadonlyMap<string, number> {
	if (raw === undefined) return EMPTY_SHORTID_SLOT_MAP;
	if (typeof raw !== "object" || raw === null || Array.isArray(raw) || raw instanceof Uint8Array) {
		console.warn(
			`[fxtr-ui] WorkstationSession(${sessionId}) wire state.${fieldName} is not a dict; treating as empty.`,
		);
		return EMPTY_SHORTID_SLOT_MAP;
	}
	// `OpResultStub` / `EntityRefStub` instances at this root are also
	// not dicts — the same warn path applies. Class-instance check
	// against the prototype chain via Object.keys() works since these
	// classes don't override iteration in a way that conflicts.
	const obj = raw as { readonly [key: string]: StateValue };
	const out = new Map<string, number>();
	for (const key of Object.keys(obj)) {
		const v = obj[key];
		if (typeof v !== "number" || !Number.isInteger(v)) {
			console.warn(
				`[fxtr-ui] WorkstationSession(${sessionId}) wire state.${fieldName}[${JSON.stringify(key)}] is not an integer slot; skipping.`,
			);
			continue;
		}
		out.set(key, v);
	}
	return out.size === 0 ? EMPTY_SHORTID_SLOT_MAP : out;
}

/**
 * Classify a server-reject reason as retryable (transient,
 * caller could rebase against current state and re-issue) or
 * permanent (mutation structurally invalid, session dead, etc.).
 */
function classifyReject(reason: MutationRejectReason): "retryable" | "permanent" {
	switch (reason) {
		case "stale_ack":
		case "conflict":
		case "session_busy":
			return "retryable";
		case "validation_failed":
		case "callback_setup_failed":
		case "session_gone":
			return "permanent";
	}
}

/**
 * Classify a transport-layer error as retryable or permanent.
 * `ConnectionLostError` is retryable (transport may recover);
 * `ConnectionClosedError` is permanent (connector was closed);
 * `ProtocolError` is permanent (indicates a bug — retrying won't
 * help); anything else is permanent (conservative default).
 */
function classifyTransportError(err: Error): "retryable" | "permanent" {
	if (err instanceof ConnectionLostError) return "retryable";
	if (err instanceof ConnectionClosedError) return "permanent";
	if (err instanceof ProtocolError) return "permanent";
	return "permanent";
}
