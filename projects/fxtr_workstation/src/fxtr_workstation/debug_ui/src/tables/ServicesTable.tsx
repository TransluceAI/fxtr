import { ServiceRecord } from "../ws_client";

const systemChipStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "0 6px",
  marginRight: 4,
  border: "1px solid #888",
  borderRadius: 8,
  fontSize: "0.85em",
  fontStyle: "italic",
  background: "#f4f4f4",
};

export function ServicesTable(props: {
  services: ServiceRecord[];
  connectionNames: Map<string, string>;
}): JSX.Element {
  return (
    <section style={{ marginBottom: 24 }}>
      <h2>Tracked services ({props.services.length})</h2>
      <table border={1} cellPadding={4} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th>short id</th>
            <th>status</th>
            <th>type id</th>
            <th>external names</th>
            <th>registered by</th>
          </tr>
        </thead>
        <tbody>
          {props.services.map((s) => {
            const connectionLabels = s.registering_connection_ids.map(
              (id) => props.connectionNames.get(id) ?? id.slice(0, 8),
            );
            return (
              <tr
                key={s.short_id}
                style={s.broken ? { textDecoration: "line-through", opacity: 0.6 } : undefined}
              >
                <td>{s.short_id}</td>
                <td>{s.broken ? "broken" : "alive"}</td>
                <td title={s.service_type_id}>
                  {s.service_type_id.slice(0, 12)}…
                </td>
                <td>{s.external_names.join(", ")}</td>
                <td>
                  {s.system_source_kinds.map((kind) => (
                    <span key={`sys:${kind}`} style={systemChipStyle}>
                      system:{kind}
                    </span>
                  ))}
                  {connectionLabels.join(", ")}
                </td>
              </tr>
            );
          })}
          {props.services.length === 0 && (
            <tr>
              <td colSpan={5}>
                <em>no services</em>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
