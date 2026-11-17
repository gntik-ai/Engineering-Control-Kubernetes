import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

/** One day of a chart: the day as YYYY-MM-DD, and what it counts. */
export interface DayValue {
  day: string;
  value: number;
}

const HEIGHT = 200;
/** Room around the plot: the cap of the tallest column, the days below, the scale left. */
const TOP = 22;
const BOTTOM = 24;
const RIGHT = 8;
/** A column is never thicker than this: what is left of its day is air. */
const THICKEST = 24;
const GAP = 2;
const ROUND = 4;

const SHORT = new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', timeZone: 'UTC' });
const LONG = new Intl.DateTimeFormat('en', { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
/** A day is a date and no moment: it is read as UTC so that no clock moves it. */
const dated = (day: string) => new Date(`${day}T00:00:00Z`);
export const shortDay = (day: string) => SHORT.format(dated(day));
export const longDay = (day: string) => LONG.format(dated(day));

/** A scale from nothing to a round number above the largest value, in a few even steps. */
export function scaleOf(largest: number, whole: boolean): { top: number; step: number; ticks: number[] } {
  if (!(largest > 0)) {
    const step = whole ? 1 : 0.25;
    return { top: step * 4, step, ticks: [0, 1, 2, 3, 4].map((n) => n * step) };
  }
  const rough = largest / 4;
  const power = 10 ** Math.floor(Math.log10(rough));
  const found = (whole ? [1, 2, 5, 10] : [1, 2, 2.5, 5, 10]).map((times) => times * power).find((s) => s >= rough);
  const step = whole ? Math.max(1, Math.round(found ?? power)) : (found ?? power);
  const count = Math.max(1, Math.ceil(largest / step - 1e-9));
  // Rounded, so that 0.1 + 0.2 is not written with seventeen digits.
  const ticks = Array.from({ length: count + 1 }, (_, n) => Number((n * step).toPrecision(12)));
  return { top: count * step, step, ticks };
}

function useWidth(): [(node: HTMLDivElement | null) => void, number] {
  const [width, setWidth] = useState(0);
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!node) return;
    const measure = () => setWidth(node.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [node]);
  return [setNode, width];
}

/** A column that stands on the baseline: round where it ends, square where it starts. */
function column(x: number, y: number, width: number, height: number): string {
  const r = Math.min(ROUND, width / 2, height);
  return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`;
}

/**
 * One number a day, as columns. The days run left to right, the oldest first. Pointing
 * at a day, or walking the days with the arrow keys, says the day and its number.
 */
export function ColumnChart({
  series,
  color,
  label,
  whole = false,
  tick,
  say,
  empty,
}: {
  series: DayValue[];
  /** The colour of the columns, as a CSS colour. */
  color: string;
  /** What the chart shows, for who cannot see it. */
  label: string;
  /** The values are counts: the scale has no fractions. */
  whole?: boolean;
  /** A value on the scale, given the scale's step. */
  tick: (value: number, step: number) => string;
  /** A day's value in words, as the readout says it. */
  say: (value: number) => string;
  /** Said in place of the columns when every day is nothing. */
  empty: string;
}) {
  const [frame, width] = useWidth();
  const [active, setActive] = useState<number | null>(null);
  const live = useId();
  const told = useRef<HTMLSpanElement>(null);
  const largest = series.reduce((most, entry) => Math.max(most, entry.value), 0);
  const { top, step, ticks } = scaleOf(largest, whole);
  const labels = ticks.map((value) => tick(value, step));
  // The scale's numbers take the room their longest one needs.
  const left = 12 + Math.max(...labels.map((text) => text.length)) * 6.4;
  const inner = Math.max(0, width - left - RIGHT);
  const tall = HEIGHT - TOP - BOTTOM;
  const slot = series.length ? inner / series.length : 0;
  const thick = Math.max(1, Math.min(THICKEST, slot - GAP));
  const y = (value: number) => TOP + tall - (value / top) * tall;
  const centre = (index: number) => left + slot * index + slot / 2;
  // Of a few days each is named, or every other one when they are narrow. Of many, one
  // a week is, counted back from the last one; one every other week when they are narrow.
  const every = series.length <= 14 ? (slot >= 52 ? 1 : 2) : slot * 7 >= 52 ? 7 : 14;
  const named = (index: number) => (series.length - 1 - index) % every === 0;
  const peak = largest > 0 ? series.findIndex((entry) => entry.value === largest) : -1;

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const last = series.length - 1;
    const now = active ?? last;
    const next =
      event.key === 'ArrowLeft'
        ? Math.max(0, now - 1)
        : event.key === 'ArrowRight'
          ? Math.min(last, now + 1)
          : event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? last
              : null;
    if (next === null) return;
    event.preventDefault();
    setActive(next);
  };
  const shown = active !== null && active < series.length ? series[active] : null;
  useEffect(() => {
    if (told.current) told.current.textContent = shown ? `${longDay(shown.day)}: ${say(shown.value)}` : '';
  }, [shown, say]);

  return (
    <div
      ref={frame}
      role="group"
      tabIndex={0}
      aria-label={`${label}. The arrow keys read each day.`}
      aria-describedby={live}
      className="relative rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
      style={{ height: HEIGHT }}
      onKeyDown={move}
      onFocus={() => setActive((now) => now ?? series.length - 1)}
      onBlur={() => setActive(null)}
      onPointerLeave={() => setActive(null)}
    >
      <span id={live} ref={told} aria-live="polite" className="sr-only" />
      {width > 0 ? (
        <svg width={width} height={HEIGHT} aria-hidden="true" className="block">
          {ticks.map((value, index) => (
            <g key={value}>
              <line
                x1={left}
                x2={width - RIGHT}
                y1={Math.round(y(value)) + 0.5}
                y2={Math.round(y(value)) + 0.5}
                stroke="hsl(var(--border))"
                strokeWidth={1}
              />
              <text
                x={left - 8}
                y={y(value)}
                dy="0.32em"
                textAnchor="end"
                fontSize={11}
                fill="hsl(var(--muted-foreground))"
                style={{ fontVariantNumeric: 'tabular-nums' }}
              >
                {labels[index]}
              </text>
            </g>
          ))}
          {shown && active !== null ? (
            <rect x={left + slot * active} y={TOP} width={slot} height={tall} fill="hsl(var(--muted))" />
          ) : null}
          {series.map((entry, index) =>
            entry.value > 0 ? (
              <path
                key={entry.day}
                d={column(centre(index) - thick / 2, y(entry.value), thick, Math.max(1, TOP + tall - y(entry.value)))}
                fill={color}
              />
            ) : null,
          )}
          {peak >= 0 && active === null ? (
            <text
              x={Math.min(width - RIGHT, Math.max(left, centre(peak)))}
              y={y(largest) - 6}
              textAnchor="middle"
              fontSize={11}
              fontWeight={600}
              fill="hsl(var(--foreground))"
            >
              {say(largest)}
            </text>
          ) : null}
          {series.map((entry, index) =>
            named(index) ? (
              <text
                key={entry.day}
                x={centre(index)}
                y={HEIGHT - 7}
                textAnchor={index === series.length - 1 ? 'end' : 'middle'}
                dx={index === series.length - 1 ? slot / 2 : 0}
                fontSize={11}
                fill="hsl(var(--muted-foreground))"
              >
                {shortDay(entry.day)}
              </text>
            ) : null,
          )}
          {/* A day answers the pointer over its whole height, not only over its column. */}
          {series.map((entry, index) => (
            <rect
              key={entry.day}
              x={left + slot * index}
              y={0}
              width={slot}
              height={HEIGHT - BOTTOM}
              fill="transparent"
              onPointerEnter={() => setActive(index)}
              onPointerDown={() => setActive(index)}
            />
          ))}
        </svg>
      ) : null}
      {largest <= 0 && width > 0 ? (
        <p
          className="pointer-events-none absolute text-center text-[13px] text-muted-foreground"
          style={{ left, right: RIGHT, top: TOP + tall / 2 - 10 }}
        >
          {/* On the card's own colour, so that no line of the scale runs through the words. */}
          <span className="rounded px-2 py-0.5" style={{ background: 'hsl(var(--card))' }}>
            {empty}
          </span>
        </p>
      ) : null}
      {shown && active !== null ? (
        <div
          role="presentation"
          className="pointer-events-none absolute z-10 whitespace-nowrap rounded-md border border-border px-2.5 py-1.5 text-[12px] shadow-md"
          style={{
            left: Math.min(Math.max(centre(active), left + 60), Math.max(left + 60, width - RIGHT - 60)),
            top: Math.max(0, y(shown.value) - 8),
            transform: 'translate(-50%, -100%)',
            background: 'hsl(var(--popover))',
            color: 'hsl(var(--popover-foreground))',
          }}
        >
          <div className="font-semibold">{say(shown.value)}</div>
          <div className="text-muted-foreground">{longDay(shown.day)}</div>
        </div>
      ) : null}
    </div>
  );
}
