import { render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";
import { EntityID, EntityLink, EntityPanel, EntityHoverPreview, ViewShellProvider, type EntityData, type JsonValue, type ViewShell } from "fxtr-view";
import { RenderEntity, RegistryProvider, dispatchRegistry } from "fxtr-view-shell-util";
import { PassthroughEntityLinkImpl, TableShellProvider } from "fxtr-view-shell-util/testing";
import {
  BIO,
  MESSAGE,
  MODEL_CONFIG,
  REWRITE_INSTRUCTIONS,
  RUBRIC,
  SCENARIO,
  SIMULATED_TRANSCRIPT,
  TRANSCRIPT,
  demoRenderers,
  type ModelConfigData,
  type TranscriptData,
} from "../src/index";

const model: ModelConfigData = { $type: MODEL_CONFIG, name: "gpt-x", temperature: 0.7 };
const transcript: TranscriptData = {
  $type: TRANSCRIPT,
  model: new EntityID("model1"),
  scenario: new EntityID("scen1"),
  messages: [
    { $type: MESSAGE, role: "user", content: "Hello there, I have a question about my order." },
    { $type: MESSAGE, role: "assistant", content: "Of course, what is the order number?" },
  ],
};
const entities = new Map<string, EntityData>([
  ["model1", model],
  ["scen1", { $type: SCENARIO, title: "Lost parcel", prompt: "The user cannot find their parcel. ".repeat(10) }],
  ["rubric1", { $type: RUBRIC, name: "helpfulness", criteria: ["asks for the order number", "apologizes"] }],
  ["tr1", transcript],
  ["bio1", { $type: BIO, name: "Ada", persona: "A patient engineer who likes precise answers.", detail: 2 }],
  ["instr1", { $type: REWRITE_INSTRUCTIONS, style: "terser", target_detail: 3 }],
  ["sim1", { $type: SIMULATED_TRANSCRIPT, model: new EntityID("model1"), bio: new EntityID("bio1"), messages: [] }],
]);
const registry = dispatchRegistry(demoRenderers);
const storage = atom<JsonValue>(null);
const shell = { registry, entities };

describe("panels", () => {
  it("renders a transcript with linked model and scenario and chat turns", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderEntity slot={EntityPanel} id={new EntityID("tr1")} storage={storage} />
      </TableShellProvider>,
    );
    const links = Array.from(document.querySelectorAll(".fxtr-view-link")).map((l) => l.textContent);
    expect(links).toEqual(["ModelConfig gpt-x", "Scenario Lost parcel"]);
    expect(document.querySelector(".panel > :first-child")?.textContent).toBe("Transcript");
    expect(document.querySelectorAll(".turn").length).toBe(2);
    expect(document.querySelector(".turn[data-role='assistant'] .content")?.textContent).toBe(
      "Of course, what is the order number?",
    );
  });
  it("renders the simple types", () => {
    for (const [cid, expected] of [
      ["model1", "gpt-x"],
      ["rubric1", "helpfulness"],
      ["bio1", "Ada"],
      ["instr1", "rewrite: terser"],
    ] as const) {
      const { unmount } = render(
        <TableShellProvider {...shell}>
          <RenderEntity slot={EntityPanel} id={new EntityID(cid)} storage={storage} />
        </TableShellProvider>,
      );
      expect(document.querySelector(".title")?.textContent).toBe(expected);
      // Every panel starts with its kind.
      expect(document.querySelector(".panel > :first-child .kind")).not.toBeNull();
      unmount();
    }
  });
  it("says so for an empty simulated transcript", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderEntity slot={EntityPanel} id={new EntityID("sim1")} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("no turns")).toBeTruthy();
    expect(document.querySelector(".header")?.textContent).toBe("ModelConfig gpt-x as Bio Ada · 0 turns");
  });
});

describe("inline links", () => {
  it("labels each type", () => {
    render(
      <TableShellProvider {...shell}>
        {["model1", "scen1", "rubric1", "tr1", "bio1", "instr1", "sim1"].map((cid) => (
          <EntityLink key={cid} id={new EntityID(cid)} />
        ))}
      </TableShellProvider>,
    );
    const labels = Array.from(document.querySelectorAll(".link")).map((l) => l.textContent);
    expect(labels).toEqual([
      "ModelConfig gpt-x",
      "Scenario Lost parcel",
      "Rubric helpfulness",
      "Transcript 2 turns",
      "Bio Ada",
      "RewriteInstructions terser",
      "SimulatedTranscript 0 turns",
    ]);
    // Every link starts with its kind.
    expect(document.querySelectorAll(".link > .kind:first-child").length).toBe(7);
  });
});

describe("hover previews", () => {
  it("shows a transcript's model name (loaded through the typed id) and first turn", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderEntity slot={EntityHoverPreview} id={new EntityID("tr1")} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".header")?.textContent).toBe("Transcript gpt-x · 2 turns");
    expect(document.querySelector(".previewTurn")?.textContent).toBe(
      "user Hello there, I have a question about my order. · 1 more",
    );
  });
  it("suspends onto the default loading fallback while its own entity is pending", () => {
    const pendingShell: ViewShell = {
      useEntity: () => ({ status: "loading", promise: new Promise(() => {}) }),
      EntityLinkImpl: PassthroughEntityLinkImpl,
    };
    render(
      <RegistryProvider registry={registry}>
        <ViewShellProvider shell={pendingShell}>
          <RenderEntity slot={EntityHoverPreview} id={new EntityID("tr1")} />
        </ViewShellProvider>
      </RegistryProvider>,
    );
    expect(screen.getByText("…")).toBeTruthy();
  });
  it("excerpts long prose", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderEntity slot={EntityHoverPreview} id={new EntityID("scen1")} />
      </TableShellProvider>,
    );
    const prose = document.querySelector(".preview .prose")?.textContent ?? "";
    expect(prose.endsWith("…")).toBe(true);
    expect(prose.length).toBeLessThan(170);
  });
});
