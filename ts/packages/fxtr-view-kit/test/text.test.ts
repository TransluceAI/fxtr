import { describe, expect, it } from "vitest";
import { excerpt, turnsLabel } from "../src/index";

describe("text helpers", () => {
  it("excerpt cuts at a word", () => {
    expect(excerpt("one two three four", 9)).toBe("one two…");
    expect(excerpt("short", 9)).toBe("short");
  });
  it("turnsLabel counts turns", () => {
    expect(turnsLabel(1)).toBe("1 turn");
    expect(turnsLabel(3)).toBe("3 turns");
  });
});
