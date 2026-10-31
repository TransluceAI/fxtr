import { useState } from "react";
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { atom, type PrimitiveAtom } from "jotai";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JsonValue } from "fxtr-view";
import { ChartFrame, DetailBox, Legend, Segmented, pressable, useStoredState } from "../src/index";

describe("ChartFrame", () => {
  afterEach(() => vi.restoreAllMocks());

  it("draws its children at the fallback width until measured", () => {
    render(<ChartFrame label="lengths" fallbackWidth={300}>{(width) => <span>{`width ${width}`}</span>}</ChartFrame>);
    expect(screen.getByRole("figure", { name: "lengths" })).toBeTruthy();
    expect(screen.getByText("width 300")).toBeTruthy();
  });
  it("puts the tooltip on the side of its point with more room", () => {
    const { rerender } = render(
      <ChartFrame label="lengths" fallbackWidth={300} tooltip={{ x: 40, y: 10, content: "near the left" }}>
        {() => null}
      </ChartFrame>,
    );
    expect(screen.getByRole("tooltip").dataset["side"]).toBe("right");
    rerender(
      <ChartFrame label="lengths" fallbackWidth={300} tooltip={{ x: 260, y: 10, content: "near the right" }}>
        {() => null}
      </ChartFrame>,
    );
    expect(screen.getByRole("tooltip").dataset["side"]).toBe("left");
  });

  it("keeps a measured tooltip within both edges as its content and frame resize", () => {
    let tooltipWidth = 234;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, this.getAttribute("role") === "tooltip" ? tooltipWidth : 0, 40);
    });
    const chart = (x: number, width = 320, content = "model accuracy by evaluation scenario") => (
      <ChartFrame label="scores" fallbackWidth={width} tooltip={{ x, y: 80, content }}>
        {() => null}
      </ChartFrame>
    );
    const { rerender } = render(chart(160));
    expect(screen.getByRole("tooltip").style.left).toBe("86px");
    rerender(chart(161));
    expect(screen.getByRole("tooltip").style.left).toBe("0px");

    tooltipWidth = 260;
    rerender(chart(160, 320, "longer context about this evaluation scenario"));
    expect(screen.getByRole("tooltip").style.left).toBe("60px");

    tooltipWidth = 200;
    rerender(chart(100, 200));
    expect(screen.getByRole("tooltip").style.left).toBe("0px");
  });
});

describe("pressable", () => {
  it("presses with a click, Enter, and Space, and is named", () => {
    let presses = 0;
    render(
      <svg>
        <rect {...pressable(() => presses++, "Ada: 12 characters")} />
      </svg>,
    );
    const mark = screen.getByRole("button", { name: "Ada: 12 characters" });
    fireEvent.click(mark);
    fireEvent.keyDown(mark, { key: "Enter" });
    fireEvent.keyDown(mark, { key: " " });
    fireEvent.keyDown(mark, { key: "a" });
    expect(presses).toBe(3);
    expect(mark.getAttribute("tabindex")).toBe("0");
  });
});

describe("Legend", () => {
  it("lists each series with its swatch", () => {
    render(<Legend items={[{ label: "conversational", color: "var(--chart-1)" }, { label: "instructional", color: "var(--chart-2)", shape: "circle" }]} />);
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(["conversational", "instructional"]);
  });
});

describe("Segmented", () => {
  function Choice() {
    const [order, setOrder] = useState<"guest" | "length">("guest");
    return (
      <Segmented
        label="order"
        options={[
          { value: "guest", label: "guest" },
          { value: "length", label: "length" },
        ]}
        value={order}
        onChange={setOrder}
      />
    );
  }
  it("is a radio group that clicks and arrow keys move", () => {
    render(<Choice />);
    const group = screen.getByRole("radiogroup", { name: "order" });
    const [guest, length] = screen.getAllByRole("radio");
    expect(guest?.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(length!);
    expect(length?.getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(group, { key: "ArrowRight" });
    expect(guest?.getAttribute("aria-checked")).toBe("true");
  });
});

describe("DetailBox", () => {
  it("closes from its button", () => {
    let closed = false;
    render(
      <DetailBox title="Ada" onClose={() => (closed = true)}>
        detail
      </DetailBox>,
    );
    fireEvent.click(screen.getByRole("button", { name: "close" }));
    expect(closed).toBe(true);
  });
});

describe("useStoredState", () => {
  function Counter({ storage, name }: { storage: PrimitiveAtom<JsonValue>; name: string }) {
    const [state, setState] = useStoredState(storage, name, { count: 0 }, (value): value is { count: number } =>
      typeof value === "object" && value !== null && "count" in value && typeof value.count === "number",
    );
    return <button onClick={() => setState((prev) => ({ count: prev.count + 1 }))}>{`${name} ${state.count}`}</button>;
  }
  it("keeps state in the storage, across a remount, beside other keys", () => {
    const storage = atom<JsonValue>(null);
    const { unmount } = render(
      <>
        <Counter storage={storage} name="a" />
        <Counter storage={storage} name="b" />
      </>,
    );
    fireEvent.click(screen.getByText("a 0"));
    unmount();
    render(
      <>
        <Counter storage={storage} name="a" />
        <Counter storage={storage} name="b" />
      </>,
    );
    expect(screen.getByText("a 1")).toBeTruthy();
    expect(screen.getByText("b 0")).toBeTruthy();
  });
  it("uses the initial value for reads and updates when the validator rejects stored state", () => {
    const storage = atom<JsonValue>({ a: { count: "three" } });
    render(<Counter storage={storage} name="a" />);
    expect(screen.getByText("a 0")).toBeTruthy();
    fireEvent.click(screen.getByText("a 0"));
    expect(screen.getByText("a 1")).toBeTruthy();
  });

  it.each([null, "Ada"])("selects, clears, and restores nullable state initialized to %s", (initial) => {
    const storage = atom<JsonValue>(null);
    const usePick = () => useStoredState<string | null>(storage, "picked", initial);
    const first = renderHook(usePick);
    act(() => first.result.current[1]("Grace"));
    expect(first.result.current[0]).toBe("Grace");
    first.unmount();

    const second = renderHook(usePick);
    expect(second.result.current[0]).toBe("Grace");
    act(() => second.result.current[1]((prev) => prev === "Grace" ? null : "unexpected"));
    expect(second.result.current[0]).toBeNull();
    second.unmount();

    const third = renderHook(usePick);
    expect(third.result.current[0]).toBeNull();
  });

  it("supports mixed tuples and nullable object fields", () => {
    const storage = atom<JsonValue>(null);
    const { result } = renderHook(() => useStoredState<{ point: [number, string]; picked: string | null }>(
      storage, "selection", { point: [0, "first"], picked: null },
    ));
    act(() => result.current[1]({ point: [1, "second"], picked: "Ada" }));
    expect(result.current[0]).toEqual({ point: [1, "second"], picked: "Ada" });
  });

  it("lets a validator accept nullable values and reject obsolete choices", () => {
    const storage = atom<JsonValue>({ picked: "obsolete" });
    const usePick = () => useStoredState<"Ada" | null>(storage, "picked", "Ada", (value): value is "Ada" | null =>
      value === null || value === "Ada",
    );
    const first = renderHook(usePick);
    expect(first.result.current[0]).toBe("Ada");
    act(() => first.result.current[1](null));
    first.unmount();
    expect(renderHook(usePick).result.current[0]).toBeNull();
  });
});
