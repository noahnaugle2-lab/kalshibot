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

// ---- WebAuthn passkeys (Face ID / Touch ID) ----
// Login issues an httpOnly session cookie the API/WS accept; no token stored.

function b64urlToBuf(s: string): ArrayBuffer {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const bin = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
function bufToB64url(b: ArrayBuffer): string {
  let bin = '';
  for (const x of new Uint8Array(b)) bin += String.fromCharCode(x);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function passkeySupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential && !!navigator.credentials;
}

export async function authStatus(): Promise<{ registered: boolean; authed: boolean }> {
  try {
    const res = await fetch('/auth/status', { credentials: 'same-origin' });
    if (!res.ok) return { registered: false, authed: false };
    return await res.json();
  } catch {
    return { registered: false, authed: false };
  }
}

/** Sign in with an existing passkey (prompts Face ID / Touch ID). */
export async function passkeyLogin(): Promise<void> {
  const optRes = await fetch('/auth/login/options', { method: 'POST', credentials: 'same-origin' });
  if (!optRes.ok) throw new ApiError(optRes.status, (await optRes.text()) || 'no passkey registered');
  const o = await optRes.json();
  const publicKey: any = {
    ...o,
    challenge: b64urlToBuf(o.challenge),
    allowCredentials: (o.allowCredentials ?? []).map((c: any) => ({ ...c, id: b64urlToBuf(c.id) })),
  };
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new ApiError(0, 'passkey cancelled');
  const r = cred.response as AuthenticatorAssertionResponse;
  const body = {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      authenticatorData: bufToB64url(r.authenticatorData),
      signature: bufToB64url(r.signature),
      userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
  const res = await fetch('/auth/login/verify', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, (await res.text()) || 'passkey login failed');
}

/** Enroll this device's passkey. Authorized by the bearer token (bootstrap). */
export async function passkeyRegister(token: string): Promise<void> {
  const auth = { Authorization: `Bearer ${token}` };
  const optRes = await fetch('/auth/register/options', {
    method: 'POST',
    credentials: 'same-origin',
    headers: auth,
  });
  if (!optRes.ok) throw new ApiError(optRes.status, (await optRes.text()) || 'token rejected');
  const o = await optRes.json();
  const publicKey: any = {
    ...o,
    challenge: b64urlToBuf(o.challenge),
    user: { ...o.user, id: b64urlToBuf(o.user.id) },
    excludeCredentials: (o.excludeCredentials ?? []).map((c: any) => ({ ...c, id: b64urlToBuf(c.id) })),
  };
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new ApiError(0, 'enrollment cancelled');
  const r = cred.response as AuthenticatorAttestationResponse;
  const body = {
    id: cred.id,
    rawId: bufToB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON),
      attestationObject: bufToB64url(r.attestationObject),
      transports: typeof r.getTransports === 'function' ? r.getTransports() : [],
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
  const res = await fetch('/auth/register/verify', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...auth },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, (await res.text()) || 'enrollment failed');
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
