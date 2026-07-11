import { Logo } from './Logo';
import { useMemo } from 'react';
import { NavLink } from 'react-router-dom';
import { AlertTriangle, Moon, OctagonX, Sun } from 'lucide-react';
import { IS_MOCK } from '../api/client';
import { money } from '../lib/format';
import { useApp } from '../store/store';

const TABS: Array<[string, string]> = [
  ['/', 'LIVE'],
  ['/leaderboard', 'LEADERBOARD'],
  ['/smartmoney', 'SMART MONEY'],
  ['/history', 'HISTORY'],
  ['/config', 'CONFIG'],
];

export function ModeChip({ mode }: { mode: string }) {
  const cls =
    mode === 'LIVE'
      ? 'bg-redbg border-red text-red'
      : mode === 'DEMO'
        ? 'bg-amberbg border-amber text-amber'
        : 'bg-indigobg border-indigo text-indigosoft';
  return (
    <span className={`border text-[9px] font-bold tracking-[0.12em] px-2 py-0.5 rounded-[3px] ${cls}`}>
      {mode}
    </span>
  );
}

/** Persistent 52px header + mode banner + kill / WS-disconnect strips. */
export function Header() {
  const {
    status,
    equity,
    nowSec,
    wsConnected,
    reconnectIn,
    lastDataAge,
    theme,
    toggleTheme,
    scenario,
    setScenario,
    killEngaged,
    live,
    openKillModal,
    disengageKill,
    cancelledOrders,
    devSimulateWsDrop,
  } = useApp();

  const mode = status?.mode ?? 'SHADOW';

  // Header PnL chip: 24H and SINCE START, net, derived from equity curves.
  // Benched (paused) assets are EXCLUDED from the headline — their sunk PnL
  // isn't actionable; the all-inclusive figure lives in the tooltip.
  const pausedAssets = useMemo(
    () => new Set(Object.values(live).filter((l) => l.paused).map((l) => l.asset)),
    [live],
  );
  const pnl = useMemo(() => {
    if (!equity) return null;
    let total = 0;
    let day = 0;
    let allTotal = 0;
    let started = false;
    const cutoff = nowSec - 86400;
    for (const s of equity.series) {
      const pts = s.points;
      if (pts.length < 2) continue;
      const last = pts[pts.length - 1][1];
      allTotal += last;
      if (pausedAssets.has(s.asset)) continue;
      started = true;
      total += last;
      let base = pts[0][1];
      for (const p of pts) {
        if (p[0] <= cutoff) base = p[1];
        else break;
      }
      day += last - base;
    }
    return { total, day, allTotal, started };
  }, [equity, nowSec, pausedAssets]);

  const zeroDay = !pnl || !pnl.started;

  return (
    <div className="sticky top-0 z-50 bg-headerbg border-b border-line">
      {/* ---- 52px bar ---- */}
      <div className="flex items-center flex-wrap gap-x-5 gap-y-1.5 px-5 py-2 lg:py-0 lg:h-[52px]">
        <div className="flex items-center gap-2.5">
          <Logo size={34} />
          <span className="text-[14px] font-extrabold tracking-[0.06em] text-fg">KALSHIBOT</span>
          <ModeChip mode={mode} />
        </div>

        <nav className="flex gap-0.5 flex-1 overflow-x-auto">
          {TABS.map(([to, label]) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                'text-[10px] font-bold tracking-[0.1em] px-3 py-2.5 md:py-1.5 rounded whitespace-nowrap transition-colors flex items-center ' +
                (isActive ? 'text-fg bg-[#171A2B] [.light_&]:bg-panel2' : 'text-faint hover:text-fg')
              }
            >
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="flex items-center gap-3.5 flex-wrap">
          {/* PnL chip */}
          <span
            className="hidden md:flex items-center gap-2.5 text-[10px] bg-panel2 border border-line rounded px-2.5 py-1"
            title={
              pausedAssets.size > 0 && pnl
                ? `benched assets excluded (${[...pausedAssets].join(', ')}) · all assets since start: ${money(pnl.allTotal)}`
                : undefined
            }
          >
            <span className="flex items-center gap-1.5">
              <span className="text-[8px] tracking-[0.1em] text-faint">24H</span>
              <span
                className={
                  'font-extrabold ' +
                  (zeroDay ? 'text-faint' : (pnl?.day ?? 0) >= 0 ? 'text-green' : 'text-red')
                }
              >
                {zeroDay ? '$0.00' : money(pnl!.day)}
              </span>
            </span>
            <span className="w-px h-3 bg-linestrong" />
            <span className="flex items-center gap-1.5">
              <span className="text-[8px] tracking-[0.1em] text-faint">SINCE START</span>
              <span
                className={
                  'font-extrabold ' +
                  (zeroDay ? 'text-faint' : (pnl?.total ?? 0) >= 0 ? 'text-green' : 'text-red')
                }
              >
                {zeroDay ? '$0.00' : money(pnl!.total)}
              </span>
            </span>
            <span className="text-[8px] text-ghost">
              NET{pausedAssets.size > 0 ? ' · EX-BENCHED' : ''}
            </span>
          </span>

          {/* WS indicator (dev: click to simulate a drop) */}
          <button
            onClick={import.meta.env.DEV && IS_MOCK ? devSimulateWsDrop : undefined}
            title={import.meta.env.DEV && IS_MOCK ? 'click to simulate a WS drop' : undefined}
            className={
              'flex items-center gap-1.5 text-[9px] tracking-[0.08em] bg-transparent border-0 p-0 ' +
              (wsConnected ? 'text-dim' : 'text-amber') +
              (import.meta.env.DEV && IS_MOCK ? ' cursor-pointer' : ' cursor-default')
            }
          >
            <span className={'w-[7px] h-[7px] rounded-full inline-block ' + (wsConnected ? 'bg-green' : 'bg-amber')} />
            {wsConnected ? 'WS CONNECTED' : 'WS DOWN'}
          </button>

          {/* theme toggle */}
          <button
            onClick={toggleTheme}
            className="flex items-center gap-1.5 text-[9px] font-bold tracking-[0.08em] px-[9px] py-1 border border-linestrong rounded cursor-pointer text-dim hover:text-fg bg-transparent"
          >
            {theme === 'dark' ? <Sun size={10} /> : <Moon size={10} />}
            {theme === 'dark' ? 'LIGHT' : 'DARK'}
          </button>

          {/* prototype-only scenario toggle, kept behind the dev flag */}
          {import.meta.env.DEV && IS_MOCK && (
            <div className="flex border border-linestrong rounded overflow-hidden">
              {(['day1', 'day9'] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => setScenario(s)}
                  className={
                    'text-[9px] font-bold tracking-[0.08em] px-[9px] py-1 cursor-pointer border-0 ' +
                    (scenario === s ? 'text-indigosoft bg-indigobg' : 'text-faint bg-transparent')
                  }
                >
                  {s === 'day1' ? 'DAY 1' : 'DAY 9'}
                </button>
              ))}
            </div>
          )}

          {/* kill switch — reachable from every screen */}
          {killEngaged ? (
            <button
              onClick={disengageKill}
              className="bg-redbg border border-red text-red text-[10px] font-extrabold tracking-[0.1em] px-3.5 py-1.5 rounded cursor-pointer"
            >
              DISENGAGE
            </button>
          ) : (
            <button
              onClick={openKillModal}
              className="bg-[#1A0D10] [.light_&]:bg-redbg border border-redline text-red text-[10px] font-extrabold tracking-[0.1em] px-3.5 py-1.5 rounded cursor-pointer hover:border-red hover:bg-redbg transition-colors"
            >
              KILL
            </button>
          )}
        </div>
      </div>

      {/* ---- mobile PnL strip: the "is the bot making money?" headline, shown
           below the desktop chip's md breakpoint so it exists on the phone the
           owner monitors from. The all-assets (incl. benched) figure is a
           visible column here, not a hover-only tooltip. ---- */}
      <div className="flex md:hidden items-center gap-4 px-5 py-2 border-t border-line bg-panel2 text-[11px]">
        <span className="flex items-center gap-1.5">
          <span className="text-[8px] tracking-[0.1em] text-faint">24H</span>
          <span
            className={
              'font-extrabold ' +
              (zeroDay ? 'text-faint' : (pnl?.day ?? 0) >= 0 ? 'text-green' : 'text-red')
            }
          >
            {zeroDay ? '$0.00' : money(pnl!.day)}
          </span>
        </span>
        <span className="w-px h-3 bg-linestrong" />
        <span className="flex items-center gap-1.5">
          <span className="text-[8px] tracking-[0.1em] text-faint">
            NET{pausedAssets.size > 0 ? ' EX-BENCH' : ''}
          </span>
          <span
            className={
              'font-extrabold ' +
              (zeroDay ? 'text-faint' : (pnl?.total ?? 0) >= 0 ? 'text-green' : 'text-red')
            }
          >
            {zeroDay ? '$0.00' : money(pnl!.total)}
          </span>
        </span>
        {pausedAssets.size > 0 && pnl && (
          <>
            <span className="w-px h-3 bg-linestrong" />
            <span className="flex items-center gap-1.5">
              <span className="text-[8px] tracking-[0.1em] text-faint">ALL</span>
              <span className={'font-bold ' + (pnl.allTotal >= 0 ? 'text-green' : 'text-red')}>
                {money(pnl.allTotal)}
              </span>
            </span>
          </>
        )}
      </div>

      {/* ---- mode banner (non-negotiable, always visible) ---- */}
      {mode === 'SHADOW' && (
        <div
          className="bg-indigobg text-indigosoft text-[10px] font-semibold tracking-[0.08em] px-5 py-[5px] flex items-center gap-2.5 border-t"
          style={{ borderColor: 'var(--banner-line)' }}
        >
          <span className="w-1.5 h-1.5 bg-indigo rounded-[1px] inline-block" />
          SHADOW MODE — SIMULATED FILLS · NO REAL ORDERS
        </div>
      )}
      {mode === 'DEMO' && (
        <div className="bg-amberbg text-amber text-[10px] font-semibold tracking-[0.08em] px-5 py-[5px] flex items-center gap-2.5 border-t border-amberline">
          <span className="w-1.5 h-1.5 bg-amber rounded-[1px] inline-block" />
          DEMO MODE — DEMO EXCHANGE · PLUMBING TESTS ONLY · NO PRODUCTION ORDERS
        </div>
      )}
      {mode === 'LIVE' && (
        <div className="bg-redbg text-red text-[10px] font-bold tracking-[0.08em] px-5 py-[5px] flex items-center gap-2.5 border-t-2 border-red animate-borderpulse">
          <span className="w-1.5 h-1.5 bg-red rounded-[1px] inline-block" />
          LIVE MODE — REAL MONEY · REAL ORDERS
        </div>
      )}

      {/* ---- kill engaged strip ---- */}
      {killEngaged && (
        <div className="bg-reddeep border-t border-red text-redsoft text-[10px] font-bold tracking-[0.08em] px-5 py-1.5 flex items-center gap-2">
          <OctagonX size={11} className="shrink-0" />
          KILL SWITCH ENGAGED — {cancelledOrders} RESTING ORDERS CANCELLED · ALL TRADING LOOPS HALTED
        </div>
      )}

      {/* ---- WS disconnect strip ---- */}
      {!wsConnected && (
        <div className="bg-amberbg border-t border-amber text-amber text-[10px] font-bold tracking-[0.08em] px-5 py-1.5 flex items-center gap-2 animate-pulseslow">
          <AlertTriangle size={11} className="shrink-0" />
          WEBSOCKET DISCONNECTED — RECONNECTING IN {reconnectIn}S · LAST DATA {lastDataAge}S OLD · NOTHING BELOW IS
          LIVE
        </div>
      )}
    </div>
  );
}
