import { RpcConnectionRecord } from "../ws_client";

export function RpcConnectionsTable(props: {
  connections: RpcConnectionRecord[];
}): JSX.Element {
  return (
    <section style={{ marginBottom: 24 }}>
      <h2>RPC connections ({props.connections.length})</h2>
      <table border={1} cellPadding={4} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th>id</th>
            <th>name</th>
            <th>peer</th>
            <th>connected_at</th>
            <th>services</th>
            <th>entities</th>
          </tr>
        </thead>
        <tbody>
          {props.connections.map((c) => (
            <tr key={c.connection_id}>
              <td title={c.connection_id}>{c.connection_id.slice(0, 8)}…</td>
              <td>{c.name ?? <em>(unnamed)</em>}</td>
              <td>{c.peer_addr ?? "—"}</td>
              <td>{c.connected_at}</td>
              <td>{c.service_registration_short_ids.join(", ")}</td>
              <td>{c.entity_registration_short_ids.join(", ")}</td>
            </tr>
          ))}
          {props.connections.length === 0 && (
            <tr>
              <td colSpan={6}>
                <em>no RPC connections</em>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
