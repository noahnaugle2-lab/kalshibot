import { useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { Asset, AssetConfig, Blackout } from '../../api/types';
import { fmtUtcDate, fmtUtcHm } from '../../lib/format';
import { ASSETS, ASSET_COLOR } from '../../lib/palette';
import { useApp } from '../../store/store';

const STRATEGIES = ['latency_momentum_v1', 'mean_revert_v2', 'vol_breakout_v1', 'baseline_hold'];

const inputCls =
  'bg-panel2 border border-linestrong text-fg text-[11px] px-2 py-[5px] rounded w-full box-border outline-none focus:border-indigo';

function fmtBlackoutWindow(b: Blackout): string {
  return `${fmtUtcDate(b.start)} · ${fmtUtcHm(b.start)}–${fmtUtcHm(b.end)} UTC`;
}

export function ConfigView() {
  const { epoch, showToast, setAssetPaused } = useApp();
  const [saved, setSaved] = useState<Partial<Record<Asset, AssetConfig>>>({});
  const [drafts, setDrafts] = useState<Partial<Record<Asset, AssetConfig>>>({});
  const [blackouts, setBlackouts] = useState<Blackout[]>([]);

  useEffect(() => {
    let cancelled = false;
    api
      .config()
      .then((r) => {
        if (cancelled) return;
        setSaved(r.assets);
        setDrafts(structuredClone(r.assets));
        setBlackouts(r.blackouts);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [epoch]);

  const updateDraft = (sym: Asset, patch: Partial<AssetConfig>) => {
    setDrafts((prev) => {
      const cur = prev[sym];
      return cur ? { ...prev, [sym]: { ...cur, ...patch } } : prev;
    });
  };

  // Optimistic save with rollback on server error.
  const save = async (sym: Asset) => {
    const draft = drafts[sym];
    const prevSaved = saved[sym];
    if (!draft) return;
    setSaved((p) => ({ ...p, [sym]: draft }));
    try {
      const result = await api.saveAssetConfig(sym, draft);
      setSaved((p) => ({ ...p, [sym]: result }));
      showToast(`PUT /api/config/assets/${sym} → saved`);
    } catch (e) {
      setSaved((p) => (prevSaved ? { ...p, [sym]: prevSaved } : p));
      setDrafts((p) => (prevSaved ? { ...p, [sym]: structuredClone(prevSaved) } : p));
      showToast(`PUT /api/config/assets/${sym} failed — ${(e as Error).message}`, true);
    }
  };

  const togglePause = async (sym: Asset) => {
    const cur = drafts[sym];
    if (!cur) return;
    const next = !cur.paused;
    updateDraft(sym, { paused: next });
    setSaved((p) => {
      const s = p[sym];
      return s ? { ...p, [sym]: { ...s, paused: next } } : p;
    });
    await setAssetPaused(sym, next);
  };

  return (
    <div className="px-5 py-[18px] max-w-[1440px] mx-auto flex flex-col gap-4">
      <div className="flex items-center gap-4">
        <span className="text-[13px] font-extrabold tracking-[0.06em] text-fg">CONFIG</span>
      </div>
      <div className="text-[10px] text-dim leading-[1.6] max-w-[860px] -mt-1.5 mb-0.5">
        Per-asset strategy and risk controls — each card saves independently to the server with optimistic UI and
        rollback on error. Event blackouts halt trading around scheduled news for the assets listed. Trading mode
        (SHADOW / DEMO / LIVE) is read-only here; it changes only via server config.
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3.5">
        {ASSETS.map((sym) => {
          const c = drafts[sym];
          if (!c) {
            return <div key={sym} className="bg-panel border border-line rounded-md h-[240px] animate-pulseslow" />;
          }
          const dirty = JSON.stringify(c) !== JSON.stringify(saved[sym]);
          return (
            <div key={sym} className="bg-panel border border-line rounded-md p-3.5 flex flex-col gap-[11px]">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-[2px]" style={{ background: ASSET_COLOR[sym] }} />
                <span className="text-[14px] font-extrabold text-fg">{sym}</span>
                <span className="flex-1" />
                <button
                  onClick={() => togglePause(sym)}
                  className={
                    'text-[8px] font-bold tracking-[0.08em] px-[9px] py-[3px] rounded-[3px] cursor-pointer border ' +
                    (c.paused
                      ? 'border-amberline text-amber bg-amberbg'
                      : 'border-greenline text-green bg-greenbg2')
                  }
                >
                  {c.paused ? 'PAUSED' : 'RUNNING'}
                </button>
              </div>

              <div className="flex flex-col gap-1">
                <span className="text-[8px] tracking-[0.12em] text-faint">STRATEGY</span>
                <select
                  value={c.strategy ?? ''}
                  onChange={(e) => updateDraft(sym, { strategy: e.target.value })}
                  className="bg-panel2 border border-linestrong text-bright text-[10px] px-2 py-1.5 rounded w-full outline-none cursor-pointer focus:border-indigo"
                >
                  {STRATEGIES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </div>

              <div className="grid grid-cols-2 gap-2.5">
                <div className="flex flex-col gap-1">
                  <span className="text-[8px] tracking-[0.12em] text-faint">EDGE THRESHOLD ¢</span>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    value={c.edge_threshold_cents}
                    onChange={(e) => updateDraft(sym, { edge_threshold_cents: Number(e.target.value) })}
                    className={inputCls}
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <span className="text-[8px] tracking-[0.12em] text-faint">MAX POSITION</span>
                  <input
                    type="number"
                    min={1}
                    max={1000}
                    value={c.max_position_contracts}
                    onChange={(e) => updateDraft(sym, { max_position_contracts: Number(e.target.value) })}
                    className={inputCls}
                  />
                </div>
              </div>

              <div className="flex flex-col gap-1">
                <span className="flex justify-between text-[8px] tracking-[0.12em] text-faint">
                  SMART $ WEIGHT <span className="text-indigosoft">{c.smart_money_weight.toFixed(2)}</span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={c.smart_money_weight}
                  onChange={(e) => updateDraft(sym, { smart_money_weight: Number(e.target.value) })}
                  className="w-full"
                />
              </div>

              <button
                onClick={() => save(sym)}
                className={
                  'text-center bg-indigobg border text-indigosoft text-[9px] font-extrabold tracking-[0.1em] py-[7px] rounded cursor-pointer transition-colors ' +
                  (dirty ? 'border-indigo' : 'border-indigoline hover:border-indigo')
                }
              >
                SAVE {sym}
              </button>
            </div>
          );
        })}
      </div>

      {/* event blackouts */}
      <div className="bg-panel border border-line rounded-md overflow-hidden">
        <div className="px-3.5 py-2.5 border-b border-line flex items-center">
          <span className="text-[10px] font-extrabold tracking-[0.1em] text-fg">EVENT BLACKOUTS</span>
          <span className="flex-1" />
          <button className="text-[9px] text-indigo font-bold cursor-pointer bg-transparent border-0 p-0">
            + ADD ENTRY
          </button>
        </div>
        {blackouts.map((b, i) => (
          <div
            key={i}
            className="grid grid-cols-[220px_200px_1fr_60px] gap-2.5 px-3.5 py-2.5 border-b border-linesub text-[10px] items-center"
          >
            <span className="font-bold text-fg">{b.label}</span>
            <span className="text-dim">{fmtBlackoutWindow(b)}</span>
            <span className="flex gap-[5px] flex-wrap">
              {(b.affected_assets === 'ALL' ? ['ALL'] : b.affected_assets).map((a) => (
                <span
                  key={a}
                  className="bg-panel2 border border-line rounded-[3px] px-[7px] py-px text-[8px] font-bold text-fg"
                >
                  {a}
                </span>
              ))}
            </span>
            <button className="text-right text-faint cursor-pointer text-[9px] bg-transparent border-0 p-0 hover:text-fg">
              EDIT
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
