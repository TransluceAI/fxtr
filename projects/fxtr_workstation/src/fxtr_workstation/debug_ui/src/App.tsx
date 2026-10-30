import { useEffect, useState } from "react";
import {
  EntityRecord,
  RpcConnectionRecord,
  ServiceRecord,
  SessionRecord,
  WorkstationDebugClient,
} from "./ws_client";
import { RpcConnectionsTable } from "./tables/RpcConnectionsTable";
import { ServicesTable } from "./tables/ServicesTable";
import { EntitiesTable } from "./tables/EntitiesTable";
import { SessionsPanel } from "./tables/SessionsPanel";

function buildClientUrl(): string {
  // Same origin as the page; just swap http(s) → ws(s) and replace
  // the path with /client.
  const wsScheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${wsScheme}//${window.location.host}/client`;
}

export function App(): JSX.Element {
  const [client, setClient] = useState<WorkstationDebugClient | null>(null);
  const [rpcConnections, setRpcConnections] = useState<RpcConnectionRecord[]>(
    [],
  );
  const [services, setServices] = useState<ServiceRecord[]>([]);
  const [entities, setEntities] = useState<EntityRecord[]>([]);
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const client = new WorkstationDebugClient(buildClientUrl());
    let cancelled = false;
    const unsubs: Array<() => void> = [];

    (async () => {
      try {
        await client.ready();
        if (cancelled) return;
        setClient(client);
        unsubs.push(
          client.subscribe(
            "rpc_connections",
            (data) =>
              setRpcConnections(data as unknown as RpcConnectionRecord[]),
            { debug: true },
          ),
          client.subscribe(
            "services",
            (data) => setServices(data as unknown as ServiceRecord[]),
            { debug: true },
          ),
          client.subscribe(
            "entities",
            (data) => setEntities(data as unknown as EntityRecord[]),
            { debug: true },
          ),
          client.subscribe(
            "sessions",
            (data) => setSessions(data as unknown as SessionRecord[]),
            { debug: true },
          ),
        );
      } catch (e) {
        setError(`failed to connect: ${e}`);
      }
    })();

    return () => {
      cancelled = true;
      unsubs.forEach((u) => u());
      client.close();
    };
  }, []);

  // Build a connection-id → name map for the join columns in the
  // services / entities tables.
  const connectionNames = new Map<string, string>(
    rpcConnections.map((c) => [
      c.connection_id,
      c.name ?? c.connection_id.slice(0, 8),
    ]),
  );

  return (
    <div style={{ fontFamily: "monospace", padding: 16 }}>
      <h1>fxtr workstation — debug</h1>
      {error && (
        <div style={{ color: "crimson", marginBottom: 16 }}>{error}</div>
      )}
      <RpcConnectionsTable connections={rpcConnections} />
      <ServicesTable services={services} connectionNames={connectionNames} />
      <EntitiesTable entities={entities} connectionNames={connectionNames} />
      {client && (
        <SessionsPanel
          sessions={sessions}
          services={services}
          entities={entities}
          client={client}
        />
      )}
    </div>
  );
}
