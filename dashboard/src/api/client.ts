// src/api/client.ts — the ONE module all data flows through.
// - fetch wrapper (bearer token from localStorage, 401 → login redirect)
// - WS manager (native WebSocket, auto-reconnect with countdown)
// - mock switch: VITE_API_MOCK=1 (default in dev) routes everything to mock.ts
import * as mock from './mock';
import type {
  Asset,
  AssetConfig,
  Blackout,
  EquityResponse,
  KillResponse,
  LeaderboardBasis,
  LeaderboardResponse,
  LiveAsset,
  Regime,
  SmartMoneyFilter,
  SmartMoneyResponse,
  Status,
  TradesResponse,
  WsMessage,
} from './types';

export const IS_MOCK = (import.meta.env.VITE_API_MOCK ?? (import.meta.env.DEV ? '1' : '0')) === '1';

// ---------------------------------------------------------------- auth

const TOKEN_KEY = 'kalshibot.token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

let unauthorizedHandler: (() => void) | null = null;
/** The app store registers here; ANY API 401 lands on the login screen. */
export function onUnauthorized(cb: () => void): void {
  unauthorizedHandler = cb;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

// ---------------------------------------------------------------- transport

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch('/api' + path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    unauthorizedHandler?.();
    throw new ApiError(401, 'unauthorized');
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new ApiError(res.status, body || `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

function mockCall<T>(fn: () => T): Promise<T> {
  return new Promise((resolve) => {
    setTimeout(() => resolve(structuredClone(fn())), 40 + Math.random() * 80);
  });
}

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== '');
  if (!entries.length) return '';
  return '?' + entries.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&');
}

// ---------------------------------------------------------------- API surface

export interface FullConfig {
  assets: Record<Asset, AssetConfig>;
  blackouts: Blackout[];
}

export type LeaderboardRegimeFilter = 'all' | Regime;

export const api = {
  status(): Promise<Status> {
    return IS_MOCK ? mockCall(mock.getStatus) : http<Status>('/status');
  },

  live(): Promise<LiveAsset[]> {
    return IS_MOCK ? mockCall(mock.getLive) : http<LiveAsset[]>('/live');
  },

  leaderboard(params: {
    basis: LeaderboardBasis;
    smart_money: SmartMoneyFilter;
    regime: LeaderboardRegimeFilter;
  }): Promise<LeaderboardResponse> {
    return IS_MOCK
      ? mockCall(() => mock.getLeaderboard(params))
      : http<LeaderboardResponse>(
          `/leaderboard${qs({ basis: params.basis, smart_money: params.smart_money, regime: params.regime, version: 'latest' })}`,
        );
  },

  trades(params: { asset?: Asset; strategy?: string; limit?: number; cursor?: string } = {}): Promise<TradesResponse> {
    return IS_MOCK
      ? mockCall(() => mock.getTrades(params))
      : http<TradesResponse>(`/trades${qs({ asset: params.asset, strategy: params.strategy, limit: params.limit, cursor: params.cursor })}`);
  },

  equity(params: { assets?: Asset[]; basis?: LeaderboardBasis } = {}): Promise<EquityResponse> {
    return IS_MOCK
      ? mockCall(() => mock.getEquity(params))
      : http<EquityResponse>(`/equity${qs({ assets: params.assets?.join(','), basis: params.basis })}`);
  },

  smartmoney(): Promise<SmartMoneyResponse> {
    return IS_MOCK ? mockCall(mock.getSmartMoney) : http<SmartMoneyResponse>('/smartmoney');
  },

  config(): Promise<FullConfig> {
    return IS_MOCK ? mockCall(mock.getConfig) : http<FullConfig>('/config');
  },

  saveAssetConfig(sym: Asset, cfg: AssetConfig): Promise<AssetConfig> {
    return IS_MOCK
      ? mockCall(() => mock.putAssetConfig(sym, cfg))
      : http<AssetConfig>(`/config/assets/${sym}`, { method: 'PUT', body: JSON.stringify(cfg) });
  },

  blackouts(): Promise<Blackout[]> {
    return IS_MOCK ? mockCall(mock.getBlackouts) : http<Blackout[]>('/blackouts');
  },

  saveBlackouts(list: Blackout[]): Promise<Blackout[]> {
    return IS_MOCK
      ? mockCall(() => mock.putBlackouts(list))
      : http<Blackout[]>('/blackouts', { method: 'PUT', body: JSON.stringify(list) });
  },

  /** POST /api/control/kill — engages the kill switch; posting again while
   *  engaged disengages (mock behavior; contract only defines the POST). */
  kill(): Promise<KillResponse> {
    return IS_MOCK ? mockCall(mock.postKill) : http<KillResponse>('/control/kill', { method: 'POST' });
  },

  pause(sym: Asset): Promise<{ paused: boolean }> {
    return IS_MOCK
      ? mockCall(() => mock.postPause(sym))
      : http<{ paused: boolean }>(`/control/pause/${sym}`, { method: 'POST' });
  },

  resume(sym: Asset): Promise<{ paused: boolean }> {
    return IS_MOCK
      ? mockCall(() => mock.postResume(sym))
      : http<{ paused: boolean }>(`/control/resume/${sym}`, { method: 'POST' });
  },
};

// ---------------------------------------------------------------- WebSocket

export interface SocketState {
  connected: boolean;
  /** epoch seconds of the next reconnect attempt (when disconnected) */
  retryAt: number | null;
}

export interface LiveSocketHandlers {
  onMessage(m: WsMessage): void;
  onState(s: SocketState): void;
}

const RECONNECT_DELAY_S = 5;

/** Connect to WS /ws/live (real: native WebSocket with ?token=; mock:
 *  simulated pushes on timers). Auto-reconnects every 5s until closed. */
export function connectLiveSocket(handlers: LiveSocketHandlers): { close(): void } {
  let closed = false;
  let sock: { close(): void } | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;

  const scheduleReconnect = () => {
    if (closed) return;
    handlers.onState({ connected: false, retryAt: Date.now() / 1000 + RECONNECT_DELAY_S });
    retryTimer = setTimeout(open, RECONNECT_DELAY_S * 1000);
  };

  const open = () => {
    if (closed) return;
    if (IS_MOCK) {
      sock = mock.openMockSocket({
        onOpen: () => handlers.onState({ connected: true, retryAt: null }),
        onMessage: handlers.onMessage,
        onClose: scheduleReconnect,
      });
    } else {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const token = getToken();
      const ws = new WebSocket(
        `${proto}://${location.host}/ws/live${token ? `?token=${encodeURIComponent(token)}` : ''}`,
      );
      ws.onopen = () => handlers.onState({ connected: true, retryAt: null });
      ws.onmessage = (e) => {
        try {
          handlers.onMessage(JSON.parse(e.data as string) as WsMessage);
        } catch {
          // ignore malformed frames
        }
      };
      ws.onclose = () => {
        if (!closed) scheduleReconnect();
      };
      sock = { close: () => ws.close() };
    }
  };

  open();
  return {
    close() {
      closed = true;
      clearTimeout(retryTimer);
      sock?.close();
    },
  };
}

// ---------------------------------------------------------------- dev helpers

/** Dev/mock only: simulate a WS drop to exercise the disconnect banner. */
export function simulateWsDrop(): void {
  if (IS_MOCK) mock.simulateDrop();
}

/** Dev/mock only: DAY 1 / DAY 9 scenario switch. */
export function setMockScenario(s: mock.Scenario): void {
  if (IS_MOCK) mock.setScenario(s);
}

export function getMockScenario(): mock.Scenario {
  return IS_MOCK ? mock.getScenario() : 'day9';
}

/** Mock-only sparkline seed; real mode returns {} and the UI accumulates
 *  spot history from WS snapshots. */
export function primeSpotHistory(): Partial<Record<Asset, number[]>> {
  return IS_MOCK ? mock.getSpotHistory() : {};
}
