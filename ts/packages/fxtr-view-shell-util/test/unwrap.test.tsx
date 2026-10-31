import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  EntityID,
  EntityInlineLink,
  ViewShellProvider,
  renderer,
  unwrap,
  useEntity,
  type EntityData,
  type EntityViewProps,
  type LoadResult,
  type ViewShell,
} from "fxtr-view";
import { RegistryProvider, dispatchRegistry } from "../src/index";
import { RenderSlot } from "../src/render";
import { PassthroughEntityLinkImpl } from "../testing";

describe("unwrap under RenderSlot", () => {
  it("suspends a dispatched component onto the loading fallback, then renders when the load settles", async () => {
    let resolve!: (data: EntityData) => void;
    const promise = new Promise<EntityData>((r) => (resolve = r));
    let loaded: EntityData | null = null;
    void promise.then((d) => (loaded = d));
    // A shell whose useEntity is pending until `resolve`, then loaded.
    const registry = dispatchRegistry([renderer(EntityInlineLink, "org.example.Note", "default", Unwrapping)]);
    const shell: ViewShell = {
      useEntity: (): LoadResult<EntityData> => (loaded ? { status: "loaded", value: loaded } : { status: "loading", promise }),
      EntityLinkImpl: PassthroughEntityLinkImpl,
    };
    function Unwrapping({ id }: EntityViewProps) {
      const data = unwrap(useEntity(id));
      return <b>{String(data["body"])}</b>;
    }
    render(
      <RegistryProvider registry={registry}>
        <ViewShellProvider shell={shell}>
          <RenderSlot slot={EntityInlineLink} subjectType="org.example.Note" id={new EntityID("n1")} fallbacks={{ loading: <i>waiting</i> }} />
        </ViewShellProvider>
      </RegistryProvider>,
    );
    expect(screen.getByText("waiting")).toBeTruthy();
    await act(async () => {
      resolve({ $type: "org.example.Note", body: "hi" });
      await promise;
    });
    expect(screen.getByText("hi")).toBeTruthy();
  });
});
