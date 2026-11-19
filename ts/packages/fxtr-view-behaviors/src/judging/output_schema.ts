// A judgment's output schema, and what it says about one field of the answer.

import { isDataMap, type DataMap, type DataValue } from "fxtr-view";
import { sectionsOf } from "./request_sections";
import type { JudgmentData } from "./types";

/** The output schema the judge was given, read out of its first request (a judgment does not
 *  store the schema apart from its requests); null when it is not there or does not parse. The
 *  request's text keeps the order of the schema's fields, the order the judge was asked for
 *  them in. */
export function outputSchemaOf(judgment: JudgmentData): DataMap | null {
  for (const message of judgment.attempts[0]?.request.context ?? []) {
    for (const part of message.content) {
      const section = part.type === "text" ? sectionsOf(part.text).find((candidate) => candidate.name === "output_schema") : undefined;
      if (section === undefined) continue;
      try {
        const schema: unknown = JSON.parse(section.content);
        return isDataMap(schema) ? schema : null;
      } catch {
        return null;
      }
    }
  }
  return null;
}

/** The schema of the value at `path`, or undefined when the schema does not reach it. */
export function schemaAt(schema: DataMap | null, path: readonly (string | number)[]): DataMap | undefined {
  let at: DataValue | undefined = schema ?? undefined;
  for (const part of path) {
    if (!isDataMap(at)) return undefined;
    const properties: DataValue | undefined = at["properties"];
    at = typeof part === "number" ? at["items"] : isDataMap(properties) ? properties[part] : undefined;
  }
  return isDataMap(at) ? at : undefined;
}

/** What a field's schema says it holds, in a few words: `integer · one of 0, 10`. Null when it
 *  says neither a type nor the values allowed. */
export function typeText(field: DataMap): string | null {
  const type = field["type"];
  const types = (Array.isArray(type) ? type : [type]).filter((name): name is string => typeof name === "string");
  const choices = field["enum"];
  const low = field["minimum"];
  const high = field["maximum"];
  const allowed = Array.isArray(choices) ? `one of ${(choices as readonly DataValue[]).map((choice) => JSON.stringify(choice)).join(", ")}`
    : typeof low === "number" && typeof high === "number" ? `${low} to ${high}`
    : typeof low === "number" ? `${low} or more` : typeof high === "number" ? `${high} or less` : null;
  const parts = [types.join(" or "), allowed].filter((part): part is string => part !== null && part !== "");
  return parts.length === 0 ? null : parts.join(" · ");
}
