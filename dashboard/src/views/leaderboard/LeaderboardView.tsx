import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type LeaderboardRegimeFilter } from '../../api/client';
import type {
  Asset,
  LeaderboardBasis,
  LeaderboardResponse,
  LeaderboardRow,
  SmartMoneyFilter,
} from '../../api/types';
import { fmtBrier, fmtUtcClock, maybe, money, pct, signedCents, CENT, MINUS } from '../../lib/format';
import { ASSET_COLOR, confidencePillStyle, recommendationPillStyle } from '../../lib/palette';
import { EmptyState } from '../../components/EmptyState';
import { FetchError } from '../../components/FetchError';
import { MicroLabel, SegButton } from '../../components/SegButton';
import { useApp } from '../../store/store';
import { RowExpansion } from './RowExpansion';

const GRID = '60px 52px minmax(140px,1fr) 56px 56px 56px 48px 48px 78px 48px 46px 50px 56px 66px 78px';

type SortKey = 'pf' | 'pl' | 'hit' | 'n';

const SORT_VALUE: Record<SortKey, (r: LeaderboardRow) => number> = {
  pf: (r) => r.profit_factor ?? -Infinity,
  pl: (r) => r.pl_ratio_pct ?? -Infinity,
  hit: (r) => r.hit_rate ?? -Infinity,
  n: (r) => r.n_trades,
};

/** Null-safe fixed-point ("—" fallback). */
const nf = (v: number | null | undefined, dp = 2, fb = '\u2014') =>
  v == null || Number.isNaN(v) ? fb : v.toFixed(dp);

/** Profit factor display: null = no losses yet (∞ when winning) or no data. */
const pfText = (r: LeaderboardRow) =>
  r.profit_factor == null
    ? r.n_trades > 0 && (r.hit_rate ?? 0) >= 1
      ? '\u221E'
      : '\u2014'
    : r.profit_factor.toFixed(2);

const CONF_LABEL = { high: 'HIGH', medium: 'MED', low: 'LOW' } as const;

export function LeaderboardView() {
  const { epoch, equity, status, nowSec } = useApp();
  const navigate = useNavigate();
  const [basis, setBasis] = useState<LeaderboardBasis>('net');
  const [sm, setSm] = useState<SmartMoneyFilter>('with');
  const [regime, setRegime] = useState<LeaderboardRegimeFilter>('all');
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'pf', dir: -1 });
  const [openRow, setOpenRow] = useState<Asset | null>(null);
  const [data, setData] = useState<LeaderboardResponse | null>(null);
  const [without, setWithout] = useState<LeaderboardResponse | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [retry, setRetry] = useState(0);

  // The regime filter and smart-money with/without re-query the API.
  useEffect(() => {
    let cancelled = false;
    const primary = sm === 'both' ? 'with' : sm;
    api
      .leaderboard({ basis, smart_money: primary, regime })
      .then((r) => {
        if (cancelled) return;
        setData(r);
        setLoadError(false);
      })
      .catch(() => !cancelled && setLoadError(true));
    if (sm === 'both') {
      api
        .leaderboard({ basis, smart_money: 'without', regime })
        .then((r) => !cancelled && setWithout(r))
        .catch(() => !cancelled && setLoadError(true));
    } else {
      setWithout(null);
    }
    return () => {
      cancelled = true;
    };
  }, [basis, sm, regime, epoch, retry]);

  const rows = useMemo(() => {
    if (!data) return [];
    const val = SORT_VALUE[sort.key];
    return [...data.rows].sort((a, b) => (sort.dir === -1 ? val(b) - val(a) : val(a) - val(b)));
  }, [data, sort]);

  const smDelta = useMemo(() => {
    if (sm !== 'both' || !data || !without) return null;
    const map: Partial<Record<Asset, number>> = {};
    for (const r of data.rows) {
      const w = without.rows.find((x) => x.asset === r.asset);
      if (w && r.profit_factor != null && w.profit_factor != null)
        map[r.asset] = r.profit_factor - w.profit_factor;
    }
    return map;
  }, [sm, data, without]);

  const campaignTotal = useMemo(() => {
    if (!equity) return 0;
    return equity.series.reduce((sum, s) => sum + (s.points.length ? s.points[s.points.length - 1][1] : 0), 0);
  }, [equity]);

  const campaignDay = status ? Math.max(1, Math.floor((nowSec - status.started_at) / 86400) + 1) : 1;

  const header = (label: string, key: SortKey) => {
    const active = sort.key === key;
    return (
      <button
        onClick={() => setSort((s) => ({ key, dir: s.key === key ? ((-s.dir) as 1 | -1) : -1 }))}
        className={
          'text-right cursor-pointer bg-transparent border-0 p-0 text-[8px] tracking-[0.1em] ' +
          (active ? 'text-indigosoft' : 'text-faint')
        }
      >
        {label} {active ? (sort.dir === -1 ? '↓' : '↑') : ''}
      </button>
    );
  };

  const empty = data !== null && data.rows.length === 0;

  return (
    <div className="px-5 py-[18px] max-w-[1440px] mx-auto">
      {loadError && <FetchError label="leaderboard" onRetry={() => setRetry((n) => n + 1)} />}
      <div className="flex items-center gap-4 mb-3.5 flex-wrap">
        <span className="text-[13px] font-extrabold tracking-[0.06em] text-fg">LEADERBOARD</span>
        <span className="flex-1" />
        <div className="flex items-center gap-1.5">
          <MicroLabel>BASIS</MicroLabel>
          <SegButton active={basis === 'net'} onClick={() => setBasis('net')}>NET</SegButton>
          <SegButton active={basis === 'gross'} onClick={() => setBasis('gross')}>GROSS</SegButton>
        </div>
        <div className="flex items-center gap-1.5">
          <MicroLabel>SMART $</MicroLabel>
          <SegButton active={sm === 'with'} onClick={() => setSm('with')}>WITH</SegButton>
          <SegButton active={sm === 'without'} onClick={() => setSm('without')}>WITHOUT</SegButton>
          <SegButton active={sm === 'both'} onClick={() => setSm('both')}>BOTH</SegButton>
        </div>
        <div className="flex items-center gap-1.5">
          <MicroLabel>REGIME</MicroLabel>
          {(['all', 'EARLY', 'MID', 'LATE'] as const).map((r) => (
            <SegButton key={r} active={regime === r} onClick={() => setRegime(r)}>
              {r === 'all' ? 'ALL' : r}
            </SegButton>
          ))}
        </div>
      </div>

      <div className="text-[10px] text-dim leading-[1.6] max-w-[860px] -mt-1.5 mb-3.5">
        The money screen: nine markets ranked head-to-head by profitability and predictability across the shadow
        campaign. Sample sizes are always shown — a lucky small sample never outranks proven edge. Click any row
        for per-regime and time-of-day breakdowns and its equity curve.
      </div>

      {empty ? (
        <EmptyState
          title="CAMPAIGN DAY 1"
          subtitle="metrics appear after the first settled windows — first 15-minute settlement expected within the hour"
        />
      ) : (
        <>
          <div className="bg-panel border border-line rounded-md overflow-x-auto">
            <div className="min-w-[1240px]">
              {/* header row */}
              <div
                className="grid gap-2 px-3.5 py-[9px] text-faint tracking-[0.1em] text-[8px] border-b border-line items-center"
                style={{ gridTemplateColumns: GRID }}
              >
                <span># / CONF</span>
                <span>ASSET</span>
                <span>STRATEGY</span>
                {header('PF', 'pf')}
                {header('P/L%', 'pl')}
                <span className="text-right">{CENT}/CT</span>
                <span className="text-right">ROC</span>
                {header('HIT', 'hit')}
                <span className="text-right">BRIER M/MKT</span>
                <span className="text-right">SIG/D</span>
                <span className="text-right">FILL</span>
                <span className="text-right">SPRD</span>
                <span className="text-right">MAXDD</span>
                {header('N', 'n')}
                <span className="text-right">REC</span>
              </div>

              {rows.map((r, i) => {
                const open = openRow === r.asset;
                const badge = r.n_trades < 30 ? 'INSUFF' : r.n_trades < 100 ? 'LOW' : null;
                const better =
                  r.brier_model != null && r.brier_market != null && r.brier_model < r.brier_market;
                const delta = smDelta?.[r.asset];
                return (
                  <div key={r.asset}>
                    <div
                      onClick={() => setOpenRow(open ? null : r.asset)}
                      className={
                        'grid gap-2 px-3.5 py-[9px] border-b border-linesub text-[10px] items-center cursor-pointer hover:bg-panel2 ' +
                        (open ? 'bg-expandbg' : '')
                      }
                      style={{ gridTemplateColumns: GRID }}
                    >
                      <span className="flex items-center gap-[5px]">
                        <span className="font-extrabold text-[12px] text-fg">{i + 1}</span>
                        <span
                          className="text-[7px] font-bold tracking-[0.06em] px-1 py-px rounded-[2px]"
                          style={confidencePillStyle(r.confidence)}
                        >
                          {CONF_LABEL[r.confidence]}
                        </span>
                      </span>
                      <span className="flex items-center gap-[5px]">
                        <span className="w-[7px] h-[7px] rounded-[2px]" style={{ background: ASSET_COLOR[r.asset] }} />
                        <span className="font-extrabold text-fg">{r.asset}</span>
                      </span>
                      <span className="text-dim text-[9px] overflow-hidden text-ellipsis whitespace-nowrap">
                        {r.strategy}
                      </span>
                      <span className="text-right font-extrabold text-[13px] text-fg">
                        {pfText(r)}
                        {delta != null && !Number.isNaN(delta) && (
                          <span
                            className="block text-[8px] font-normal"
                            style={{ color: delta >= 0 ? 'var(--green)' : 'var(--red)' }}
                          >
                            {(delta >= 0 ? '+' : MINUS) + Math.abs(delta).toFixed(2)} sm
                          </span>
                        )}
                      </span>
                      <span className="text-right text-fg">
                        {r.pl_ratio_pct == null
                          ? '\u2014'
                          : (r.pl_ratio_pct >= 0 ? '+' : '') + r.pl_ratio_pct.toFixed(0) + '%'}
                      </span>
                      <span className="text-right text-fg">{maybe(r.net_pnl_per_contract, signedCents)}</span>
                      <span className="text-right text-fg">
                        {r.return_on_capital == null
                          ? '\u2014'
                          : (r.return_on_capital >= 0 ? '+' : MINUS) +
                            Math.abs(r.return_on_capital * 100).toFixed(0) + '%'}
                      </span>
                      <span className="text-right text-fg">{maybe(r.hit_rate, (v) => pct(v))}</span>
                      <span className="text-right">
                        <span
                          className="font-bold"
                          style={{
                            color: better ? 'var(--green)' : 'var(--dim)',
                            borderBottom: better ? '1px solid var(--green)' : 'none',
                          }}
                        >
                          {maybe(r.brier_model, fmtBrier)}
                        </span>
                        <span className="text-faint">/{maybe(r.brier_market, fmtBrier)}</span>
                      </span>
                      <span className="text-right text-dim">{nf(r.signals_per_day, 1)}</span>
                      <span className="text-right text-dim">{maybe(r.fill_rate, (v) => pct(v))}</span>
                      <span className="text-right text-dim">
                        {maybe(r.avg_spread_cents, (v) => v.toFixed(1) + CENT)}
                      </span>
                      <span className="text-right text-redsoft">${nf(r.max_drawdown, 2)}</span>
                      <span className="text-right text-fg">
                        {r.n_trades}{' '}
                        {badge && (
                          <span
                            className={
                              'px-1 py-px rounded-[2px] text-[7px] font-bold ' +
                              (badge === 'INSUFF' ? 'bg-redbg text-red' : 'bg-amberbg text-amber')
                            }
                          >
                            {badge}
                          </span>
                        )}
                      </span>
                      <span className="text-right">
                        <span
                          className="px-[7px] py-0.5 rounded-[3px] text-[8px] font-bold tracking-[0.08em]"
                          style={recommendationPillStyle(r.recommendation)}
                        >
                          {r.recommendation.toUpperCase()}
                        </span>
                      </span>
                    </div>
                    {open && (
                      <RowExpansion
                        row={r}
                        basis={basis}
                        smartMoney={sm === 'both' ? 'with' : sm}
                        onGoHistory={() => navigate(`/history?asset=${r.asset}`)}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* legend */}
          <div className="mt-2.5 text-[9px] text-faint flex flex-wrap gap-x-[18px] gap-y-1">
            <span>
              <span className="text-green">▐</span> brier model &lt; market = bot better calibrated than the market
              (exploitability signal)
            </span>
            <span>
              n &lt; 30 <span className="bg-redbg text-red px-1 py-px rounded-[2px] text-[7px] font-bold">INSUFF</span>{' '}
              · 30–99 <span className="bg-amberbg text-amber px-1 py-px rounded-[2px] text-[7px] font-bold">LOW</span>
            </span>
            {data && <span>computed {fmtUtcClock(data.computed_at)} · version latest</span>}
          </div>

          {/* bottom analytics row */}
          <div className="grid grid-cols-1 lg:grid-cols-[300px_1fr_1fr] gap-3.5 mt-3.5">
            {/* campaign summary */}
            <div className="bg-panel border border-line rounded-md p-3.5 flex flex-col gap-3">
              <div className="text-[9px] font-extrabold tracking-[0.12em] text-dim">CAMPAIGN SUMMARY</div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <div className="text-[8px] tracking-[0.1em] text-faint mb-[3px]">TOTAL NET PNL</div>
                  <div
                    className="text-[18px] font-extrabold"
                    style={{ color: campaignTotal >= 0 ? 'var(--green)' : 'var(--red)' }}
                  >
                    {money(campaignTotal)}
                  </div>
                </div>
                <div>
                  <div className="text-[8px] tracking-[0.1em] text-faint mb-[3px]">TOTAL TRADES</div>
                  <div className="text-[18px] font-extrabold text-fg">
                    {rows.reduce((t, r) => t + r.n_trades, 0).toLocaleString()}
                  </div>
                </div>
                <div>
                  <div className="text-[8px] tracking-[0.1em] text-faint mb-[3px]">SETTLED WINDOWS</div>
                  <div className="text-[18px] font-extrabold text-fg">
                    {rows.reduce((t, r) => t + r.n_settled_windows, 0).toLocaleString()}
                  </div>
                </div>
                <div>
                  <div className="text-[8px] tracking-[0.1em] text-faint mb-[3px]">AVG FILL RATE</div>
                  <div className="text-[18px] font-extrabold text-fg">
                    {rows.length ? Math.round((rows.reduce((t, r) => t + (r.fill_rate ?? 0), 0) / rows.length) * 100) + '%' : '—'}
                  </div>
                </div>
              </div>
              <div className="border-t border-linesub pt-2.5 flex flex-col gap-1.5 text-[10px]">
                {rows.length > 0 && (
                  <>
                    <div className="flex justify-between">
                      <span className="text-faint">best market</span>
                      <span>
                        <span className="font-extrabold text-fg">
                          {[...rows].sort((a, b) => (b.profit_factor ?? 0) - (a.profit_factor ?? 0))[0].asset}
                        </span>{' '}
                        <span className="text-green">
                          PF {[...rows].sort((a, b) => (b.profit_factor ?? 0) - (a.profit_factor ?? 0))[0].profit_factor?.toFixed(2) ?? '\u2014'}
                        </span>
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-faint">worst market</span>
                      <span>
                        <span className="font-extrabold text-fg">
                          {[...rows].sort((a, b) => (a.profit_factor ?? 0) - (b.profit_factor ?? 0))[0].asset}
                        </span>{' '}
                        <span className="text-red">
                          PF {[...rows].sort((a, b) => (a.profit_factor ?? 0) - (b.profit_factor ?? 0))[0].profit_factor?.toFixed(2) ?? '\u2014'}
                        </span>
                      </span>
                    </div>
                  </>
                )}
                <div className="flex justify-between">
                  <span className="text-faint">campaign day</span>
                  <span className="font-extrabold text-fg">{campaignDay} of 14</span>
                </div>
              </div>
            </div>

            {/* profit factor bars */}
            <div className="bg-panel border border-line rounded-md p-3.5 flex flex-col gap-2">
              <div className="flex justify-between items-baseline">
                <span className="text-[9px] font-extrabold tracking-[0.12em] text-dim">PROFIT FACTOR</span>
                <span className="text-[8px] text-faint">┊ break-even 1.00</span>
              </div>
              {rows.map((r) => (
                <div key={r.asset} className="flex items-center gap-2 text-[9px]">
                  <span className="w-9 font-extrabold text-fg flex items-center gap-1">
                    <span className="w-[5px] h-[5px] rounded-[1px]" style={{ background: ASSET_COLOR[r.asset] }} />
                    {r.asset}
                  </span>
                  <span className="flex-1 relative h-[9px] bg-track rounded-[2px]">
                    <span
                      className="absolute left-0 top-0 bottom-0 rounded-[2px] opacity-85"
                      style={{
                        width: `${Math.min((r.profit_factor ?? 0) / 2, 1) * 100}%`,
                        background: (r.profit_factor ?? 0) >= 1 ? 'var(--indigo)' : 'var(--pf-neg)',
                      }}
                    />
                    <span className="absolute left-1/2 -top-0.5 -bottom-0.5 w-px" style={{ background: 'var(--strike)' }} />
                  </span>
                  <span
                    className="w-8 text-right font-bold"
                    style={{ color: (r.profit_factor ?? 0) >= 1 ? 'var(--fg)' : 'var(--red-soft)' }}
                  >
                    {pfText(r)}
                  </span>
                </div>
              ))}
            </div>

            {/* calibration dumbbells */}
            <div className="bg-panel border border-line rounded-md p-3.5 flex flex-col gap-2">
              <div className="flex justify-between items-baseline">
                <span className="text-[9px] font-extrabold tracking-[0.12em] text-dim">CALIBRATION · BRIER</span>
                <span className="text-[8px] text-faint">
                  <span className="text-indigosoft">●</span> model <span className="text-faint">●</span> market ·
                  lower is better
                </span>
              </div>
              {rows.map((r) => {
                if (r.brier_model == null || r.brier_market == null) return null;
                const pos = (v: number) => Math.max(0, Math.min(100, ((v - 0.15) / 0.15) * 100)) * 0.94;
                const mL = pos(r.brier_model);
                const kL = pos(r.brier_market);
                const better = r.brier_model < r.brier_market;
                return (
                  <div key={r.asset} className="flex items-center gap-2 text-[9px]">
                    <span className="w-9 font-extrabold text-fg">{r.asset}</span>
                    <span className="flex-1 relative h-[9px]">
                      <span className="absolute left-0 right-0 top-1 h-px bg-linesub" />
                      <span
                        className="absolute h-0.5 opacity-50"
                        style={{
                          left: `${Math.min(mL, kL) + 1}%`,
                          width: `${Math.abs(mL - kL)}%`,
                          top: '3.5px',
                          background: better ? 'var(--green)' : 'var(--red)',
                        }}
                      />
                      <span
                        className="absolute w-[7px] h-[7px] rounded-full"
                        style={{ left: `${kL}%`, top: 1, background: 'var(--faint)' }}
                      />
                      <span
                        className="absolute w-[7px] h-[7px] rounded-full"
                        style={{ left: `${mL}%`, top: 1, background: 'var(--indigo-soft)' }}
                      />
                    </span>
                    <span className="w-[60px] text-right">
                      <span className="font-bold" style={{ color: better ? 'var(--green)' : 'var(--dim)' }}>
                        {maybe(r.brier_model, fmtBrier)}
                      </span>
                      <span className="text-faint">/{maybe(r.brier_market, fmtBrier)}</span>
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
