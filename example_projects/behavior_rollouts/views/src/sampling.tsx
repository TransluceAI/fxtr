// A `SamplingParamsEntity` (src/behavior_rollouts/models.py): the sampling parameters a step
// gives a model, stored as an entity. Its link names the settings that are set, such as
// `SamplingParams · high effort · 16384 tokens`, and its panel shows every field as JSON.

import { isDataMap, unwrap, useEntity, type DataMap, type DataValue, type EntityData, type EntityID, type EntityPanelProps, type EntityViewProps } from "fxtr-view";
import styles from "./rollouts.module.css";

export const SAMPLING_PARAMS_ENTITY = "org.transluce.examples.behavior_rollouts.SamplingParamsEntity.v1";

interface SamplingParamsEntityData extends EntityData {
  readonly params: DataMap;
}

/** The fields of behaviors' `SamplingParams`, in the order it declares them. A stored map keeps
 *  no order of its own (fxtr sorts its keys), so the panel puts them back. */
const SAMPLING_PARAMS_FIELDS = ["max_tokens", "temperature", "top_p", "stop_sequences", "reasoning", "speed", "extra"];

function number(value: DataValue | undefined): string | null {
  return typeof value === "number" || typeof value === "bigint" ? value.toString() : null;
}

/** The settings of a `SamplingParams` map that are set, a short phrase each, joined with ` · `:
 *  reasoning, then the output limit, then the sampling knobs. `provider defaults` when none is. */
export function samplingSummary(params: DataMap): string {
  const parts: string[] = [];
  const reasoning = params["reasoning"];
  if (isDataMap(reasoning)) {
    if (reasoning["enabled"] === false) parts.push("no reasoning");
    else {
      if (typeof reasoning["effort"] === "string") parts.push(`${reasoning["effort"]} effort`);
      const budget = number(reasoning["budget_tokens"]);
      if (budget !== null) parts.push(`${budget} reasoning tokens`);
      if (parts.length === 0) parts.push("reasoning");
    }
  }
  const maxTokens = number(params["max_tokens"]);
  if (maxTokens !== null) parts.push(`${maxTokens} tokens`);
  const temperature = number(params["temperature"]);
  if (temperature !== null) parts.push(`temperature ${temperature}`);
  const topP = number(params["top_p"]);
  if (topP !== null) parts.push(`top_p ${topP}`);
  if (typeof params["speed"] === "string") parts.push(`${params["speed"]} speed`);
  return parts.length === 0 ? "provider defaults" : parts.join(" · ");
}

/** A stored value as JSON to read: type tags left out at every depth, a big integer as its
 *  digits, and the top level's keys in `order` first. fxtr-view-behaviors' judgment view has the
 *  same helper; it is not exported from the package. */
function readableJson(value: DataValue, order: readonly string[] = []): unknown {
  if (Array.isArray(value)) return (value as readonly DataValue[]).map((item) => readableJson(item));
  if (typeof value === "bigint") return value.toString();
  if (!isDataMap(value)) return value;
  const rank = (key: string) => (order.includes(key) ? order.indexOf(key) : order.length);
  const keys = Object.keys(value).filter((key) => key !== "$type").sort((a, b) => rank(a) - rank(b));
  return Object.fromEntries(keys.map((key) => [key, readableJson(value[key] ?? null)]));
}

/** A sampling entity in a line: its kind, then the settings that are set. */
export function SamplingParamsInline({ id }: EntityViewProps) {
  const entity = unwrap(useEntity(id as EntityID<SamplingParamsEntityData>));
  return <>{`SamplingParams · ${samplingSummary(entity.params)}`}</>;
}

/** A sampling entity in a panel: its kind and the settings that are set, then every field. */
export function SamplingParamsPanel({ id }: EntityPanelProps) {
  const entity = unwrap(useEntity(id as EntityID<SamplingParamsEntityData>));
  return (
    <section className={styles["panel"]} aria-label="Sampling parameters">
      <header className={styles["header"]}>
        <span className={styles["kind"]}>SamplingParams</span>
        <span className={styles["muted"]}>{samplingSummary(entity.params)}</span>
      </header>
      <pre className={styles["sampling"]}>{JSON.stringify(readableJson(entity.params, SAMPLING_PARAMS_FIELDS), null, 2)}</pre>
    </section>
  );
}
