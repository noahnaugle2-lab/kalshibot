import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ArrowDown, ArrowUp, Circle } from 'lucide-react';
import { api } from '../../api/client';
import type { Asset, EquityResponse, LeaderboardBasis, Trade } from '../../api/types';
import { fmtUtcDate, fmtUtcHm, fmtUtcTime, money, CENT } from '../../lib/format';
import { ASSETS, ASSET_COLOR, regimeFg } from '../../lib/palette';
import { EmptyState } from '../../components/EmptyState';
import { FetchError } from '../../components/FetchError';
import { MicroLabel, SegButton } from '../../components/SegButton';
import { useApp } from '../../store/store';

type HistRange = '24h' | '7d' | 'campaign';
const PAGE_SIZE = 24;

const selectCls =
  'bg-panel2 border border-linestrong text-bright text-[9px] px-2 py-1 rounded outline-none cursor-pointer';

export function HistoryView() {
  const { epoch, nowSec, recentTrades, live } = useApp();
  const [searchParams] = useSearchParams();
  const [range, setRange] = useState<HistRange>('campaign');
  const [basis, setBasis] = useState<LeaderboardBasis>('net');
  const [assetFilter, setAssetFilter] = useState<string>(() => {
    const a = searchParams.get('asset');
    return a && (ASSETS as string[]).includes(a) ? a : 'ALL';
  });
  const [sourceFilter, setSourceFilter] = useState<string>('ALL');
  const [hidden, setHidden] = useState<Partial<Record<Asset, boolean>>>({});
  const [hover, setHover] = useState<{
    asset: Asset; ts: number; pnl: number; cx: number; cy: number;
    px: number; py: number;
  } | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [equity, setEquity] = useState<EquityResponse | null>(null);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [cursor, setCursor] = useState<string>('');
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [retry, setRetry] = useState(0);

  // deep link from leaderboard (→ HISTORY · SYM)
  useEffect(() => {
    const a = searchParams.get('asset');
    if (a && (ASSETS as string[]).includes(a)) setAssetFilter(a);
  }, [searchParams]);

  // equity is basis-aware; range slicing/rebasing is client-side
  useEffect(() => {
    let cancelled = false;
    api
      .equity({ basis })
      .then((r) => {
        if (cancelled) return;
        setEquity(r);
        setLoadError(false);
      })
      .catch(() => !cancelled && setLoadError(true));
    return () => {
      cancelled = true;
    };
  }, [basis, epoch, retry]);

  // trade log is server-paginated per the contract (asset param + cursor)
  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    api
      .trades({ asset: assetFilter === 'ALL' ? undefined : (assetFilter as Asset), limit: PAGE_SIZE })
      .then((r) => {
        if (cancelled) return;
        setTrades(r.trades);
        setCursor(r.cursor);
        setLoaded(true);
        setLoadError(false);
      })
      .catch(() => !cancelled && setLoadError(true));
    return () => {
      cancelled = true;
    };
  }, [assetFilter, epoch, retry]);

  const loadMore = () => {
    api
      .trades({ asset: assetFilter === 'ALL' ? undefined : (assetFilter as Asset), limit: PAGE_SIZE, cursor })
      .then((r) => {
        setTrades((prev) => [...prev, ...r.trades]);
        setCursor(r.cursor);
      })
      .catch(() => {});
  };

  const cutoff = range === '24h' ? nowSec - 86400 : range === '7d' ? nowSec - 604800 : 0;

  // merge live WS trade pushes into the visible log
  const allTrades = useMemo(() => {
    const seen = new Set(trades.map((t) => t.id));
    const extra = recentTrades.filter(
      (t) => !seen.has(t.id) && (assetFilter === 'ALL' || t.asset === assetFilter),
    );
    return [...extra, ...trades];
  }, [trades, recentTrades, assetFilter]);

  const visibleTrades = allTrades.filter(
    (t) =>
      t.ts >= cutoff &&
      (sourceFilter === 'ALL' || t.decision_source === sourceFilter),
  );

  // ---- equity chart geometry ----
  const chart = useMemo(() => {
    if (!equity) return null;
    const sliced = equity.series
      // paused assets (e.g. BTC kept as a data source) stay off the chart
      .filter((s) => !live[s.asset]?.paused)
      .map((s) => {
      const pts = s.points.filter((p) => p[0] >= cutoff);
      const base = pts.length ? pts[0][1] : 0;
      return { asset: s.asset, pts: pts.map((p) => [p[0], p[1] - base] as [number, number]) };
    });
    const visible = sliced.filter((s) => !hidden[s.asset]);
    const all = visible.flatMap((s) => s.pts.map((p) => p[1]));
    const lo = Math.min(...all, 0);
    const hi = Math.max(...all, 1);
    const tLo = Math.min(...visible.flatMap((s) => s.pts.map((p) => p[0])), nowSec);
    const tHi = Math.max(...visible.flatMap((s) => s.pts.map((p) => p[0])), nowSec);
    const y = (v: number) => 232 - ((v - lo) / (hi - lo || 1)) * 224;
    const x = (t: number) => ((t - tLo) / (tHi - tLo || 1)) * 800;
    const series = visible
      .filter((s) => s.pts.length > 1)
      .map((s) => ({
        asset: s.asset,
        d: s.pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' '),
        scaled: s.pts.map((p) => ({ cx: x(p[0]), cy: y(p[1]), ts: p[0], pnl: p[1] })),
      }));
    // legend PnL over the range
    const legend = sliced.map((s) => ({
      asset: s.asset,
      pnl: s.pts.length ? s.pts[s.pts.length - 1][1] : 0,
    }));
    // x-axis labels
    const nLabels = range === 'campaign' ? 4 : 5;
    const labels: string[] = [];
    let prevDate = '';
    for (let i = 0; i < nLabels; i++) {
      const t = tLo + ((tHi - tLo) * i) / (nLabels - 1);
      if (range === '24h') {
        const d = fmtUtcDate(t);
        labels.push(d !== prevDate ? `${d} ${fmtUtcHm(t)}` : fmtUtcHm(t));
        prevDate = d;
      } else {
        labels.push(fmtUtcDate(t));
      }
    }
    return { series, legend, zeroY: y(0).toFixed(1), labels, lo, hi };
  }, [equity, cutoff, hidden, nowSec, range, live]);

  const dayOne = loaded && allTrades.length === 0 && (!equity || equity.series.every((s) => s.points.length < 2));

  // shared hover resolver so a finger tap/drag (pointer events) scrubs the
  // chart on the owner's iPhone, not just a desktop mouse
  const resolveHover = (clientX: number, clientY: number, rect: DOMRect) => {
    if (!chart) return;
    const mx = ((clientX - rect.left) / rect.width) * 800;
    const my = ((clientY - rect.top) / rect.height) * 240;
    let best: typeof hover = null;
    let bestDist = 26; // viewBox-units grab radius
    for (const s of chart.series) {
      let nearest = s.scaled[0];
      let dx = Infinity;
      for (const p of s.scaled) {
        const d = Math.abs(p.cx - mx);
        if (d < dx) { dx = d; nearest = p; }
      }
      const dist = Math.abs(nearest.cy - my);
      if (dist < bestDist) {
        bestDist = dist;
        best = {
          asset: s.asset, ts: nearest.ts, pnl: nearest.pnl,
          cx: nearest.cx, cy: nearest.cy,
          px: ((clientX - rect.left) / rect.width) * 100,
          py: ((clientY - rect.top) / rect.height) * 100,
        };
      }
    }
    setHover(best);
  };

  return (
    <div className="px-5 py-[18px] max-w-[1440px] mx-auto flex flex-col gap-4">
      <div className="flex items-center gap-4 flex-wrap">
        <span className="text-[13px] font-extrabold tracking-[0.06em] text-fg">HISTORY</span>
        <span className="flex-1" />
        <div className="flex items-center gap-1.5">
          <MicroLabel>RANGE</MicroLabel>
          <SegButton active={range === '24h'} onClick={() => setRange('24h')}>24H</SegButton>
          <SegButton active={range === '7d'} onClick={() => setRange('7d')}>7D</SegButton>
          <SegButton active={range === 'campaign'} onClick={() => setRange('campaign')}>CAMPAIGN</SegButton>
        </div>
        <div className="flex items-center gap-1.5">
          <MicroLabel>BASIS</MicroLabel>
          <SegButton active={basis === 'net'} onClick={() => setBasis('net')}>NET</SegButton>
          <SegButton active={basis === 'gross'} onClick={() => setBasis('gross')}>GROSS</SegButton>
        </div>
      </div>

      <div className="text-[10px] text-dim leading-[1.6] max-w-[860px] -mt-1.5 mb-0.5">
        Every simulated fill of the campaign: cumulative PnL per asset (toggle lines via the legend) and the full
        trade log. Expand any Claude-decided trade to read its reasoning, confidence, and decision latency. Net
        figures include Kalshi fees; flip the basis to compare.
      </div>

      {loadError && <FetchError label="history" onRetry={() => setRetry((n) => n + 1)} />}

      {dayOne ? (
        <EmptyState
          title="NO SETTLED TRADES YET"
          subtitle="the equity curves and trade log fill in as windows settle — check back after the first quarter-hour"
        />
      ) : (
        <>
          {/* equity chart */}
          <div className="bg-panel border border-line rounded-md p-3.5">
            <div className="flex gap-2 flex-wrap mb-2.5">
              {(chart?.legend ?? []).map((l) => (
                <button
                  key={l.asset}
                  onClick={() => setHidden((h) => ({ ...h, [l.asset]: !h[l.asset] }))}
                  className="flex items-center gap-[5px] text-[9px] font-bold px-[9px] py-[3px] rounded cursor-pointer border border-line bg-transparent text-fg"
                  style={{ opacity: hidden[l.asset] ? 0.3 : 1 }}
                >
                  <span className="w-[7px] h-[7px] rounded-[2px]" style={{ background: ASSET_COLOR[l.asset] }} />
                  {l.asset}{' '}
                  <span style={{ color: l.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>{money(l.pnl)}</span>
                </button>
              ))}
            </div>
            <div className="relative">
            {/* y-axis dollar labels (HTML overlay, not SVG <text> — the chart
                uses preserveAspectRatio="none" which would squash SVG text) */}
            {chart && (
              <div className="absolute left-0 top-0 bottom-5 w-full pointer-events-none text-[8px] text-ghost z-[1]">
                <span className="absolute left-0 top-0">{money(chart.hi)}</span>
                <span className="absolute left-0" style={{ top: `${(Number(chart.zeroY) / 240) * 100}%` }}>
                  $0
                </span>
                <span className="absolute left-0 bottom-0">{money(chart.lo)}</span>
              </div>
            )}
            <svg
              width="100%"
              height="260"
              viewBox="0 0 800 240"
              preserveAspectRatio="none"
              className="block touch-none"
              onMouseLeave={() => setHover(null)}
              onMouseMove={(e) => resolveHover(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect())}
              onPointerDown={(e) => resolveHover(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect())}
              onPointerMove={(e) => {
                if (e.pointerType === 'touch') {
                  resolveHover(e.clientX, e.clientY, e.currentTarget.getBoundingClientRect());
                }
              }}
              onPointerLeave={() => setHover(null)}
            >
              <line
                x1="0"
                x2="800"
                y1={chart?.zeroY ?? '120'}
                y2={chart?.zeroY ?? '120'}
                style={{ stroke: 'var(--linestrong2)' }}
                strokeWidth={0.7}
                strokeDasharray="3 3"
              />
              {(chart?.series ?? []).map((s) => (
                <polyline
                  key={s.asset}
                  points={s.d}
                  fill="none"
                  stroke={ASSET_COLOR[s.asset]}
                  strokeWidth={hover?.asset === s.asset ? 2.4 : 1.2}
                  opacity={hover && hover.asset !== s.asset ? 0.35 : 0.9}
                />
              ))}
              {hover && (
                <circle
                  cx={hover.cx}
                  cy={hover.cy}
                  r={3.5}
                  fill={ASSET_COLOR[hover.asset]}
                  stroke="var(--panel)"
                  strokeWidth={1.2}
                />
              )}
            </svg>
            {hover && (
              <div
                className="absolute pointer-events-none z-10 bg-panel2 border border-strong rounded px-2 py-1 text-[9px] whitespace-nowrap"
                style={{
                  left: `${Math.min(hover.px, 82)}%`,
                  top: `${Math.max(hover.py - 12, 0)}%`,
                }}
              >
                <span className="inline-block w-[7px] h-[7px] rounded-[2px] mr-1 align-middle"
                  style={{ background: ASSET_COLOR[hover.asset] }} />
                <span className="font-extrabold text-fg">{hover.asset}</span>{' '}
                <span style={{ color: hover.pnl >= 0 ? 'var(--green)' : 'var(--red)' }}>
                  {money(hover.pnl)}
                </span>{' '}
                <span className="text-faint">{fmtUtcTime(hover.ts)}</span>
              </div>
            )}
            </div>
            <div className="flex justify-between text-[8px] text-ghost mt-1">
              {(chart?.labels ?? []).map((l, i) => (
                <span key={i}>{l}</span>
              ))}
            </div>
          </div>

          {/* trade log */}
          <div className="bg-panel border border-line rounded-md overflow-hidden">
            <div className="px-3.5 py-2.5 border-b border-line flex items-center gap-3 flex-wrap">
              <span className="text-[10px] font-extrabold tracking-[0.1em] text-fg">TRADE LOG</span>
              <span className="flex-1" />
              <span className="text-[9px] text-faint tracking-[0.08em]">ASSET</span>
              <select value={assetFilter} onChange={(e) => setAssetFilter(e.target.value)} className={selectCls}>
                <option value="ALL">ALL</option>
                {ASSETS.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
              <span className="text-[9px] text-faint tracking-[0.08em]">SOURCE</span>
              <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)} className={selectCls}>
                <option value="ALL">ALL</option>
                <option value="claude">CLAUDE</option>
                <option value="baseline">BASELINE</option>
              </select>
            </div>

            <div className="overflow-x-auto">
              <div className="min-w-[960px]">
                <div className="grid grid-cols-[88px_46px_74px_40px_56px_50px_50px_66px_minmax(120px,1fr)_60px_70px] gap-2 px-3.5 py-2 text-faint tracking-[0.1em] text-[8px] border-b border-linesub">
                  <span>TIME</span>
                  <span>ASSET</span>
                  <span>INTENT</span>
                  <span className="text-right">QTY</span>
                  <span className="text-right">FILL</span>
                  <span className="text-right">FEES</span>
                  <span>RESULT</span>
                  <span className="text-right">NET PNL</span>
                  <span>STRATEGY</span>
                  <span>REGIME</span>
                  <span>SOURCE</span>
                </div>
                {visibleTrades.map((t) => {
                  const isClaude = t.decision_source === 'claude' && t.claude !== null;
                  const open = expanded === t.id && isClaude;
                  const displayPnl = basis === 'gross' ? t.pnl_net + t.fees : t.pnl_net;
                  return (
                    <div key={t.id}>
                      <div
                        onClick={() => isClaude && setExpanded(open ? null : t.id)}
                        className={
                          'grid grid-cols-[88px_46px_74px_40px_56px_50px_50px_66px_minmax(120px,1fr)_60px_70px] gap-2 px-3.5 py-2 border-b border-linesub text-[9px] items-center hover:bg-panel2 ' +
                          (isClaude ? 'cursor-pointer' : 'cursor-default')
                        }
                      >
                        <span className="text-dim">{fmtUtcTime(t.ts)}</span>
                        <span className="font-extrabold text-fg">{t.asset}</span>
                        <span
                          className="font-bold"
                          style={{ color: t.intent === 'BUY_YES' ? 'var(--green)' : 'var(--red)' }}
                        >
                          {t.intent}
                        </span>
                        <span className="text-right text-fg">{t.contracts}</span>
                        <span className="text-right text-fg">
                          {(t.avg_fill_price * 100).toFixed(1)}
                          {CENT}
                        </span>
                        <span className="text-right text-dim">${t.fees.toFixed(2)}</span>
                        {t.result === null ? (
                          <span className="text-ghost">—</span>
                        ) : (
                          <span
                            className="font-bold"
                            style={{ color: t.result === 'yes' ? 'var(--green)' : 'var(--red)' }}
                          >
                            {t.result.toUpperCase()}
                          </span>
                        )}
                        {t.result === null ? (
                          <span className="text-right text-ghost">—</span>
                        ) : (
                          <span
                            className="text-right font-bold"
                            style={{ color: displayPnl >= 0 ? 'var(--green)' : 'var(--red)' }}
                          >
                            {money(displayPnl)}
                          </span>
                        )}
                        <span className="text-faint text-[8px] overflow-hidden text-ellipsis whitespace-nowrap">
                          {t.strategy}
                        </span>
                        <span className="text-[8px] font-bold" style={{ color: regimeFg(t.regime_at_entry) }}>
                          {t.regime_at_entry}
                        </span>
                        <span>
                          <span
                            className={
                              'px-1.5 py-px rounded-[3px] text-[8px] font-bold ' +
                              (t.decision_source === 'claude'
                                ? 'bg-indigobg text-indigosoft'
                                : 'bg-graychip text-dim')
                            }
                          >
                            {(t.decision_source ?? '—').toUpperCase()}
                          </span>
                        </span>
                      </div>
                      {open && t.claude && (
                        <div className="px-5 py-3 border-b border-linesub bg-expandbg flex flex-wrap gap-7 text-[9px]">
                          <div className="max-w-[640px]">
                            <div className="text-[8px] tracking-[0.12em] text-faint mb-[5px]">
                              CLAUDE REASONING · confidence {t.claude.confidence.toFixed(2)} ·{' '}
                              {t.claude.latency_ms}ms
                            </div>
                            <div className="leading-[1.6]" style={{ color: 'var(--dim)' }}>
                              {t.claude.reasoning}
                            </div>
                          </div>
                          <div>
                            <div className="text-[8px] tracking-[0.12em] text-faint mb-[5px]">SMART $ AT ENTRY</div>
                            <div
                              className="font-bold inline-flex items-center gap-1"
                              style={{
                                color:
                                  t.smart_money_lean === 'UP'
                                    ? 'var(--green)'
                                    : t.smart_money_lean === 'DOWN'
                                      ? 'var(--red)'
                                      : 'var(--faint)',
                              }}
                            >
                              {t.smart_money_lean === 'UP' ? (
                                <>
                                  <ArrowUp size={10} /> UP
                                </>
                              ) : t.smart_money_lean === 'DOWN' ? (
                                <>
                                  <ArrowDown size={10} /> DOWN
                                </>
                              ) : (
                                <>
                                  <Circle size={7} fill="currentColor" /> neutral
                                </>
                              )}
                            </div>
                          </div>
                          <div>
                            <div className="text-[8px] tracking-[0.12em] text-faint mb-[5px]">MARKET</div>
                            <div className="text-dim">{t.market_ticker}</div>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="px-3.5 py-[9px] text-[9px] text-faint flex justify-between">
              <span>{visibleTrades.length} trades shown · server-paginated (cursor)</span>
              {cursor ? (
                <button
                  onClick={loadMore}
                  className="text-indigo cursor-pointer font-bold bg-transparent border-0 p-0 text-[9px]"
                >
                  LOAD MORE →
                </button>
              ) : (
                <span className="text-ghost">end of log</span>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
