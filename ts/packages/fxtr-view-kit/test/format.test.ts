import { describe, expect, it } from "vitest";
import {
  CHART_OTHER,
  MISSING,
  countLabel,
  divergingFill,
  divergingInk,
  formatNumber,
  formatPercent,
  formatSigned,
  sequentialFill,
  sequentialInk,
  seriesColor,
} from "../src/index";

describe("number formatting", () => {
  it("writes a true minus, groups thousands, and fixes decimals", () => {
    expect(formatNumber(-1234.5)).toBe("−1,234.50");
    expect(formatNumber(12)).toBe("12");
    expect(formatNumber(0.5, 1)).toBe("0.5");
  });
  it("drops the sign of a value that rounds to zero", () => {
    expect(formatNumber(-0.001, 2)).toBe("0.00");
    expect(formatSigned(-0.001)).toBe("0.00");
  });
  it("signs differences either way", () => {
    expect(formatSigned(0.123)).toBe("+0.12");
    expect(formatSigned(-0.123)).toBe("−0.12");
  });
  it("writes shares as percentages", () => {
    expect(formatPercent(0.425)).toBe("43%");
    expect(formatPercent(0.425, 1)).toBe("42.5%");
  });
  it("writes a missing value as a dash", () => {
    expect(formatNumber(null)).toBe(MISSING);
    expect(formatPercent(undefined)).toBe(MISSING);
    expect(formatSigned(Number.NaN)).toBe(MISSING);
  });
  it("counts with the noun", () => {
    expect(countLabel(1, "greeting")).toBe("1 greeting");
    expect(countLabel(1204, "reply", "replies")).toBe("1,204 replies");
  });
});

describe("chart colours", () => {
  it("assigns series in order and folds the rest into other, never cycling", () => {
    expect(seriesColor(0)).toBe("var(--chart-1)");
    expect(seriesColor(7)).toBe("var(--chart-8)");
    expect(seriesColor(8)).toBe(CHART_OTHER);
  });
  it("mixes along the sequential scale, clamped", () => {
    expect(sequentialFill(0.25)).toBe("color-mix(in oklab, var(--chart-seq-1) 25%, var(--chart-seq-0))");
    expect(sequentialFill(2)).toBe("color-mix(in oklab, var(--chart-seq-1) 100%, var(--chart-seq-0))");
  });
  it("mixes toward the pole on the value's side of the diverging scale", () => {
    expect(divergingFill(-0.5)).toBe("color-mix(in oklab, var(--chart-div-neg) 50%, var(--chart-div-mid))");
    expect(divergingFill(0.5)).toBe("color-mix(in oklab, var(--chart-div-pos) 50%, var(--chart-div-mid))");
  });
  it("flips the ink over strong fills, per theme", () => {
    expect(sequentialInk(0.1)).toBe("var(--fg)");
    expect(sequentialInk(0.6)).toBe("light-dark(var(--fg), var(--bg))");
    expect(sequentialInk(0.9)).toBe("var(--bg)");
    expect(divergingInk(-0.9)).toBe("var(--bg)");
  });
});
