import { render } from "@testing-library/react";
import { atom } from "jotai";
import { EntityID, EntityInlineLink, EntityPanel, type DataMap, type EntityData, type JsonValue } from "fxtr-view";
import { RenderEntity, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import { rolloutRenderers } from "../src";
import { SAMPLING_PARAMS_ENTITY, samplingSummary } from "../src/sampling";

const tag = (name: string) => `org.transluce.behaviors.${name}`;
/** A `SamplingParams` map, stored with its keys sorted, as fxtr stores a map. */
const params = (fields: Record<string, unknown>) => ({
  $type: tag("SamplingParams.v2"), extra: {}, max_tokens: null, reasoning: null, speed: null,
  stop_sequences: [], temperature: null, top_p: null, ...fields,
}) as unknown as DataMap;
const reasoning = (fields: Record<string, unknown>) => ({
  $type: tag("ReasoningParams.v2"), budget_tokens: null, effort: null, enabled: true, summary: null, ...fields,
});

it("names the settings that are set, reasoning first, and none when the provider decides", () => {
  expect(samplingSummary(params({ max_tokens: 16384, reasoning: reasoning({ effort: "high" }) }))).toBe("high effort · 16384 tokens");
  expect(samplingSummary(params({ reasoning: reasoning({ enabled: false }), temperature: 0.5 }))).toBe("no reasoning · temperature 0.5");
  expect(samplingSummary(params({ reasoning: reasoning({ budget_tokens: 2048n }) }))).toBe("2048 reasoning tokens");
  expect(samplingSummary(params({}))).toBe("provider defaults");
});

it("shows a sampling entity's settings in its link, and every field in declared order in its panel", () => {
  const entities = new Map<string, EntityData>([
    ["judge", { $type: SAMPLING_PARAMS_ENTITY, params: params({ max_tokens: 16384, reasoning: reasoning({ effort: "high" }), extra: { seed: 7 } }) }],
    ["defaults", { $type: SAMPLING_PARAMS_ENTITY, params: params({}) }],
  ]);
  const { container } = render(
    <TableShellProvider registry={dispatchRegistry(rolloutRenderers)} entities={entities}>
      <RenderEntity slot={EntityInlineLink} id={new EntityID("judge")} />
      <RenderEntity slot={EntityInlineLink} id={new EntityID("defaults")} />
      <RenderEntity slot={EntityPanel} id={new EntityID("judge")} storage={atom<JsonValue>(null)} />
    </TableShellProvider>,
  );
  expect(container.textContent).toContain("SamplingParams · high effort · 16384 tokens");
  expect(container.textContent).toContain("SamplingParams · provider defaults");
  const panel = container.querySelector("section[aria-label='Sampling parameters']")!;
  expect([...panel.querySelector("header")!.children].map((part) => part.textContent)).toEqual(["SamplingParams", "high effort · 16384 tokens"]);
  const shown = JSON.parse(panel.querySelector("pre")!.textContent!) as Record<string, unknown>;
  expect(Object.keys(shown)).toEqual(["max_tokens", "temperature", "top_p", "stop_sequences", "reasoning", "speed", "extra"]);
  expect(shown["reasoning"]).toEqual({ budget_tokens: null, effort: "high", enabled: true, summary: null });
  expect(shown["extra"]).toEqual({ seed: 7 });
});
