import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CidText } from "../src/index";

describe("CidText", () => {
  it("marks the prefix, the short run, and the rest", () => {
    const { container } = render(<CidText cid="bafyreiabcdefghijkl" />);
    expect(container.querySelector(".fxtr-view-cid-prefix")?.textContent).toBe("bafyrei");
    expect(container.querySelector(".fxtr-view-cid-short")?.textContent).toBe("abcdefgh");
    expect(container.querySelector(".fxtr-view-cid-rest")?.textContent).toBe("ijkl");
    expect(container.textContent).toBe("bafyreiabcdefghijkl");
  });
  it("omits empty parts", () => {
    const { container } = render(<CidText cid="short" />);
    expect(container.querySelector(".fxtr-view-cid-prefix")).toBeNull();
    expect(container.querySelector(".fxtr-view-cid-rest")).toBeNull();
    expect(container.textContent).toBe("short");
  });
});
