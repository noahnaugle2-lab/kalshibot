/** Hand-rolled SVG sparkline with an optional dashed strike line.
 *  viewBox 120x32, preserveAspectRatio=none (full-bleed, like the prototype).
 *  The polyline is mapped into the LOWER band (y 11..30) so the spot-price
 *  overlay text in the top band never collides with the line. */
export function Sparkline({
  series,
  strike,
  color,
  height,
  strokeWidth = 1,
}: {
  series: number[];
  strike: number | null;
  color: string;
  height: number;
  strokeWidth?: number;
}) {
  const vals = strike !== null ? [...series, strike] : series;
  let points = '';
  let strikeY: string | null = null;
  if (vals.length > 0) {
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = (hi - lo) * 0.12 || 1;
    const l = lo - pad;
    const h = hi + pad;
    const y = (v: number) => 30 - ((v - l) / (h - l)) * 28;
    if (series.length > 1) {
      points = series
        .map((v, i) => `${((i / (series.length - 1)) * 120).toFixed(1)},${y(v).toFixed(1)}`)
        .join(' ');
    }
    if (strike !== null) strikeY = y(strike).toFixed(1);
  }
  return (
    <svg width="100%" height={height} viewBox="0 0 120 32" preserveAspectRatio="none" className="block">
      {strikeY !== null && (
        <line x1="0" x2="120" y1={strikeY} y2={strikeY} style={{ stroke: 'var(--strike)' }} strokeWidth={0.5} strokeDasharray="2 2" />
      )}
      {points && <polyline points={points} fill="none" stroke={color} strokeWidth={strokeWidth} opacity={0.85} />}
    </svg>
  );
}
