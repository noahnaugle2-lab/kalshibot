import type { Regime } from '../api/types';

/** True minus sign (U+2212) per the design spec. */
export const MINUS = '−';
export const DASH = '—'; // em dash used for null/ghost values
export const CENT = '¢';

/** Signed money: "+$9.85" / "−$3.40". */
export function money(v: number): string {
  return (v >= 0 ? '+$' : `${MINUS}$`) + Math.abs(v).toFixed(2);
}

/** Locale number with fixed decimals ("63,209.98"). */
export function num(v: number, dp: number): string {
  return v.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

/** Price/probability in cents, 1 decimal: 0.992 → "99.2¢". */
export function cents(p: number, dp = 1): string {
  return (p * 100).toFixed(dp) + CENT;
}

/** Signed cents value from a dollar edge: 0.028 → "+2.8¢". */
export function signedCents(p: number, dp = 1): string {
  return (p >= 0 ? '+' : MINUS) + Math.abs(p * 100).toFixed(dp) + CENT;
}

export function signedNum(v: number, dp: number): string {
  return (v >= 0 ? '+' : MINUS) + num(Math.abs(v), dp);
}

export function signedPct(v: number, dp = 0): string {
  return (v >= 0 ? '+' : MINUS) + Math.abs(v * 100).toFixed(dp) + '%';
}

export function pct(v: number, dp = 0): string {
  return (v * 100).toFixed(dp) + '%';
}

/** "05:12" from seconds. */
export function fmtCountdown(s: number): string {
  const t = Math.max(0, Math.floor(s));
  const m = Math.floor(t / 60);
  const r = t % 60;
  return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
}

// All display times are US Eastern, 12-hour clock (owner preference).
// Intl handles EST/EDT automatically via the IANA zone.
const TZ = 'America/New_York';

function parts(ts: number, opts: Intl.DateTimeFormatOptions): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of new Intl.DateTimeFormat('en-US', { timeZone: TZ, ...opts }).formatToParts(
    new Date(ts * 1000),
  )) {
    out[p.type] = p.value;
  }
  return out;
}

/** "JUL 03 2:22 PM" (ET). */
export function fmtUtcTime(ts: number): string {
  const p = parts(ts, { month: 'short', day: '2-digit', hour: 'numeric', minute: '2-digit', hour12: true });
  return `${p.month.toUpperCase()} ${p.day} ${p.hour}:${p.minute} ${p.dayPeriod}`;
}

/** "JUL 03" (ET). */
export function fmtUtcDate(ts: number): string {
  const p = parts(ts, { month: 'short', day: '2-digit' });
  return `${p.month.toUpperCase()} ${p.day}`;
}

/** "4:45:02 PM ET". */
export function fmtUtcClock(ts: number): string {
  const p = parts(ts, { hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
  return `${p.hour}:${p.minute}:${p.second} ${p.dayPeriod} ET`;
}

/** "6:00 PM" (ET). */
export function fmtUtcHm(ts: number): string {
  const p = parts(ts, { hour: 'numeric', minute: '2-digit', hour12: true });
  return `${p.hour}:${p.minute} ${p.dayPeriod}`;
}

/** 79794 → "79.8k"; 950 → "950". */
export function kfmt(v: number): string {
  if (Math.abs(v) >= 1000) return (v / 1000).toFixed(1) + 'k';
  return String(Math.round(v));
}

/** Brier score display ".19" style. */
export function fmtBrier(v: number): string {
  return v.toFixed(2).replace(/^0/, '');
}

/** Regime from seconds remaining (documented boundaries). */
export function regimeOf(secondsRemaining: number): Regime {
  if (secondsRemaining > 600) return 'EARLY';
  if (secondsRemaining > 300) return 'MID';
  if (secondsRemaining > 90) return 'LATE';
  return 'SETTLEMENT';
}

/** Format a nullable value, falling back to an em dash. */
export function maybe<T>(v: T | null | undefined, f: (x: T) => string, fallback = DASH): string {
  return v === null || v === undefined ? fallback : f(v);
}
