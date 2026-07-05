import { useEffect, useMemo, useState } from 'react';
import { X } from 'lucide-react';
import { api } from '../../api/client';
import type { FeatureSnapshot, LiveAsset, Trade } from '../../api/types';
import { cents, fmtCountdown, fmtUtcTime, kfmt, money, num, regimeOf, signedCents, CENT, MINUS } from '../../lib/format';
import { ASSET_COLOR, ASSET_DP, regimeChipStyle, regimeFg } from '../../lib/palette';
import { Sparkline } from '../../components/Sparkline';
import { useApp } from '../../store/store';

interface FieldRow {
  k: string;
  v: string;
  color?: string;
  ghost?: boolean;
}

function row(k: string, raw: unknown, fmt: (v: never) => string, color?: string): FieldRow {
  if (raw === null || raw === undefined) return { k, v: 'null', ghost: true };
  return { k, v: fmt(raw as never), color };
}

function buildFields(snap: FeatureSnapshot, dp: number): FieldRow[] {
  const edgeCol = (v: number) => (v > 0 ? 'var(--green)' : 'var(--red)');
  return [
    row('schema_version', snap.schema_version, (v: number) => `v${v}`),
    row('ts', snap.ts, (v: number) => v.toFixed(3)),
    row('spot', snap.spot, (v: number) => num(v, dp)),
    row('spot_source', snap.spot_source, (v: string) => v),
    row('spot_age_seconds', snap.spot_age_seconds, (v: number) => v.toFixed(2)),
    row('floor_strike', snap.floor_strike, (v: number) => num(v, dp)),
    row('distance_dollars', snap.distance_dollars, (v: number) => (v >= 0 ? '+' : MINUS) + num(Math.abs(v), dp)),
    row('distance_z', snap.distance_z, (v: number) => v.toFixed(2)),
    row('seconds_remaining', snap.seconds_remaining, (v: number) => v.toFixed(1)),
    { k: 'regime', v: snap.regime, color: regimeFg(snap.regime) },
    row('ret_30s', snap.ret_30s, (v: number) => v.toFixed(6)),
    row('ret_1m', snap.ret_1m, (v: number) => v.toFixed(6)),
    row('ret_5m', snap.ret_5m, (v: number) => v.toFixed(6)),
    row('realized_vol_5m', snap.realized_vol_5m, (v: number) => v.toFixed(7)),
    row('btc_ret_30s', snap.btc_ret_30s, (v: number) => v.toFixed(6)),
    row('btc_ret_1m', snap.btc_ret_1m, (v: number) => v.toFixed(6)),
    row('btc_implied_prob', snap.btc_implied_prob, (v: number) => cents(v)),
    row('yes_bid', snap.yes_bid, (v: number) => cents(v)),
    row('yes_ask', snap.yes_ask, (v: number) => cents(v)),
    row('implied_prob', snap.implied_prob, (v: number) => cents(v)),
    row('spread_cents', snap.spread_cents, (v: number) => v.toFixed(1)),
    row('depth_yes_within_2c', snap.depth_yes_within_2c, kfmt),
    row('depth_no_within_2c', snap.depth_no_within_2c, kfmt),
    row('flow_imbalance', snap.flow_imbalance, (v: number) => v.toFixed(3)),
    row('book_age_seconds', snap.book_age_seconds, (v: number) => v.toFixed(1)),
    row('model_prob', snap.model_prob, (v: number) => cents(v)),
    row('edge_yes_gross', snap.edge_yes_gross, signedCents, snap.edge_yes_gross !== null ? edgeCol(snap.edge_yes_gross) : undefined),
    row('edge_yes_net', snap.edge_yes_net, signedCents, snap.edge_yes_net !== null ? edgeCol(snap.edge_yes_net) : undefined),
    row('edge_no_gross', snap.edge_no_gross, signedCents, snap.edge_no_gross !== null ? edgeCol(snap.edge_no_gross) : undefined),
    row('edge_no_net', snap.edge_no_net, signedCents, snap.edge_no_net !== null ? edgeCol(snap.edge_no_net) : undefined),
    row('smart_lean', snap.smart_lean, (v: string) => v, snap.smart_lean === 'UP' ? 'var(--green)' : snap.smart_lean === 'DOWN' ? 'var(--red)' : undefined),
    row('smart_strength', snap.smart_strength, (v: number) => v.toFixed(2)),
  ];
}

/** Right-side 420px drawer over a scrim: full FeatureSnapshot + this-window trades. */
export function DetailDrawer({ la, onClose }: { la: LiveAsset; onClose: () => void }) {
  const { nowSec, getSpotHistory, recentTrades } = useApp();
  const [fetched, setFetched] = useState<Trade[]>([]);
  const sym = la.asset;
  const snap = la.snapshot;
  const color = ASSET_COLOR[sym];
  const dp = ASSET_DP[sym];

  const remaining = Math.max(0, (la.market?.close_ts ?? nowSec) - nowSec);
  const regime = regimeOf(remaining);
  const age = snap ? Math.max(0, nowSec - snap.ts) : 0;
  const stale = age > 5;

  useEffect(() => {
    let cancelled = false;
    api
      .trades({ asset: sym, limit: 50 })
      .then((r) => {
        if (!cancelled) setFetched(r.trades);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [sym]);

  const openTs = la.market?.open_ts ?? Infinity;  // rollover gap: no window yet
  const windowTrades = useMemo(() => {
    const seen = new Set<number>();
    return [...recentTrades, ...fetched]
      .filter((t) => t.asset === sym && t.ts >= openTs)
      .filter((t) => (seen.has(t.id) ? false : (seen.add(t.id), true)))
      .sort((a, b) => b.ts - a.ts);
  }, [recentTrades, fetched, sym, openTs]);

  const fields = snap ? buildFields(snap, dp) : [];

  if (!la.market || !snap) {
    // rollover gap: close the drawer's content gracefully
    return (
      <div className="fixed inset-0 z-40" onClick={onClose}>
        <div className="absolute inset-0" style={{ background: 'rgba(0,0,0,0.5)' }} />
        <div
          className="absolute right-0 top-0 bottom-0 w-[420px] bg-panel border-l border-line p-4 text-[10px] text-faint"
          onClick={(e) => e.stopPropagation()}
        >
          {sym}: waiting for the next window to list…
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="fixed inset-0 z-[90]" style={{ background: 'var(--scrim)' }} onClick={onClose} />
      <div className="fixed top-0 right-0 bottom-0 w-[420px] max-w-[94vw] bg-drawerbg border-l border-line z-[91] overflow-y-auto p-[18px]">
        <div className="flex items-center gap-2.5 mb-1">
          <span className="w-[9px] h-[9px] rounded-[2px] inline-block" style={{ background: color }} />
          <span className="text-[18px] font-extrabold text-fg">{sym}</span>
          <span className="text-[8px] font-bold tracking-[0.1em] px-2 py-0.5 rounded-[3px]" style={regimeChipStyle(regime)}>
            {regime}
          </span>
          <span className="flex-1" />
          <button onClick={onClose} className="text-faint hover:text-fg cursor-pointer bg-transparent border-0 p-1">
            <X size={14} />
          </button>
        </div>
        <div className="text-[9px] text-ghost mb-3.5">
          {la.market.ticker} · schema v{snap.schema_version} · snapshot age {age < 1 ? age.toFixed(1) : Math.floor(age)}s
          {stale ? ' (STALE)' : ''} · closes in {fmtCountdown(remaining)}
        </div>

        <div className="mb-4">
          <Sparkline series={getSpotHistory(sym)} strike={snap.floor_strike} color={color} height={80} strokeWidth={0.9} />
        </div>

        <div className="text-[8px] tracking-[0.12em] text-faint mb-2">FEATURE SNAPSHOT</div>
        <div className="grid grid-cols-2 gap-px bg-linesub border border-linesub rounded-md overflow-hidden mb-4">
          {fields.map((f) => (
            <div key={f.k} className="bg-panel px-2.5 py-[7px] flex justify-between gap-2">
              <span className="text-[8px] text-faint">{f.k}</span>
              <span
                className="text-[9px] text-right"
                style={{ color: f.ghost ? 'var(--ghost)' : (f.color ?? 'var(--bright)') }}
              >
                {f.v}
              </span>
            </div>
          ))}
        </div>

        <div className="text-[8px] tracking-[0.12em] text-faint mb-2">TRADES · THIS WINDOW</div>
        {windowTrades.length === 0 ? (
          <div className="border border-dashed border-linestrong rounded-md p-4 text-center text-[9px] text-faint">
            no fills in this window
          </div>
        ) : (
          windowTrades.map((t) => (
            <div
              key={t.id}
              className="flex justify-between items-center px-2.5 py-2 border border-linesub rounded-[5px] mb-1.5 text-[9px]"
            >
              <span className="text-dim">{fmtUtcTime(t.ts)}</span>
              <span className="font-bold" style={{ color: t.intent === 'BUY_YES' ? 'var(--green)' : 'var(--red)' }}>
                {t.intent}
              </span>
              <span className="text-fg">
                {t.contracts} @ {(t.avg_fill_price * 100).toFixed(1)}
                {CENT}
              </span>
              {t.result === null ? (
                <span className="text-ghost">unsettled</span>
              ) : (
                <span className="font-bold" style={{ color: t.pnl_net >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {money(t.pnl_net)}
                </span>
              )}
            </div>
          ))
        )}
      </div>
    </>
  );
}
