import { useEffect, useState } from 'react';
import { ArrowDown, ArrowUp, Circle, Copy, ExternalLink, Trophy } from 'lucide-react';
import { api } from '../../api/client';
import type { SmartMoneyResponse } from '../../api/types';
import { pct } from '../../lib/format';
import { ASSETS, ASSET_COLOR } from '../../lib/palette';
import { FetchError } from '../../components/FetchError';
import { useApp } from '../../store/store';

function LeanGlyph({ lean, strength }: { lean: 'UP' | 'DOWN' | null; strength?: number }) {
  if (!lean) {
    return (
      <span className="text-faint inline-flex items-center">
        <Circle size={7} fill="currentColor" />
      </span>
    );
  }
  return (
    <span
      className="font-bold inline-flex items-center gap-0.5"
      style={{ color: lean === 'UP' ? 'var(--green)' : 'var(--red)' }}
    >
      {lean === 'UP' ? <ArrowUp size={10} /> : <ArrowDown size={10} />}
      {strength !== undefined ? strength.toFixed(2) : ''}
    </span>
  );
}

export function SmartMoneyView() {
  const { epoch, showToast } = useApp();
  const [data, setData] = useState<SmartMoneyResponse | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .smartmoney()
      .then((r) => {
        if (cancelled) return;
        setData(r);
        setLoadError(false);
      })
      .catch(() => !cancelled && setLoadError(true));
    return () => {
      cancelled = true;
    };
  }, [epoch, retry]);

  const copyAddress = (addr: string) => {
    if (navigator.clipboard) void navigator.clipboard.writeText(addr);
    showToast('address copied');
  };

  return (
    <div className="px-5 py-[18px] max-w-[1440px] mx-auto flex flex-col gap-4">
      <div className="flex items-center gap-4">
        <span className="text-[13px] font-extrabold tracking-[0.06em] text-fg">SMART MONEY</span>
      </div>
      <div className="text-[10px] text-dim leading-[1.6] max-w-[860px] -mt-1.5 mb-0.5">
        Two independent signal layers — order-flow patterns on Kalshi (Layer A) and consistently profitable
        Polymarket wallets (Layer B) — merged into one directional lean per asset. Strategies weight this lean
        from 0 to 1 in Config; patterns that stop working get benched automatically.
      </div>

      {loadError && <FetchError label="smart money" onRetry={() => setRetry((n) => n + 1)} />}

      {/* merged leans strip */}
      <div className="bg-panel border border-line rounded-md px-3.5 py-3 flex gap-2.5 flex-wrap items-center">
        <span className="text-[8px] tracking-[0.12em] text-faint mr-1">MERGED LEANS</span>
        {ASSETS.map((sym) => {
          const lean = data?.merged_leans[sym];
          return (
            <span
              key={sym}
              className="flex items-center gap-1.5 bg-panel2 border border-line rounded px-2.5 py-[5px] text-[10px]"
            >
              <span className="w-1.5 h-1.5 rounded-[2px]" style={{ background: ASSET_COLOR[sym] }} />
              <span className="font-extrabold text-fg">{sym}</span>
              <LeanGlyph lean={lean?.lean ?? null} strength={lean?.strength} />
            </span>
          );
        })}
      </div>

      {/* Independent wallet-consensus entry experiment */}
      <div className="bg-panel border border-line rounded-md overflow-hidden">
        <div className="px-3.5 py-2.5 border-b border-line text-[10px] font-extrabold tracking-[0.1em] text-fg">
          WALLET CONSENSUS · READ-ONLY DRY RUN
        </div>
        <div className="px-3.5 py-2 text-[9px] text-dim border-b border-linesub">
          Top copyable wallets per asset · first 300 seconds only · 8 effective wallets · 65% weighted agreement · 3¢ model edge
        </div>
        <div className="overflow-x-auto">
          <div className="min-w-[800px]">
            <div className="grid grid-cols-[70px_90px_100px_100px_110px_1fr] gap-2.5 px-3.5 py-2 text-faint tracking-[0.1em] text-[8px] border-b border-linesub">
              <span>ASSET</span><span>LEAN</span><span className="text-right">ACTIVE</span>
              <span className="text-right">CONSENSUS</span><span className="text-right">SETTLED · NET</span><span>STATUS</span>
            </div>
            {(data?.wallet_consensus.latest_observations ?? []).map((o) => {
              const summary = data?.wallet_consensus.summary.find((s) => s.asset === o.asset);
              return (
                <div key={o.asset} className="grid grid-cols-[70px_90px_100px_100px_110px_1fr] gap-2.5 px-3.5 py-2.5 border-b border-linesub text-[10px] items-center">
                  <span className="font-extrabold text-fg">{o.asset}</span>
                  <LeanGlyph lean={o.lean === 'NEUTRAL' ? null : o.lean} />
                  <span className="text-right text-dim">{o.active_wallets} · {o.effective_wallets.toFixed(1)} eff</span>
                  <span className="text-right font-bold text-fg">{o.dominant_share == null ? '—' : pct(o.dominant_share)}</span>
                  <span className="text-right text-dim">
                    {summary?.settled ?? 0} · <span style={{ color: (summary?.net ?? 0) >= 0 ? 'var(--green)' : 'var(--red)' }}>
                      {(summary?.net ?? 0) >= 0 ? '+' : '−'}${Math.abs(summary?.net ?? 0).toFixed(2)}
                    </span>
                  </span>
                  <span className={o.eligible ? 'text-green' : 'text-faint'}>{o.reason}</span>
                </div>
              );
            })}
            {(data?.wallet_consensus.latest_observations ?? []).length === 0 && (
              <div className="px-3.5 py-4 text-faint text-[9px]">awaiting the first ranked-wallet observation</div>
            )}
          </div>
        </div>
      </div>

      {/* Layer A — flow patterns */}
      <div className="bg-panel border border-line rounded-md overflow-hidden">
        <div className="px-3.5 py-2.5 border-b border-line text-[10px] font-extrabold tracking-[0.1em] text-fg">
          LAYER A — FLOW PATTERNS
        </div>
        <div className="overflow-x-auto">
          <div className="min-w-[860px]">
            <div className="grid grid-cols-[170px_1fr_90px_60px_80px_200px] gap-2.5 px-3.5 py-2 text-faint tracking-[0.1em] text-[8px] border-b border-linesub">
              <span>PATTERN</span>
              <span>DESCRIPTION</span>
              <span className="text-right">HIT 30D</span>
              <span className="text-right">N</span>
              <span>STATUS</span>
              <span>CURRENT LEANS</span>
            </div>
            {(data?.patterns ?? []).map((p) => {
              const leans = Object.entries(p.current_leans ?? {}).filter(([, v]) => v);
              return (
                <div
                  key={p.id}
                  className="grid grid-cols-[170px_1fr_90px_60px_80px_200px] gap-2.5 px-3.5 py-2.5 border-b border-linesub text-[10px] items-center"
                  style={{ opacity: p.status !== 'active' ? 0.6 : 1 }}
                >
                  <span className="font-bold text-[9px] text-fg">{p.id}</span>
                  <span className="text-dim text-[9px]">{p.description}</span>
                  <span
                    className="text-right font-extrabold text-[12px]"
                    style={{
                      color:
                        (p.hit_rate_30d ?? 0) >= 0.55 ? 'var(--green)' : (p.hit_rate_30d ?? 0.5) >= 0.5 ? 'var(--fg)' : 'var(--red)',
                    }}
                  >
                    {p.hit_rate_30d == null ? '\u2014' : pct(p.hit_rate_30d)}
                  </span>
                  <span className="text-right text-dim">
                    {p.n_30d}{' '}
                    {p.n_30d < 100 && (
                      <span className="bg-amberbg text-amber px-1 py-px rounded-[2px] text-[7px] font-bold">LOW</span>
                    )}
                  </span>
                  <span>
                    <span
                      className={
                        'px-[7px] py-0.5 rounded-[3px] text-[8px] font-bold tracking-[0.08em] ' +
                        (p.status === 'active' ? 'bg-greenbg2 text-green' : 'bg-graychip text-dim')
                      }
                    >
                      {p.status.toUpperCase()}
                    </span>
                  </span>
                  <span className="flex gap-1.5 flex-wrap">
                    {leans.length === 0 ? (
                      <span className="text-[9px] text-faint">—</span>
                    ) : (
                      leans.map(([sym, dir]) => (
                        <span
                          key={sym}
                          className="text-[9px] inline-flex items-center gap-0.5"
                          style={{ color: dir === 'UP' ? 'var(--green)' : 'var(--red)' }}
                        >
                          {sym} {dir === 'UP' ? <ArrowUp size={9} /> : <ArrowDown size={9} />}
                        </span>
                      ))
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* top winning wallets — top 5 by realized profit, linked to Polymarket */}
      <div className="bg-panel border border-line rounded-md overflow-hidden">
        <div className="px-3.5 py-2.5 border-b border-line text-[10px] font-extrabold tracking-[0.1em] text-fg flex items-center gap-2">
          <Trophy size={11} className="text-amber" />
          TOP WINNING WALLETS
          <span className="text-[8px] font-normal tracking-[0.1em] text-faint">
            BY REALIZED PROFIT · CLICK FOR POLYMARKET PROFILE
          </span>
        </div>
        <div className="overflow-x-auto">
          <div className="min-w-[720px]">
            {[...(data?.wallets ?? [])]
              .sort((a, b) => (b.profit_usd ?? 0) - (a.profit_usd ?? 0))
              .slice(0, 5)
              .map((w, i) => (
                <div
                  key={w.address}
                  className="grid grid-cols-[34px_190px_1fr_90px_70px_110px] gap-2.5 px-3.5 py-[9px] border-b border-linesub text-[10px] items-center"
                >
                  <span className="font-extrabold text-[13px] text-fg">{i + 1}</span>
                  <a
                    href={`https://polymarket.com/profile/${w.address}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`open ${w.address} on polymarket.com`}
                    className="text-[9px] text-indigosoft hover:underline inline-flex items-center gap-1"
                  >
                    {w.address.slice(0, 6)}…{w.address.slice(-4)}
                    <ExternalLink size={9} className="shrink-0" />
                  </a>
                  <span className="text-dim text-[9px]">
                    {pct(w.win_rate)} win · CI {Math.round(w.ci_low * 100)}–
                    {Math.round(w.ci_high * 100)}
                  </span>
                  <span className="text-right text-green font-extrabold text-[12px]">
                    +$
                    {Math.abs(w.profit_usd ?? 0) >= 1000
                      ? ((w.profit_usd ?? 0) / 1000).toFixed(1) + 'k'
                      : (w.profit_usd ?? 0).toFixed(2)}
                  </span>
                  <span className="text-right text-dim">{w.n_resolved} res</span>
                  <span className="text-right text-dim">
                    {Math.round(w.avg_entry_seconds_after_open ?? 0)}s entry
                  </span>
                </div>
              ))}
            {(data?.wallets ?? []).length === 0 && (
              <div className="px-3.5 py-4 text-faint text-[9px]">
                no wallet records yet — the nightly Polymarket scan populates this
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Layer B — Polymarket wallets */}
      <div className="bg-panel border border-line rounded-md overflow-hidden">
        <div className="px-3.5 py-2.5 border-b border-line text-[10px] font-extrabold tracking-[0.1em] text-fg">
          LAYER B — POLYMARKET WALLETS
        </div>
        <div className="overflow-x-auto">
          <div className="min-w-[860px]">
            <div className="grid grid-cols-[150px_220px_70px_90px_110px_1fr] gap-2.5 px-3.5 py-2 text-faint tracking-[0.1em] text-[8px] border-b border-linesub">
              <span>WALLET</span>
              <span>WIN RATE · 95% CI</span>
              <span className="text-right">N RES</span>
              <span className="text-right">PROFIT</span>
              <span className="text-right">TYP ENTRY</span>
              <span>OPEN POSITIONS</span>
            </div>
            {(data?.wallets ?? []).map((w) => {
              // CI bar rendered on a 40–80% scale
              const sc = (v: number) => Math.max(0, Math.min(100, ((v - 0.4) / 0.4) * 100));
              return (
                <div
                  key={w.address}
                  className="grid grid-cols-[150px_220px_70px_90px_110px_1fr] gap-2.5 px-3.5 py-[11px] border-b border-linesub text-[10px] items-center"
                >
                  <button
                    onClick={() => copyAddress(w.address)}
                    title={`${w.address} — click to copy`}
                    className="text-[9px] text-indigosoft cursor-pointer bg-transparent border-0 p-0 text-left inline-flex items-center gap-1 overflow-hidden"
                  >
                    <span className="truncate">
                      {w.address.slice(0, 6)}…{w.address.slice(-4)}
                    </span>{' '}
                    <Copy size={9} className="shrink-0" />
                  </button>
                  <span className="flex items-center gap-2">
                    <span className="font-extrabold text-[12px] text-fg">{pct(w.win_rate)}</span>
                    <span className="flex-1 relative h-1.5 bg-track rounded-[3px]">
                      <span
                        className="absolute top-0 bottom-0 rounded-[3px]"
                        style={{
                          left: `${sc(w.ci_low)}%`,
                          width: `${sc(w.ci_high) - sc(w.ci_low)}%`,
                          background: 'var(--ci-band)',
                        }}
                      />
                      <span
                        className="absolute w-0.5 -top-0.5 h-2.5 bg-indigo"
                        style={{ left: `${sc(w.win_rate)}%` }}
                      />
                    </span>
                    <span className="text-[8px] text-faint">
                      {Math.round(w.ci_low * 100)}–{Math.round(w.ci_high * 100)}
                    </span>
                  </span>
                  <span className="text-right text-dim">{w.n_resolved}</span>
                  <span
                    className="text-right font-bold"
                    style={{ color: (w.profit_usd ?? 0) >= 0 ? 'var(--green)' : 'var(--red)' }}
                  >
                    {(w.profit_usd ?? 0) >= 0 ? '+' : '\u2212'}$
                    {Math.abs(w.profit_usd ?? 0) >= 1000
                      ? (Math.abs(w.profit_usd) / 1000).toFixed(1) + 'k'
                      : Math.abs(w.profit_usd ?? 0).toFixed(2)}
                  </span>
                  <span className="text-right text-dim">{Math.round(w.avg_entry_seconds_after_open ?? 0)}s after open</span>
                  <span className="flex gap-1.5 flex-wrap">
                    {(w.current_positions ?? []).length === 0 ? (
                      <span className="text-faint text-[9px]">— flat</span>
                    ) : (
                      (w.current_positions ?? []).map((p, i) => (
                        <span
                          key={i}
                          className="bg-panel2 border border-line rounded-[3px] px-2 py-0.5 text-[9px] inline-flex items-center gap-1"
                        >
                          <span className="font-extrabold text-fg">{p.asset}</span>
                          <span
                            className="font-bold inline-flex items-center gap-0.5"
                            style={{ color: p.side === 'UP' ? 'var(--green)' : 'var(--red)' }}
                          >
                            {p.side === 'UP' ? <ArrowUp size={9} /> : <ArrowDown size={9} />}
                            {p.side}
                          </span>
                          <span className="text-faint">
                            ${p.size_usd >= 1000 ? (p.size_usd / 1000).toFixed(1) + 'k' : p.size_usd}
                          </span>
                        </span>
                      ))
                    )}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
