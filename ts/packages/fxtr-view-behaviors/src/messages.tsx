import styles from "./behaviors.module.css";
import { isDataMap, type DataMap } from "fxtr-view";
import { MODEL_RESPONSE, type ContentData, type MessageData, type MetadataEntries, type TurnData } from "./types";

export function Metadata({ value, label = "Metadata" }: { value: DataMap; label?: string }) {
  if (Object.keys(value).length === 0) return null;
  return (
    <details className={styles["details"]}>
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

/** Tagged metadata entries, each shown under its type tag. */
export function MetadataList({ entries, label }: { entries: MetadataEntries; label: string }) {
  if (entries.length === 0) return null;
  return (
    <details className={styles["details"]}>
      <summary>{label}</summary>
      {entries.map(({ $type, ...fields }, index) => (
        <div key={index}>
          <div className={styles["muted"]}>{$type}</div>
          <pre>{JSON.stringify(fields, null, 2)}</pre>
        </div>
      ))}
    </details>
  );
}

function ContentBlock({ block }: { block: ContentData }) {
  switch (block.type) {
    case "text":
      return <div className={styles["prose"]}>{block.text || <em>Empty text</em>}</div>;
    case "reasoning":
    case "summarized_reasoning":
      return (
        <details className={styles["details"]}>
          <summary>{block.type === "reasoning" ? "Reasoning" : "Reasoning summary"}</summary>
          <div className={styles["prose"]}>{block.text || "No summary text recorded."}</div>
        </details>
      );
    case "redacted_reasoning":
      return <p className={styles["muted"]}>Reasoning redacted by provider.</p>;
    case "tool_use":
      return (
        <details className={styles["details"]}>
          <summary>Tool call · {block.name}</summary>
          <div className={styles["muted"]}>Call ID: {block.id}</div>
          {block.parse_error && <p className={styles["error"]}>{block.parse_error}</p>}
          <pre>{JSON.stringify(block.input, null, 2)}</pre>
        </details>
      );
  }
}

export function Messages({ messages }: { messages: readonly MessageData[] }) {
  return (
    <div className={styles["messages"]}>
      {messages.map((message, index) => (
        <article className={styles["message"]} data-role={message.role} key={index}>
          <header className={styles["role"]}>
            {message.role}
            {message.role === "tool" && (
              <>
                <span className={styles["muted"]}>{String(message["tool_call_id"] ?? "")}</span>
                {message["is_error"] === true && <span className={styles["error"]}>Tool error</span>}
              </>
            )}
          </header>
          {message.content.length === 0 && <p className={styles["muted"]}>No content recorded.</p>}
          {message.content.map((block, blockIndex) => <ContentBlock block={block} key={blockIndex} />)}
        </article>
      ))}
    </div>
  );
}

/** Usage comes from the `ModelResponse` a model may record in its turn's metadata; count only
 *  validated numeric fields. */
export function usageOf(turns: readonly TurnData[]): string | null {
  let input = 0;
  let output = 0;
  let count = 0;
  for (const turn of turns) {
    const response = turn.metadata.find((entry) => entry.$type === MODEL_RESPONSE);
    const usage = response?.["usage"];
    if (!isDataMap(usage)) continue;
    const i = usage["input_tokens"];
    const o = usage["output_tokens"];
    if (typeof i !== "number" || !Number.isFinite(i) || i < 0 ||
        typeof o !== "number" || !Number.isFinite(o) || o < 0) continue;
    input += i;
    output += o;
    count++;
  }
  if (count === 0) return null;
  const partial = count < turns.length ? ` · ${count}/${turns.length} responses reporting usage` : "";
  return `${input.toLocaleString()} input · ${output.toLocaleString()} output tokens${partial}`;
}
