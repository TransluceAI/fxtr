import { EntityRecord } from "../ws_client";

export function EntitiesTable(props: {
  entities: EntityRecord[];
  connectionNames: Map<string, string>;
}): JSX.Element {
  return (
    <section style={{ marginBottom: 24 }}>
      <h2>Tracked entities ({props.entities.length})</h2>
      <table border={1} cellPadding={4} style={{ borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th>short id</th>
            <th>fxtr id</th>
            <th>external names</th>
            <th>registered by</th>
          </tr>
        </thead>
        <tbody>
          {props.entities.map((e) => (
            <tr key={e.short_id}>
              <td>{e.short_id}</td>
              <td title={e.fxtr_id}>{e.fxtr_id.slice(0, 16)}…</td>
              <td>{e.external_names.join(", ")}</td>
              <td>
                {e.registering_connection_ids
                  .map((id) => props.connectionNames.get(id) ?? id.slice(0, 8))
                  .join(", ")}
              </td>
            </tr>
          ))}
          {props.entities.length === 0 && (
            <tr>
              <td colSpan={4}>
                <em>no entities</em>
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
