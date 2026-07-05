import { useMemo, useState } from 'react';
import type { Asset } from '../../api/types';
import { ASSETS } from '../../lib/palette';
import { MicroLabel, SegButton } from '../../components/SegButton';
import { useApp } from '../../store/store';
import { AssetCard, computeEdge } from './AssetCard';
import { DetailDrawer } from './DetailDrawer';

type LiveSort = 'fixed' | 'edge' | 'pnl';

export function LiveView() {
  const { live, assetConfig, nowSec, wsConnected, killEngaged, getSpotHistory, setAssetPaused } = useApp();
  const [sort, setSort] = useState<LiveSort>('fixed');
  const [drawer, setDrawer] = useState<Asset | null>(null);

  const ordered = useMemo(() => {
    // paused assets are hidden from the grid (still recording + manageable
    // from CONFIG) — the live view shows only what's actually trading
    const list = ASSETS.map((sym) => live[sym])
      .filter((la): la is NonNullable<typeof la> => !!la)
      .filter((la) => !la.paused);
    if (sort === 'edge') {
      return [...list].sort((a, b) => (computeEdge(b)?.cents ?? -Infinity) - (computeEdge(a)?.cents ?? -Infinity));
    }
    if (sort === 'pnl') {
      return [...list].sort((a, b) => b.session_pnl.net - a.session_pnl.net);
    }
    return list;
  }, [live, sort]);

  const hidden = useMemo(
    () => ASSETS.filter((sym) => live[sym]?.paused).map((sym) => sym),
    [live],
  );

  const drawerAsset = drawer ? live[drawer] : undefined;

  return (
    <div className="px-5 py-[18px] max-w-[1440px] mx-auto">
      <div className="flex items-baseline gap-4 mb-1.5">
        <span className="text-[13px] font-extrabold tracking-[0.06em] text-fg">LIVE MARKETS</span>
      </div>
      <div className="text-[10px] text-dim leading-[1.6] max-w-[860px] mb-3.5">
        All nine 15-minute markets in real time. Each card compares the market's implied probability (book mid)
        against the bot's model — when the net-of-fees gap clears that asset's edge threshold, the card lights up
        and the bot trades it (simulated in shadow). Snapshots refresh ~every 2s; data older than 5s is flagged
        stale. Click a card for the full feature snapshot.
      </div>

      <div className="flex items-center gap-2.5 mb-3.5">
        <MicroLabel>SORT</MicroLabel>
        <SegButton active={sort === 'fixed'} onClick={() => setSort('fixed')}>
          FIXED
        </SegButton>
        <SegButton active={sort === 'edge'} onClick={() => setSort('edge')}>
          |EDGE|
        </SegButton>
        <SegButton active={sort === 'pnl'} onClick={() => setSort('pnl')}>
          SESSION PNL
        </SegButton>
        <span className="flex-1" />
        <span className="hidden sm:inline text-[9px] text-faint tracking-[0.06em]">
          window rolls every 15:00 · snapshots ~2s
        </span>
      </div>

      {ordered.length === 0 && hidden.length === 0 ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3.5">
          {ASSETS.map((sym) => (
            <div key={sym} className="bg-panel border border-line rounded-md h-[248px] animate-pulseslow" />
          ))}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3.5">
            {ordered.map((la) => (
              <AssetCard
                key={la.asset}
                la={la}
                history={getSpotHistory(la.asset)}
                nowSec={nowSec}
                wsConnected={wsConnected}
                killEngaged={killEngaged}
                threshold={assetConfig[la.asset]?.edge_threshold_cents ?? 3}
                onOpen={() => setDrawer(la.asset)}
                onTogglePause={() => setAssetPaused(la.asset, !la.paused)}
              />
            ))}
          </div>

          {hidden.length > 0 && (
            <div className="mt-3 text-[9px] text-faint">
              {hidden.length} paused asset{hidden.length > 1 ? 's' : ''} hidden ({hidden.join(', ')})
              {' — still recording; manage in '}
              <a href="/config" className="text-indigosoft hover:underline">CONFIG</a>
            </div>
          )}
        </>
      )}

      {drawerAsset && <DetailDrawer la={drawerAsset} onClose={() => setDrawer(null)} />}
    </div>
  );
}
