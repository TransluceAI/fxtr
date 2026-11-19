// What a judge was given, as far as a judgment records it, at the foot of its conversation pane
// (`JudgeConversationView` in judgment_panes.tsx), under the messages they were filled into: its
// entities, its output schema and its sampling parameters. `judge`'s other arguments (the model
// API, the functions that write the entities, the custom formats, the retry settings) are not
// recorded, so they are not shown.

import shared from "../behaviors.module.css";
import styles from "./judging.module.css";
import type { ReactNode } from "react";
import { EntityLink, isDataMap, useEntityLinks, type DataMap, type DataValue, type EntityID } from "fxtr-view";
import { countLabel } from "../entities";
import { UnsentBlocksWarning } from "./unsent_blocks";
import { SCHEMA_PARAMS, type JUDGE_CONVERSATION_PARAMS } from "./judgment_pane_params";
import { outputSchemaOf } from "./output_schema";
import type { JudgmentData } from "./types";

/** The fields of `SamplingParams` (behaviors/types/response.py), in the order it declares them.
 *  A stored map keeps no order of its own (fxtr sorts its keys), so the view puts them back. A
 *  field not listed here follows them. */
const SAMPLING_FIELDS = ["max_tokens", "temperature", "top_p", "stop_sequences", "reasoning", "speed", "extra"];

/** A stored value as JSON to read: the type tags (`$type`) left out at every depth, a map's keys
 *  in `order` first when it is the top level, and a big integer as its digits. */
export function readableJson(value: DataValue, order: readonly string[] = []): unknown {
  if (Array.isArray(value)) return (value as readonly DataValue[]).map((item) => readableJson(item));
  if (typeof value === "bigint") return value.toString();
  if (!isDataMap(value)) return value;
  const keys = Object.keys(value).filter((key) => key !== "$type");
  const rank = (key: string) => (order.includes(key) ? order.indexOf(key) : order.length);
  return Object.fromEntries([...keys].sort((a, b) => rank(a) - rank(b)).map((key) => [key, readableJson(value[key] ?? null)]));
}

/** A chip that opens a part of the judgment (its conversation, or its output schema) in a pane of its
 *  own, lit while that part is open. Without the judgment's id, it is plain text. */
export function PartChip({ id, params, children }: { id: EntityID | undefined; params: typeof JUDGE_CONVERSATION_PARAMS | typeof SCHEMA_PARAMS; children: ReactNode }) {
  const links = useEntityLinks();
  if (id === undefined) return <span>{children}</span>;
  return (
    <button type="button" className={styles["partChip"]} aria-pressed={links.isOpen(id, params)} onClick={() => links.open(id, params)}>
      {children}
    </button>
  );
}

/** The arguments `judge` was given, as far as the judgment records them, a row each, name beside value: each entity as a link, the output schema
 *  as a chip that opens it, and the sampling parameters as JSON. Under them, any blocks of a
 *  transcript or an agent run that were not sent to the judge are named. */
export function JudgeArguments({ judgment, id }: { judgment: JudgmentData; id: EntityID | undefined }) {
  const schema = outputSchemaOf(judgment);
  const properties = schema === null ? undefined : schema["properties"];
  const sampling = judgment.attempts[0]?.request.sampling;
  return (
    <section className={shared["exchange"]} aria-label="Arguments to judge">
      <h3 className={shared["turn-number"]}>Arguments to judge</h3>
      <dl className={styles["arguments"]}>
        {Object.entries(judgment.entity_ids).map(([name, entity]) => (
          <div key={name}>
            <dt>{name}</dt>
            <dd><EntityLink id={entity} /></dd>
          </div>
        ))}
        {schema !== null && (
          <div>
            <dt>output_schema</dt>
            <dd>
              <PartChip id={id} params={SCHEMA_PARAMS}>
                {`${typeof schema["type"] === "string" ? schema["type"] : "schema"}${isDataMap(properties) ? ` · ${countLabel(Object.keys(properties).length, "field")}` : ""}`}
              </PartChip>
            </dd>
          </div>
        )}
        {sampling !== undefined && (
          <div>
            <dt>sampling</dt>
            <dd><pre className={styles["json"]}>{JSON.stringify(readableJson(sampling as DataMap, SAMPLING_FIELDS), null, 2)}</pre></dd>
          </div>
        )}
      </dl>
      <UnsentBlocksWarning judgment={judgment} />
    </section>
  );
}
