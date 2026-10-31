import styles from "./rollouts.module.css";
import { Suspense, useState, type ReactElement } from "react";
import {
  EntityDataError,
  EntityLink,
  invocationRefKey,
  isEntityID,
  rowsOf,
  unwrap,
  useEntity,
  useInvocation,
  useLink,
  type ArrayData,
  type EntityID,
  type InvocationPanelProps,
  type InvocationRecord,
  type LinkKeyValue,
  type Spawn,
} from "fxtr-view";
import { DataTable, type TableColumn, type TableRowProps } from "fxtr-view-kit";
import { ChatRolloutView } from "fxtr-view-behaviors";
import { CHAT_ROLLOUT, type ChatRolloutData } from "fxtr-view-behaviors";
import { CONVERSATIONS } from "./views";

function InputValue({ id }: { id: EntityID<ArrayData> }) {
  return <>{rowsOf(unwrap(useEntity(id))).map((row) => String(row.value)).join(", ")}</>;
}

/** A stored conversation, its link at `keys`: the keys of the spawn and of the output row it is. */
function StoredConversation({ id, keys }: { id: EntityID; keys: Spawn["key"] }) {
  const data = unwrap(useEntity(id));
  if (data.$type !== CHAT_ROLLOUT) throw new EntityDataError("Expected a ChatRollout output");
  return (
    <>
      <ChatRolloutView data={data as ChatRolloutData} />
      <div className={styles["reference"]}><EntityLink id={id} at={{ keys }} /></div>
    </>
  );
}

/** Every output row holds one rollout; scalar step results and mapped workflow results share it. */
function OutputConversations({ output, keys }: { output: EntityID<ArrayData>; keys: Spawn["key"] }) {
  const [groupBy, setGroupBy] = useState("sequence");
  const array = unwrap(useEntity(output));
  const rows = rowsOf(array).map((row) => {
    if (!isEntityID(row.value)) throw new EntityDataError("Expected a ChatRollout reference");
    const rowKeys = Object.fromEntries(array.dims.map(([name], i) => [name, keyValue(row.key[i])]));
    return { ...row, id: row.value, keys: { ...keys, ...rowKeys } };
  });
  if (rows.length === 0) return <p className={styles["muted"]}>No conversations recorded.</p>;
  if (array.dims.length === 0) return <StoredConversation id={rows[0]!.id} keys={keys} />;

  const renderRow = (row: (typeof rows)[number], hiddenDim = -1) => {
    const label = row.key.flatMap((key, i) => i === hiddenDim ? [] : [`${array.dims[i]?.[0]}: ${String(key)}`]).join(" · ");
    return (
      <details className={styles["collection"]} key={JSON.stringify(row.key)}>
        <summary>{label} · <EntityLink id={row.id} at={{ keys: row.keys }} /></summary>
        <StoredConversation id={row.id} keys={row.keys} />
      </details>
    );
  };
  if (array.dims.length === 1) return (
    <div>
      <p className={styles["muted"]}>{rows.length} question sequences</p>
      {rows.map((row) => renderRow(row))}
    </div>
  );

  const groupIndex = Math.max(0, array.dims.findIndex(([name]) => name === groupBy));
  const groups = new Map<string, typeof rows>();
  for (const row of rows) {
    const key = String(row.key[groupIndex]);
    const group = groups.get(key) ?? [];
    group.push(row);
    groups.set(key, group);
  }
  return (
    <div>
      <div className={styles["toolbar"]}>
        <span className={styles["muted"]}>{rows.length} conversations</span>
        <label>Group by {" "}
          <select aria-label="Group conversations by" value={array.dims[groupIndex]![0]}
            onChange={(event) => setGroupBy(event.target.value)}>
            {array.dims.map(([name]) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
      </div>
      {[...groups].map(([key, group]) => (
        <details className={styles["group"]} key={`${groupIndex}:${key}`} open>
          <summary>{key} <span className={styles["muted"]}>· {group.length} conversations</span></summary>
          {group.map((row) => renderRow(row, groupIndex))}
        </details>
      ))}
    </div>
  );
}

/** One mapped spawn per component keeps its live loading/state independent of the others. */
function ConversationSpawn({ spawn }: { spawn: Spawn }) {
  const result = useInvocation(spawn.ref);
  const label = Object.entries(spawn.key).map(([name, value]) => `${name}: ${value}`).join(" · ");
  return (
    <details className={styles["collection"]}>
      <summary>{label || "Conversation"} · {result.status === "loaded" ? result.value.state : result.status}</summary>
      {result.status === "loaded" ? <OpenAIConversationView invocation={result.value} keys={spawn.key} />
        : result.status === "error" ? <p className={styles["error"]}>{String(result.error)}</p>
        : <p className={styles["muted"]}>Loading conversation…</p>}
    </details>
  );
}

/** A spawn's state, and its error if it failed. */
function ConversationState({ spawn }: { spawn: Spawn }) {
  const result = useInvocation(spawn.ref);
  if (result.status === "loading") return <span className={styles["muted"]}>loading…</span>;
  if (result.status === "error") return <span className={styles["error"]}>{String(result.error)}</span>;
  const { state, error } = result.value;
  return error ? <span className={styles["error"]}>{state}: {error.typeName}: {error.message}</span> : <>{state}</>;
}

/** A row among several conversations, opening that one alone: through a link to the conversation
 *  view at its keys, or, where the host opens no views from here, by picking it to show below. */
function ConversationRow({ spawn, tr, picked, onPick }: {
  spawn: Spawn;
  tr: (props: TableRowProps, selected?: boolean) => ReactElement;
  picked: boolean;
  onPick: () => void;
}) {
  const link = useLink(CONVERSATIONS, spawn.key);
  return link ? tr(link.props, link.open) : tr({ onClick: onPick }, picked);
}

/** Several conversations: a row each with its keys and state, opening that conversation alone. */
function OpenAIConversationList({ invocations }: InvocationPanelProps) {
  const [picked, setPicked] = useState<string | null>(null);
  const dims = [...new Set(invocations.flatMap((spawn) => Object.keys(spawn.key)))];
  const columns: TableColumn<Spawn>[] = [
    ...dims.map((dim): TableColumn<Spawn> => ({ key: `key:${dim}`, label: dim, value: (spawn) => spawn.key[dim] ?? null })),
    { key: "state", label: "state", cell: (spawn) => <ConversationState spawn={spawn} /> },
  ];
  const shown = invocations.find((spawn) => invocationRefKey(spawn.ref) === picked);
  return (
    <section className={styles["panel"]}>
      <div className={styles["kind"]}>Conversations · {invocations.length}</div>
      <DataTable
        rows={invocations}
        columns={columns}
        rowKey={(spawn) => invocationRefKey(spawn.ref)}
        renderRow={(spawn, tr) => {
          const key = invocationRefKey(spawn.ref);
          return <ConversationRow spawn={spawn} tr={tr} picked={picked === key} onPick={() => setPicked(picked === key ? null : key)} />;
        }}
      />
      {shown && (
        <Suspense fallback={<p className={styles["muted"]}>Loading conversation…</p>}>
          <OneConversation spawn={shown} />
        </Suspense>
      )}
    </section>
  );
}

/** A step returns one conversation; the workflow returns the model/sequence comparison. */
function OpenAIConversationView({ invocation, keys }: { invocation: InvocationRecord; keys: Spawn["key"] }) {
  const model = invocation.inputs["model_name"];
  const maxTokens = invocation.inputs["max_tokens"];
  return (
    <section className={styles["invocation"]}>
      <header className={styles["header"]}>
        <span className={styles["kind"]}>{invocation.ref.kind}</span>
        <span className={styles["muted"]}>{invocation.state}</span>
        <h2>{invocation.ref.kind === "workflow" ? "OpenAI conversations" : "OpenAI conversation"}</h2>
        <div className={styles["muted"]}>
          {model && <InputValue id={model} />}
          {maxTokens && <> · up to <InputValue id={maxTokens} /> output tokens per response</>}
        </div>
      </header>
      {invocation.error && <p className={styles["error"]}>{invocation.error.typeName}: {invocation.error.message}</p>}
      {invocation.output
        ? <OutputConversations output={invocation.output} keys={keys} />
        : <>
            <p className={styles["muted"]}>No conversation output yet · {invocation.state}</p>
            {invocation.children.flatMap((child) => child.spawns).map((spawn) => (
              <ConversationSpawn spawn={spawn} key={invocationRefKey(spawn.ref)} />
            ))}
          </>}
    </section>
  );
}

function OneConversation({ spawn }: { spawn: Spawn }) {
  return <OpenAIConversationView invocation={unwrap(useInvocation(spawn.ref))} keys={spawn.key} />;
}

/** A key value of an output row, as a link's keys hold it. */
function keyValue(value: unknown): LinkKeyValue {
  return typeof value === "number" ? value : String(value);
}

/** One conversation in full; several as a row each with its state, opening that one alone. */
export function OpenAIConversationsPanel(props: InvocationPanelProps) {
  if (props.invocations.length === 0) return <p className={styles["muted"]}>No conversations selected.</p>;
  return props.invocations.length === 1 ? <OneConversation spawn={props.invocations[0]!} /> : <OpenAIConversationList {...props} />;
}
