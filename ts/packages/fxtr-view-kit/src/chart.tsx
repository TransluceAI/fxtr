// The parts of a chart that every chart redraws otherwise: the frame that measures the width a
// chart is drawn at and hosts its tooltip, marks a reader can press with the keyboard as well as
// the pointer, and legends for series and for the colour scales. Marks, axes, and scales are the
// renderer's own; the looks they share are `layout.mark`, `layout.cellMark`, and `layout.faded`.

import { useCallback, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { divergingFill, sequentialFill } from "./colors";
import styles from "./chart.module.css";

/** The width an element is laid out at, kept current as it resizes, with a ref to attach to it:
 *  `[ref, width]`. `fallback` until the element is measured (and where nothing measures, as in
 *  tests), and kept while the element has no width (hidden). Draw at this width, in pixels, rather
 *  than scaling a fixed drawing with `viewBox`, so text stays at the size the shell sets. */
export function useWidth<E extends Element>(fallback = 480): [(element: E | null) => void, number] {
  const [width, setWidth] = useState<number | null>(null);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((element: E | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (element === null) return;
    const measure = (measured: number) => {
      if (measured > 0) setWidth(Math.floor(measured));
    };
    measure(element.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    observer.current = new ResizeObserver(([entry]) => {
      if (entry) measure(entry.contentRect.width);
    });
    observer.current.observe(element);
  }, []);
  return [ref, width ?? fallback];
}

/** A tooltip's content and where it points, in pixels from the frame's top left. */
export interface ChartTooltip {
  readonly x: number;
  readonly y: number;
  readonly content: ReactNode;
}

export interface ChartFrameProps {
  /** What the chart shows, for a screen reader: "greeting length by guest". */
  readonly label: string;
  /** The tooltip to show, if any. */
  readonly tooltip?: ChartTooltip | null;
  /** The width to draw at until the frame is measured. */
  readonly fallbackWidth?: number;
  /** The chart, drawn at the frame's width. */
  readonly children: (width: number) => ReactNode;
}

/** A chart's frame: as wide as its container, measured, its children drawn at that width, and its
 *  tooltip over them in the look of the shell's popovers, on whichever side of the point has more
 *  room, kept within the frame's width. Set the tooltip on hover and on keyboard focus alike, and
 *  clear it on leave and blur. */
export function ChartFrame({ label, tooltip, fallbackWidth, children }: ChartFrameProps) {
  const [ref, width] = useWidth<HTMLDivElement>(fallbackWidth);
  return (
    <div ref={ref} className={styles.frame} role="figure" aria-label={label}>
      {children(width)}
      {tooltip && <Tooltip tooltip={tooltip} width={width} />}
    </div>
  );
}

/** Measure the whole tooltip box so its preferred side cannot push it past either frame edge. */
function Tooltip({ tooltip, width }: { tooltip: ChartTooltip; width: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [measuredWidth, setMeasuredWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => setMeasuredWidth(element.getBoundingClientRect().width);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [tooltip.content, width]);

  const side = tooltip.x > width / 2 ? "left" : "right";
  const preferred = side === "left" ? tooltip.x - measuredWidth - 10 : tooltip.x + 10;
  const left = Math.max(0, Math.min(preferred, width - measuredWidth));
  return (
    <div ref={ref} className={styles.tooltip} role="tooltip" data-side={side} style={{ left, top: tooltip.y }}>
      {tooltip.content}
    </div>
  );
}

/** Props that make an element (an SVG mark, a cell) a button for the pointer and the keyboard:
 *  focusable, pressed with a click, Enter, or Space, and named for a screen reader. Spread them
 *  onto the element: `<rect {...pressable(() => links.open(id), "Ada: 12 characters")} />`. */
export function pressable(onPress: () => void, label: string) {
  return {
    role: "button",
    tabIndex: 0,
    "aria-label": label,
    onClick: onPress,
    onKeyDown: (event: KeyboardEvent<Element>) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      onPress();
    },
  } as const;
}

/** One entry of a legend: a series' colour, drawn as the series' marks are. */
export interface LegendItem {
  readonly label: ReactNode;
  readonly color: string;
  readonly shape?: "square" | "circle" | "line";
}

/** A legend for two or more series, on one line that wraps: a swatch, then the series' name. A
 *  chart of one series needs none (its heading names it); label the series directly as well
 *  where there is room. */
export function Legend({ items }: { items: readonly LegendItem[] }) {
  return (
    <ul className={styles.legend}>
      {items.map((item, i) => (
        <li key={i}>
          <span className={styles.swatch} data-shape={item.shape ?? "square"} style={{ background: item.color }} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

const SCALE_STOPS = [0, 0.25, 0.5, 0.75, 1];

/** A legend for a colour scale: its gradient between labels for the ends (and, for the diverging
 *  scale, the midpoint beneath its centre). */
export function ScaleLegend({
  scale,
  low,
  high,
  mid,
}: {
  scale: "sequential" | "diverging";
  low: ReactNode;
  high: ReactNode;
  mid?: ReactNode;
}) {
  const stops = scale === "sequential" ? SCALE_STOPS.map(sequentialFill) : SCALE_STOPS.map((t) => divergingFill(t * 2 - 1));
  return (
    <div className={styles.scale} data-mid={mid !== undefined || undefined}>
      <span>{low}</span>
      <span className={styles.scaleBar} style={{ background: `linear-gradient(to right, ${stops.join(", ")})` }}>
        {mid !== undefined && <span className={styles.scaleMid}>{mid}</span>}
      </span>
      <span>{high}</span>
    </div>
  );
}
