import { ArrowDown, ArrowUp, Circle, Lock, Pause, Play } from 'lucide-react';
import type { LiveAsset } from '../../api/types';
import { cents, fmtCountdown, kfmt, maybe, money, num, regimeOf, MINUS, DASH, CENT } from '../../lib/format';
import { ASSET_COLOR, ASSET_DP, regimeChipStyle } from '../../lib/palette';
import { Sparkline } from '../../components/Sparkline';
import { useValueFlash } from '../../lib/useValueFlash';

export interface CardComputed {
  edgeCents: number; // best-side net edge in cents (for |EDGE| sort)
  sessionNet: number;
}

export function computeEdge(la: LiveAsset): { side: 'YES' | 'NO'; cents: number } | null {
  const ey = la.snapshot.edge_yes_net;
  const en = la.snapshot.edge_no_net;
  if (ey === null && en === null) return null;
  const yes = ey ?? -Infinity;
  const no = en ?? -Infinity;
  return yes >= no ? { side: 'YES', cents: yes * 100 } : { side: 'NO', cents: no * 100 };
}

export function AssetCard({
  la,
  history,
  nowSec,
  wsConnected,
  killEngaged,
  threshold,
  onOpen,
  onTogglePause,
}: {
  la: LiveAsset;
  history: number[];
  nowSec: number;
  wsConnected: boolean;
  killEngaged: boolean;
  threshold: number;
  onOpen: () => void;
  onTogglePause: () => void;
}) {
  const snap = la.snapshot;
  const sym = la.asset;
  const color = ASSET_COLOR[sym];
  const dp = ASSET_DP[sym];

  const remaining = Math.max(0, la.market.close_ts - nowSec);
  const regime = regimeOf(remaining);
  const rawAge = Math.max(0, nowSec - snap.ts);
  const assetStale = rawAge > 5;
  const stale = assetStale || !wsConnected;
  const staleAge = Math.floor(rawAge);

  const flash = useValueFlash(snap.spot, !stale);
  const spotColor = flash === 1 ? 'var(--green)' : flash === -1 ? 'var(--red)' : 'var(--fg)';

  const dist = snap.distance_dollars ?? (snap.spot !== null && snap.floor_strike !== null ? snap.spot - snap.floor_strike : null);

  const edge = computeEdge(la);
  const clears = edge !== null && edge.cents >= threshold && !la.paused && regime !== 'SETTLEMENT';

  const series = stale && history.length === 0 ? [] : history;

  return (
    <div
      onClick={onOpen}
      className="bg-panel rounded-md overflow-hidden cursor-pointer border transition-colors hover:border-indigoline"
      style={{
        opacity: stale ? 0.55 : la.paused ? 0.75 : 1,
        borderColor: clears ? 'var(--edge-border)' : 'var(--line)',
        boxShadow: clears ? '0 0 0 1px var(--edge-ring)' : 'none',
      }}
    >
      {/* header row */}
      <div className="flex items-center gap-2 px-[13px] pt-[11px] pb-[7px]">
        <span className="w-2 h-2 rounded-[2px] inline-block shrink-0" style={{ background: color }} />
        <span className="text-[15px] font-extrabold text-fg">{sym}</span>
        <span
          className="text-[8px] font-bold tracking-[0.1em] px-[7px] py-0.5 rounded-[3px] inline-flex items-center gap-1"
          style={regimeChipStyle(regime)}
        >
          {regime === 'SETTLEMENT' && <Lock size={8} />}
          {regime === 'SETTLEMENT' ? 'SETTLEMENT · LOCKED' : regime}
        </span>
        {la.paused && (
          <span className="text-[8px] font-bold tracking-[0.1em] px-[7px] py-0.5 rounded-[3px] bg-graychip text-dim border border-linestrong2 inline-flex items-center gap-1">
            <Pause size={8} />
            PAUSED
          </span>
        )}
        {stale && (
          <span className="text-[8px] font-bold tracking-[0.1em] px-[7px] py-0.5 rounded-[3px] bg-amberbg text-amber">
            STALE {staleAge}s
          </span>
        )}
        {killEngaged && (
          <span className="text-[8px] font-bold tracking-[0.1em] px-[7px] py-0.5 rounded-[3px] bg-redbg text-red">
            HALTED
          </span>
        )}
        <span className="flex-1" />
        <button
          onClick={(e) => {
            e.stopPropagation();
            onTogglePause();
          }}
          title={la.paused ? `resume ${sym}` : `pause ${sym}`}
          className="text-ghost hover:text-dim bg-transparent border-0 p-0.5 cursor-pointer"
        >
          {la.paused ? <Play size={10} /> : <Pause size={10} />}
        </button>
        <span
          className="text-[22px] font-extrabold leading-none"
          style={{ color: regime === 'SETTLEMENT' ? 'var(--reg-set-fg)' : 'var(--fg)' }}
        >
          {fmtCountdown(remaining)}
        </span>
      </div>

      {/* ticker */}
      <div className="px-[13px] pb-1 text-[8px] text-ghost overflow-hidden text-ellipsis whitespace-nowrap">
        {la.market.ticker}
      </div>

      {/* sparkline + spot overlay */}
      <div className="relative">
        <Sparkline series={series} strike={snap.floor_strike} color={color} height={48} />
        <div className="absolute top-0.5 left-[13px] text-[10px]">
          <span style={{ color: spotColor, transition: 'color 0.25s' }}>
            {maybe(snap.spot, (v) => num(v, dp))}
          </span>{' '}
          {dist !== null ? (
            <span className="inline-flex items-center gap-px" style={{ color: dist >= 0 ? 'var(--green)' : 'var(--red)' }}>
              {dist >= 0 ? <ArrowUp size={9} className="inline" /> : <ArrowDown size={9} className="inline" />}
              {(dist >= 0 ? '+' : MINUS) + num(Math.abs(dist), dp)}
            </span>
          ) : (
            <span className="text-ghost">{DASH}</span>
          )}{' '}
          <span className="text-ghost">vs {maybe(snap.floor_strike, (v) => num(v, dp))}</span>
        </div>
      </div>

      {/* probability block — the core comparison */}
      <div className="px-[13px] py-[9px] flex flex-col gap-1.5 border-t border-linesub">
        <div className="flex items-center gap-2 text-[9px]">
          <span className="text-faint w-[46px] tracking-[0.08em] shrink-0">MARKET</span>
          <div className="flex-1 h-[11px] bg-track rounded-[3px]">
            <div
              className="h-full bg-indigo rounded-[3px]"
              style={{ width: `${Math.min(100, Math.max(0, (snap.implied_prob ?? 0) * 100))}%` }}
            />
          </div>
          <span className="w-10 text-right text-[11px] font-bold text-fg">
            {maybe(snap.implied_prob, (v) => cents(v))}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[9px]">
          <span className="text-faint w-[46px] tracking-[0.08em] shrink-0">MODEL</span>
          <div className="flex-1 h-[11px] bg-track rounded-[3px]">
            <div
              className="h-full rounded-[3px]"
              style={{
                width: `${Math.min(100, Math.max(0, (snap.model_prob ?? 0) * 100))}%`,
                background: 'var(--model-bar)',
              }}
            />
          </div>
          <span className="w-10 text-right text-[11px] font-bold text-fg">
            {maybe(snap.model_prob, (v) => cents(v))}
          </span>
        </div>
        {edge !== null ? (
          <div className="text-[9px]" style={{ color: clears ? 'var(--green)' : 'var(--dim)' }}>
            edge {edge.side} {(edge.cents >= 0 ? '+' : MINUS) + Math.abs(edge.cents).toFixed(1)}
            {CENT} net — {clears ? 'clears' : 'below'} {threshold}
            {CENT} threshold
          </div>
        ) : (
          <div className="text-[9px] text-ghost">edge {DASH} net unavailable</div>
        )}
      </div>

      {/* book row */}
      <div className="px-[13px] py-[7px] border-t border-linesub flex gap-3 text-[9px] text-dim">
        <span>
          YES{' '}
          <span className="text-bright">
            {snap.yes_bid !== null && snap.yes_ask !== null
              ? `${(snap.yes_bid * 100).toFixed(1)}/${(snap.yes_ask * 100).toFixed(1)}`
              : DASH}
          </span>
        </span>
        <span>
          SPR <span className="text-bright">{maybe(snap.spread_cents, (v) => v.toFixed(1) + CENT)}</span>
        </span>
        <span>
          DEPTH <span className="text-bright">{maybe(snap.depth_yes_within_2c, kfmt)}</span>
        </span>
        <span>
          BOOK <span className="text-bright">{assetStale ? `${staleAge}s` : maybe(snap.book_age_seconds, (v) => v.toFixed(1) + 's')}</span>
        </span>
      </div>

      {/* position row (only when open) */}
      {la.position && (
        <div className="px-[13px] py-[7px] border-t border-linesub flex justify-between text-[9px]">
          <span className="text-dim">
            POS{' '}
            <span
              className="font-bold"
              style={{ color: la.position.side === 'yes' ? 'var(--green)' : 'var(--red)' }}
            >
              {la.position.side.toUpperCase()} {la.position.contracts} @ {(la.position.avg_price * 100).toFixed(1)}
              {CENT}
            </span>
          </span>
          <span style={{ color: la.position.unrealized_pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
            uPnL {money(la.position.unrealized_pnl)}
          </span>
        </div>
      )}

      {/* footer: smart money + session pnl */}
      <div className="px-[13px] py-[7px] border-t border-linesub flex justify-between items-center text-[10px]">
        <span
          className="inline-flex items-center gap-1"
          style={{
            color: la.smart_money
              ? la.smart_money.lean === 'UP'
                ? 'var(--green)'
                : 'var(--red)'
              : 'var(--faint)',
          }}
        >
          {la.smart_money ? (
            <>
              {la.smart_money.lean === 'UP' ? <ArrowUp size={10} /> : <ArrowDown size={10} />}
              {la.smart_money.strength.toFixed(2)}
            </>
          ) : (
            <>
              <Circle size={7} fill="currentColor" />
              neutral
            </>
          )}{' '}
          <span className="text-ghost text-[8px] tracking-[0.08em]">SMART $</span>
        </span>
        <span
          style={{
            color:
              la.session_pnl.net > 0 ? 'var(--green)' : la.session_pnl.net < 0 ? 'var(--red)' : 'var(--faint)',
          }}
        >
          {money(la.session_pnl.net)}{' '}
          <span className="text-ghost">
            net ·{' '}
            {la.session_pnl.trades > 0 ? (
              <>
                <span style={{ color: 'var(--green)' }}>{la.session_pnl.wins}W</span>
                –
                <span style={{ color: 'var(--red)' }}>
                  {la.session_pnl.trades - la.session_pnl.wins}L
                </span>
              </>
            ) : (
              '0t'
            )}
          </span>
        </span>
      </div>
    </div>
  );
}
