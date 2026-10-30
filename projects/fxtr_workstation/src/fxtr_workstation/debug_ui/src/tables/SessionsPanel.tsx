import { useEffect, useMemo, useState } from "react";
import {
  EntityRecord,
  ServiceRecord,
  SessionRecord,
  WorkstationDebugClient,
} from "../ws_client";
import { SessionSubPanel } from "./SessionSubPanel";

export function SessionsPanel(props: {
  sessions: SessionRecord[];
  services: ServiceRecord[];
  entities: EntityRecord[];
  client: WorkstationDebugClient;
}): JSX.Element {
  const [filter, setFilter] = useState("");
  const [checked, setChecked] = useState<Set<string>>(new Set());

  // Drop checkbox state for sessions that vanish (e.g. close-mutation).
  useEffect(() => {
    setChecked((prev) => {
      const live = new Set(props.sessions.map((s) => s.session_id));
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (live.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [props.sessions]);

  const filtered = useMemo(() => {
    if (!filter) return props.sessions;
    const needle = filter.toLowerCase();
    return props.sessions.filter((s) =>
      JSON.stringify(s.metadata).toLowerCase().includes(needle),
    );
  }, [props.sessions, filter]);

  const toggle = (sessionId: string): void => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(sessionId)) next.delete(sessionId);
      else next.add(sessionId);
      return next;
    });
  };

  return (
    <section style={{ marginBottom: 24 }}>
      <h2>Sessions ({props.sessions.length})</h2>
      <div style={{ marginBottom: 8 }}>
        <label>
          filter (substring of metadata JSON):{" "}
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            style={{ fontFamily: "monospace" }}
          />
        </label>
      </div>
      <table border={1} cellPadding={4} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th></th>
            <th>session id</th>
            <th>metadata</th>
            <th>created_at</th>
          </tr>
        </thead>
        <tbody>
          {filtered.map((s) => (
            <tr key={s.session_id}>
              <td>
                <input
                  type="checkbox"
                  checked={checked.has(s.session_id)}
                  onChange={() => toggle(s.session_id)}
                />
              </td>
              <td title={s.session_id}>{s.session_id.slice(0, 8)}…</td>
              <td>{JSON.stringify(s.metadata)}</td>
              <td>{s.created_at}</td>
            </tr>
          ))}
          {filtered.length === 0 && (
            <tr>
              <td colSpan={4}>
                <em>{props.sessions.length === 0 ? "no sessions" : "no matches"}</em>
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {[...checked].map((sessionId) => (
        <SessionSubPanel
          key={sessionId}
          sessionId={sessionId}
          client={props.client}
          services={props.services}
          entities={props.entities}
        />
      ))}
    </section>
  );
}
