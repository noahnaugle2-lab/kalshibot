import type { CSSProperties } from 'react';
import type { Asset, Confidence, Recommendation, Regime } from '../api/types';

/** Fixed display order of the nine assets. */
export const ASSETS: Asset[] = ['BTC', 'ETH', 'SOL', 'ZEC', 'HYPE', 'XRP', 'DOGE', 'BNB', 'NEAR'];

/** Stable per-asset categorical colors — never repurpose. Same in both themes. */
export const ASSET_COLOR: Record<Asset, string> = {
  BTC: '#F7931A',
  ETH: '#7C8CF8',
  SOL: '#22D3A5',
  ZEC: '#B45AF2',
  HYPE: '#4ED6C0',
  XRP: '#5EA8F5',
  DOGE: '#D9B44A',
  BNB: '#EFB90B',
  NEAR: '#F0619E',
};

/** Decimal places used when formatting spot/strike/distance per asset. */
export const ASSET_DP: Record<Asset, number> = {
  BTC: 2, ETH: 2, SOL: 2, ZEC: 2, HYPE: 3, XRP: 4, DOGE: 5, BNB: 2, NEAR: 3,
};

export function regimeChipStyle(regime: Regime): CSSProperties {
  switch (regime) {
    case 'EARLY': return { background: 'var(--reg-early-bg)', color: 'var(--reg-early-fg)' };
    case 'MID': return { background: 'var(--reg-mid-bg)', color: 'var(--reg-mid-fg)' };
    case 'LATE': return { background: 'var(--reg-late-bg)', color: 'var(--reg-late-fg)' };
    case 'SETTLEMENT': return { background: 'var(--reg-set-bg)', color: 'var(--reg-set-fg)' };
  }
}

export function regimeFg(regime: Regime): string {
  switch (regime) {
    case 'EARLY': return 'var(--reg-early-fg)';
    case 'MID': return 'var(--reg-mid-fg)';
    case 'LATE': return 'var(--reg-late-fg)';
    case 'SETTLEMENT': return 'var(--reg-set-fg)';
  }
}

export function confidencePillStyle(conf: Confidence): CSSProperties {
  switch (conf) {
    case 'high': return { background: 'var(--reg-mid-bg)', color: 'var(--reg-mid-fg)' };
    case 'medium': return { background: 'var(--reg-early-bg)', color: 'var(--reg-early-fg)' };
    case 'low': return { background: 'var(--amber-bg)', color: 'var(--amber)' };
  }
}

export function recommendationPillStyle(rec: Recommendation): CSSProperties {
  switch (rec) {
    case 'keep': return { background: 'var(--green-bg2)', color: 'var(--green)' };
    case 'retune': return { background: 'var(--amber-bg)', color: 'var(--amber)' };
    case 'bench': return { background: 'var(--gray-chip)', color: 'var(--dim)' };
  }
}
