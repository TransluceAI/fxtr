// Per-session inflated sub-panel rendered below the SessionsPanel
// list when a session row is checked. Four sections, top-to-bottom:
//
//   - "Last state": collapsible JSON tree of the most recent
//     ``state_update.state``, plus the post-mutation pin sets.
//   - "Session pins": filtered slices of the global services /
//     entities tables (rows where ``pinned_by_sessions`` includes
//     this session UUID), with refcount badges from the debug
//     subscription and an explicit-pin / retained-event label.
//   - "Open slots" (5e.2): three small tables surfacing pending
//     workstation-side operations — open callbacks, in-flight
//     invokes, in-flight loads. Driven by the debug subscription's
//     ``open_callbacks`` / ``open_invokes`` / ``open_loads`` fields.
//   - "Event log": timeline of the unacked-event suffix plus
//     ``last_state``, with state-updates rendered twice — once as a
//     pending row at their ack_seq and once as a one-line committed
//     marker at their seq. Rows whose event seq is <=
//     ``last_state.ack_seq`` render grayed-out, *unless* the seq is
//     in the debug snapshot's ``preserved_events`` set — those
//     render in orange (8a/8d) to surface "this event is past the
//     horizon but the session is explicitly holding it alive".
//     ``CallbackEvent`` rows render through the same single-event
//     renderer that handles invoke-result / load-result.
//     ``LoadResultSilentEvent`` rows (8c) collapse to a single
//     "loaded silently" line — there's no payload to expand.

import { useEffect, useState } from "react";
import {
  EntityRecord,
  ServiceRecord,
  SessionDebugSnapshotEvent,
  SessionEvent,
  SessionStreamSnapshot,
  StateUpdateEvent,
  WorkstationDebugClient,
} from "../ws_client";
import { JsonTree } from "./JsonTree";

export function SessionSubPanel(props: {
  sessionId: string;
  client: WorkstationDebugClient;
  services: ServiceRecord[];
  entities: EntityRecord[];
}): JSX.Element {
  const [stream, setStream] = useState<SessionStreamSnapshot | null>(null);
  const [debug, setDebug] = useState<SessionDebugSnapshotEvent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setStream(null);
    setDebug(null);
    setError(null);
    let cancelled = false;
    let unsubMain: (() => void) | null = null;
    let unsubDebug: (() => void) | null = null;
    try {
      unsubMain = props.client.subscribeSession(props.sessionId, (snap) => {
        if (!cancelled) setStream(snap);
      });
      unsubDebug = props.client.subscribeSessionDebug(
        props.sessionId,
        (snap) => {
          if (!cancelled) setDebug(snap);
        },
      );
    } catch (e) {
      setError(`subscribe failed: ${e}`);
    }
    return () => {
      cancelled = true;
      unsubMain?.();
      unsubDebug?.();
    };
  }, [props.client, props.sessionId]);

  return (
    <div
      style={{
        border: "1px solid #ccc",
        margin: "8px 0",
        padding: 8,
        background: "#fafafa",
      }}
    >
      <div style={{ marginBottom: 8 }}>
        <strong>session</strong> <code>{props.sessionId}</code>
        {stream && (
          <span style={{ marginLeft: 12, color: "#666" }}>
            seq=<code>{stream.last_state.seq}</code>{" "}
            ack_seq=<code>{stream.last_state.ack_seq}</code>{" "}
            slot_counter=<code>{debug?.slot_counter ?? stream.last_state.slot_counter}</code>
            {stream.last_state.closed && (
              <span style={{ marginLeft: 8, color: "crimson" }}>closed</span>
            )}
          </span>
        )}
      </div>
      {error && <div style={{ color: "crimson" }}>{error}</div>}
      {!stream && !error && <em>loading…</em>}
      {stream && (
        <>
          <LastStateSection lastState={stream.last_state} />
          <PinsSection
            sessionId={props.sessionId}
            lastState={stream.last_state}
            debug={debug}
            services={props.services}
            entities={props.entities}
          />
          <OpenSlotsSection debug={debug} />
          <EventLogSection stream={stream} debug={debug} />
        </>
      )}
    </div>
  );
}

// --- Last state ---------------------------------------------------

function LastStateSection(props: { lastState: StateUpdateEvent }): JSX.Element {
  return (
    <section style={{ marginTop: 8 }}>
      <h4 style={{ margin: "4px 0" }}>Last state</h4>
      <JsonTree value={props.lastState.state} />
    </section>
  );
}

// --- Pins ---------------------------------------------------------

function PinsSection(props: {
  sessionId: string;
  lastState: StateUpdateEvent;
  debug: SessionDebugSnapshotEvent | null;
  services: ServiceRecord[];
  entities: EntityRecord[];
}): JSX.Element {
  const explicitEntities = new Set(props.lastState.tracked_entities);
  const explicitServices = new Set(props.lastState.tracked_services);

  const pinnedEntities = props.entities.filter((e) =>
    e.pinned_by_sessions.includes(props.sessionId),
  );
  const pinnedServices = props.services.filter((s) =>
    s.pinned_by_sessions.includes(props.sessionId),
  );

  return (
    <section style={{ marginTop: 8 }}>
      <h4 style={{ margin: "4px 0" }}>Session pins</h4>
      <PinTable
        title="entities"
        rows={pinnedEntities.map((e) => ({
          short_id: e.short_id,
          extra: e.fxtr_id,
          refcount: props.debug?.entity_refcounts[e.short_id] ?? null,
          source: explicitEntities.has(e.short_id)
            ? "explicit pin"
            : "retained-event only",
        }))}
        extraLabel="fxtr_id"
      />
      <PinTable
        title="services"
        rows={pinnedServices.map((s) => ({
          short_id: s.short_id,
          extra: s.service_type_id,
          refcount: props.debug?.service_refcounts[s.short_id] ?? null,
          source: explicitServices.has(s.short_id)
            ? "explicit pin"
            : "retained-event only",
        }))}
        extraLabel="type id"
      />
    </section>
  );
}

function PinTable(props: {
  title: string;
  rows: Array<{
    short_id: string;
    extra: string;
    refcount: number | null;
    source: string;
  }>;
  extraLabel: string;
}): JSX.Element {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ color: "#666" }}>
        {props.title} ({props.rows.length})
      </div>
      <table border={1} cellPadding={3} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th>short id</th>
            <th>{props.extraLabel}</th>
            <th>refcount</th>
            <th>source</th>
          </tr>
        </thead>
        <tbody>
          {props.rows.map((r) => (
            <tr key={r.short_id}>
              <td>{r.short_id}</td>
              <td title={r.extra}>{r.extra.slice(0, 16)}…</td>
              <td>
                <code>{r.refcount ?? "?"}</code>
              </td>
              <td>{r.source}</td>
            </tr>
          ))}
          {props.rows.length === 0 && (
            <tr>
              <td colSpan={4}>
                <em>no pins</em>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// --- Open slots (5e.2) -------------------------------------------

function OpenSlotsSection(props: {
  debug: SessionDebugSnapshotEvent | null;
}): JSX.Element {
  const cb = props.debug?.open_callbacks ?? {};
  const inv = props.debug?.open_invokes ?? {};
  const loads = props.debug?.open_loads ?? {};
  const cbRows = Object.entries(cb)
    .map(([slot, shortId]) => ({ slot: Number(slot), shortId }))
    .sort((a, b) => a.slot - b.slot);
  const invRows = Object.entries(inv)
    .map(([slot, info]) => ({
      slot: Number(slot),
      shortId: info.service_short_id,
      method: info.method_name,
    }))
    .sort((a, b) => a.slot - b.slot);
  const loadRows = Object.entries(loads)
    .map(([slot, shortId]) => ({ slot: Number(slot), shortId }))
    .sort((a, b) => a.slot - b.slot);
  return (
    <section style={{ marginTop: 8 }}>
      <h4 style={{ margin: "4px 0" }}>Open slots</h4>
      <OpenSlotTable
        title="callbacks"
        cols={["slot", "service short id"]}
        rows={cbRows.map((r) => [String(r.slot), r.shortId])}
      />
      <OpenSlotTable
        title="invokes"
        cols={["slot", "service short id", "method"]}
        rows={invRows.map((r) => [String(r.slot), r.shortId, r.method])}
      />
      <OpenSlotTable
        title="loads"
        cols={["slot", "entity short id"]}
        rows={loadRows.map((r) => [String(r.slot), r.shortId])}
      />
    </section>
  );
}

function OpenSlotTable(props: {
  title: string;
  cols: string[];
  rows: string[][];
}): JSX.Element {
  return (
    <div style={{ marginBottom: 6 }}>
      <div style={{ color: "#666" }}>
        {props.title} ({props.rows.length})
      </div>
      <table border={1} cellPadding={3} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {props.cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>
                  <code>{cell}</code>
                </td>
              ))}
            </tr>
          ))}
          {props.rows.length === 0 && (
            <tr>
              <td colSpan={props.cols.length}>
                <em>none open</em>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// --- Event log ----------------------------------------------------

// Each row carries a (primary, secondary) sort key:
//   - ``primary`` is the row's timeline position
//     (``ack_seq`` for a "requested" state-update row, ``seq`` for
//      committed markers and result/error rows).
//   - ``secondary`` is the event's ``seq`` so that multiple rows
//     sharing a primary (typically multiple "requested" rows with
//     the same ack_seq) are ordered by the seq they ended up
//     committing at — a stable, intuitive ordering even when the
//     ``events`` list arrives out of seq order (e.g. last_state
//     prepended during dedup).
type TimelineRow =
  | {
      kind: "state_update_requested";
      primary: number;
      secondary: number;
      event: StateUpdateEvent;
    }
  | {
      kind: "state_update_committed";
      primary: number;
      secondary: number;
      event: StateUpdateEvent;
    }
  | {
      kind: "single";
      primary: number;
      secondary: number;
      event: Exclude<SessionEvent, StateUpdateEvent>;
    };

function EventLogSection(props: {
  stream: SessionStreamSnapshot;
  debug: SessionDebugSnapshotEvent | null;
}): JSX.Element {
  const ackHorizon = props.stream.last_state.ack_seq;
  const preservedSeqs = new Set(props.debug?.preserved_events ?? []);
  // Events stay in the UI even after they've been retention-pruned
  // server-side — they're locally cached observations, useful for
  // looking back. The gray-out coloring already conveys "past the
  // horizon" (i.e. would not survive a fresh resubscribe). Preserve
  // / unpreserve flips a row between orange and gray without
  // removing it.
  const rows = buildTimeline(props.stream);

  return (
    <section style={{ marginTop: 8 }}>
      <h4 style={{ margin: "4px 0" }}>Event log</h4>
      {rows.length === 0 && (
        <div style={{ color: "#888" }}>
          <em>no events yet</em>
        </div>
      )}
      <ol
        style={{
          listStyle: "none",
          margin: 0,
          padding: 0,
          fontSize: "0.9em",
        }}
      >
        {rows.map((r, i) => (
          <TimelineRowView
            key={i}
            row={r}
            ackHorizon={ackHorizon}
            preservedSeqs={preservedSeqs}
          />
        ))}
      </ol>
    </section>
  );
}

function buildTimeline(stream: SessionStreamSnapshot): TimelineRow[] {
  // Fold last_state into the events list. ``stream.events`` is
  // non-state-only (the workstation never persists state_updates in
  // event_log), so the two are disjoint by construction — no dedup
  // needed. The synthetic seq=0 last_state used for a session with
  // no committed mutations is *not* a real event, so we omit it —
  // the empty-rows path then renders "no events yet".
  const combined: SessionEvent[] = [];
  if (stream.last_state.seq > 0) {
    combined.push(stream.last_state);
  }
  for (const e of stream.events) {
    combined.push(e);
  }

  const rows: TimelineRow[] = [];
  for (const e of combined) {
    if (e.kind === "state_update") {
      rows.push({
        kind: "state_update_requested",
        primary: e.ack_seq,
        secondary: e.seq,
        event: e,
      });
      rows.push({
        kind: "state_update_committed",
        primary: e.seq,
        secondary: e.seq,
        event: e,
      });
    } else {
      rows.push({
        kind: "single",
        primary: e.seq,
        secondary: e.seq,
        event: e,
      });
    }
  }

  // Sort lexicographically by (primary, secondary), with a small
  // kind-priority tiebreaker so committed / single rows render
  // before a "requested" row that happens to share the same
  // (primary, secondary) (which only really happens for the
  // synthetic seq=0 last_state on a fresh session).
  const kindRank = {
    state_update_committed: 0,
    single: 0,
    state_update_requested: 1,
  } as const;
  rows.sort((a, b) => {
    if (a.primary !== b.primary) return a.primary - b.primary;
    if (a.secondary !== b.secondary) return a.secondary - b.secondary;
    return kindRank[a.kind] - kindRank[b.kind];
  });
  return rows;
}

function TimelineRowView(props: {
  row: TimelineRow;
  ackHorizon: number;
  preservedSeqs: Set<number>;
}): JSX.Element {
  // Three-way classification:
  //   - preserved: seq is explicitly held past the horizon → orange.
  //     Takes priority over grayed (a preserved event is always
  //     past-horizon by construction, but we want to surface it as
  //     alive, not stale).
  //   - grayed: past-horizon (seq <= ackHorizon), not preserved →
  //     stale, the standard 4d/e gray.
  //   - normal: above the horizon → default color.
  const isPreserved = props.preservedSeqs.has(props.row.event.seq);
  const grayed = !isPreserved && props.row.event.seq <= props.ackHorizon;
  const baseStyle: React.CSSProperties = {
    padding: "2px 4px",
    borderBottom: "1px dotted #eee",
    color: isPreserved ? "#c60" : grayed ? "#888" : undefined,
  };
  switch (props.row.kind) {
    case "state_update_requested":
      return (
        <li style={baseStyle}>
          <RequestedStateUpdateRow event={props.row.event} />
          {isPreserved && <PreservedBadge />}
        </li>
      );
    case "state_update_committed":
      return (
        <li style={baseStyle}>
          <span style={{ color: "#06c" }}>seq={props.row.event.seq}</span>{" "}
          state_update committed{" "}
          <span style={{ color: "#888" }}>
            (acked up to {props.row.event.ack_seq})
          </span>
          {isPreserved && <PreservedBadge />}
        </li>
      );
    case "single":
      return (
        <li style={baseStyle}>
          <SingleEventRow event={props.row.event} />
          {isPreserved && <PreservedBadge />}
        </li>
      );
  }
}

function PreservedBadge(): JSX.Element {
  return (
    <span
      style={{
        marginLeft: 8,
        padding: "0 6px",
        border: "1px solid #c60",
        borderRadius: 3,
        fontSize: "0.85em",
        color: "#c60",
      }}
    >
      preserved
    </span>
  );
}

function RequestedStateUpdateRow(props: {
  event: StateUpdateEvent;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  return (
    <span>
      <span style={{ color: "#06c" }}>
        ack={props.event.ack_seq}@{props.event.seq}
      </span>{" "}
      <span
        style={{ cursor: "pointer", userSelect: "none" }}
        onClick={() => setOpen(!open)}
      >
        {open ? "▾" : "▸"} state_update requested
      </span>
      {props.event.closed && (
        <span style={{ color: "#888" }}> (closed)</span>
      )}
      {open && (
        <div style={{ marginLeft: 16, marginTop: 4 }}>
          <div>
            tracked_entities: <code>[{props.event.tracked_entities.join(", ")}]</code>
          </div>
          <div>
            tracked_services: <code>[{props.event.tracked_services.join(", ")}]</code>
          </div>
          <div>
            state: <JsonTree value={props.event.state} />
          </div>
        </div>
      )}
    </span>
  );
}

function SingleEventRow(props: {
  event: Exclude<SessionEvent, StateUpdateEvent>;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const e = props.event;
  const isError =
    e.kind === "load_error" ||
    e.kind === "invoke_error" ||
    e.kind === "entity_metadata_error" ||
    e.kind === "service_metadata_error";
  // 8c: LoadResultSilentEvent has no body — render compactly with no
  // expand/collapse affordance.
  const isSilent = e.kind === "load_result_silent";
  return (
    <span>
      <span style={{ color: "#06c" }}>seq={e.seq}</span>{" "}
      {isSilent ? (
        <span>
          <span>{e.kind}</span>{" "}
          <span style={{ color: "#888" }}>slot={e.slot}</span>
        </span>
      ) : (
        <>
          <span
            style={{ cursor: "pointer", userSelect: "none" }}
            onClick={() => setOpen(!open)}
          >
            {open ? "▾" : "▸"}{" "}
            <span style={{ color: isError ? "crimson" : undefined }}>
              {e.kind}
            </span>
          </span>{" "}
          <span style={{ color: "#888" }}>slot={e.slot}</span>
          {open && (
            <div style={{ marginLeft: 16, marginTop: 4 }}>
              {"payload" in e ? (
                <>
                  payload: <JsonTree value={e.payload} />
                </>
              ) : "metadata" in e ? (
                <>
                  metadata: <JsonTree value={e.metadata} />
                </>
              ) : (
                <>
                  message: <code>{e.message}</code>
                </>
              )}
            </div>
          )}
        </>
      )}
    </span>
  );
}
