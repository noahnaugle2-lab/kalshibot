import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { LeaderboardBasis, LeaderboardRow, Regime } from '../../api/types';
import { pct } from '../../lib/format';
import { ASSET_COLOR, regimeFg } from '../../lib/palette';

interface RegimeStat {
  name: Regime;
  pf: string;
  hit: string;
  n: number;
}

interface TodStat {
  name: string;
  pf: string;
  n: number;
}

/** Expanded leaderboard row: per-regime + time-of-day mini-tables, equity
 *  sparkline and a HISTORY deep link. Regime stats come from re-querying the
 *  leaderboard endpoint with ?regime=; time-of-day is derived client-side
 *  from the trades endpoint (no dedicated endpoint exists in the contract). */
export function RowExpansion({
  row,
  basis,
  smartMoney,
  onGoHistory,
}: {
  row: LeaderboardRow;
  basis: LeaderboardBasis;
  smartMoney: 'with' | 'without';
  onGoHistory: () => void;
}) {
  const [regimes, setRegimes] = useState<RegimeStat[] | null>(null);
  const [tod, setTod] = useState<TodStat[] | null>(null);
  const [equityPts, setEquityPts] = useState<number[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    const regs: Regime[] = ['EARLY', 'MID', 'LATE'];
    Promise.all(regs.map((r) => api.leaderboard({ basis, smart_money: smartMoney, regime: r })))
      .then((results) => {
        if (cancelled) return;
        setRegimes(
          regs.map((name, i) => {
            const r = results[i].rows.find((x) => x.asset === row.asset);
            return {
              name,
              pf: r?.profit_factor != null ? r.profit_factor.toFixed(2) : '—',
              hit: r?.hit_rate != null ? pct(r.hit_rate) : '—',
              n: r?.n_trades ?? 0,
            };
          }),
        );
      })
      .catch(() => {});

    api
      .trades({ asset: row.asset, limit: 200 })
      .then((r) => {
        if (cancelled) return;
        const blocks: Array<{ name: string; wins: number; losses: number; n: number }> = [
          { name: '00–06', wins: 0, losses: 0, n: 0 },
          { name: '06–12', wins: 0, losses: 0, n: 0 },
          { name: '12–18', wins: 0, losses: 0, n: 0 },
          { name: '18–24', wins: 0, losses: 0, n: 0 },
        ];
        for (const t of r.trades) {
          if (t.result === null) continue;
          const h = Number(
            new Intl.DateTimeFormat('en-US', {
              timeZone: 'America/New_York', hour: 'numeric', hour12: false,
            }).format(new Date(t.ts * 1000)),
          ) % 24;
          const b = blocks[Math.floor(h / 6)];
          b.n++;
          if (t.pnl_net >= 0) b.wins += t.pnl_net;
          else b.losses += -t.pnl_net;
        }
        setTod(
          blocks.map((b) => ({
            name: b.name,
            pf: b.n === 0 ? '—' : b.losses === 0 ? '∞' : (b.wins / b.losses).toFixed(2),
            n: b.n,
          })),
        );
      })
      .catch(() => {});

    api
      .equity({ assets: [row.asset], basis })
      .then((r) => {
        if (cancelled) return;
        setEquityPts(r.series[0]?.points.map((p) => p[1]) ?? []);
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, [row.asset, basis, smartMoney]);

  let spark = '';
  let zeroY = '38.0';
  if (equityPts && equityPts.length > 1) {
    const lo = Math.min(...equityPts, 0);
    const hi = Math.max(...equityPts, 0);
    const y = (v: number) => 38 - ((v - lo) / (hi - lo || 1)) * 36;
    spark = equityPts.map((v, j) => `${((j / (equityPts.length - 1)) * 200).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    zeroY = y(0).toFixed(1);
  }

  return (
    <div className="flex flex-wrap gap-6 px-5 py-3.5 border-b border-linesub bg-expandbg items-start">
      <div>
        <div className="text-[8px] tracking-[0.12em] text-faint mb-1.5">BY REGIME</div>
        <div className="grid grid-cols-[70px_50px_50px_44px] gap-1.5 text-[9px]">
          <span className="text-faint text-[8px]">REGIME</span>
          <span className="text-faint text-[8px] text-right">PF</span>
          <span className="text-faint text-[8px] text-right">HIT</span>
          <span className="text-faint text-[8px] text-right">N</span>
          {(regimes ?? []).map((g) => (
            <span key={g.name} className="contents">
              <span style={{ color: regimeFg(g.name) }}>{g.name}</span>
              <span className="text-right font-bold text-fg">{g.pf}</span>
              <span className="text-right text-fg">{g.hit}</span>
              <span className="text-right text-dim">{g.n}</span>
            </span>
          ))}
          {!regimes && <span className="text-ghost col-span-4">loading…</span>}
        </div>
      </div>

      <div>
        <div className="text-[8px] tracking-[0.12em] text-faint mb-1.5">TIME OF DAY (ET)</div>
        <div className="grid grid-cols-[70px_50px_44px] gap-1.5 text-[9px]">
          <span className="text-faint text-[8px]">BLOCK</span>
          <span className="text-faint text-[8px] text-right">PF</span>
          <span className="text-faint text-[8px] text-right">N</span>
          {(tod ?? []).map((g) => (
            <span key={g.name} className="contents">
              <span className="text-dim">{g.name}</span>
              <span className="text-right font-bold text-fg">{g.pf}</span>
              <span className="text-right text-dim">{g.n}</span>
            </span>
          ))}
          {!tod && <span className="text-ghost col-span-3">loading…</span>}
        </div>
      </div>

      <div className="flex-1 min-w-[200px]">
        <div className="text-[8px] tracking-[0.12em] text-faint mb-1.5">EQUITY (NET, CAMPAIGN)</div>
        <svg width="100%" height="64" viewBox="0 0 200 40" preserveAspectRatio="none" className="block">
          <line x1="0" x2="200" y1={zeroY} y2={zeroY} style={{ stroke: 'var(--linestrong2)' }} strokeWidth={0.5} strokeDasharray="2 2" />
          {spark && <polyline points={spark} fill="none" stroke={ASSET_COLOR[row.asset]} strokeWidth={1} />}
        </svg>
      </div>

      <div className="flex flex-col gap-2 text-[9px]">
        <span className="text-dim">
          streak <span className="text-fg font-bold">{row.longest_losing_streak}L</span> · vol{' '}
          <span className="text-fg font-bold">{row.pnl_volatility}</span> · slip{' '}
          <span className="text-fg font-bold">{row.avg_slippage_cents}¢</span>
        </span>
        <button
          onClick={onGoHistory}
          className="text-indigo cursor-pointer font-bold tracking-[0.06em] bg-transparent border-0 p-0 text-left text-[9px]"
        >
          → HISTORY · {row.asset}
        </button>
      </div>
    </div>
  );
}
