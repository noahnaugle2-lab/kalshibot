import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import {
  api,
  authStatus,
  connectLiveSocket,
  getMockScenario,
  getToken,
  IS_MOCK,
  onUnauthorized,
  passkeyLogin,
  passkeyRegister,
  primeSpotHistory,
  setMockScenario,
  setToken,
  simulateWsDrop,
} from '../api/client';
import type { Asset, AssetConfig, EquityResponse, LiveAsset, Status, Trade } from '../api/types';

export type Scenario = 'day1' | 'day9';
export type Theme = 'dark' | 'light';

export interface ToastState {
  msg: string;
  error?: boolean;
}

export interface AppStore {
  // auth
  authed: boolean;
  signIn(token: string): void;
  signInPasskey(): Promise<void>;
  registerPasskey(token: string): Promise<void>;
  // theme
  theme: Theme;
  toggleTheme(): void;
  // dev scenario toggle (mock only)
  scenario: Scenario;
  setScenario(s: Scenario): void;
  /** bumps whenever global data must be refetched (scenario change, sign-in) */
  epoch: number;
  // data
  status: Status | null;
  live: Partial<Record<Asset, LiveAsset>>;
  assetConfig: Partial<Record<Asset, AssetConfig>>;
  equity: EquityResponse | null;
  recentTrades: Trade[];
  getSpotHistory(sym: Asset): number[];
  // clock / freshness
  nowSec: number;
  wsConnected: boolean;
  reconnectIn: number;
  lastDataAge: number;
  // kill switch
  killEngaged: boolean;
  killModalOpen: boolean;
  openKillModal(): void;
  closeKillModal(): void;
  engageKill(): Promise<void>;
  disengageKill(): Promise<void>;
  cancelledOrders: number;
  restingOrders: number;
  // pause control
  setAssetPaused(sym: Asset, paused: boolean): Promise<void>;
  // toast
  toast: ToastState | null;
  showToast(msg: string, error?: boolean): void;
  // dev
  devSimulateWsDrop(): void;
}

const Ctx = createContext<AppStore | null>(null);

const THEME_KEY = 'kalshibot.theme';

export function AppProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState<boolean>(() => (IS_MOCK ? true : !!getToken()));
  const [theme, setTheme] = useState<Theme>(() =>
    localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark',
  );
  const [scenario, setScenarioState] = useState<Scenario>(() => getMockScenario());
  const [epoch, setEpoch] = useState(0);

  const [status, setStatus] = useState<Status | null>(null);
  const [live, setLive] = useState<Partial<Record<Asset, LiveAsset>>>({});
  const [assetConfig, setAssetConfig] = useState<Partial<Record<Asset, AssetConfig>>>({});
  const [equity, setEquity] = useState<EquityResponse | null>(null);
  const [recentTrades, setRecentTrades] = useState<Trade[]>([]);

  const [nowSec, setNowSec] = useState(() => Date.now() / 1000);
  const [wsConnected, setWsConnected] = useState(false);
  const [retryAt, setRetryAt] = useState<number | null>(null);
  const [lastMsgAt, setLastMsgAt] = useState<number>(() => Date.now() / 1000);

  const [killModalOpen, setKillModalOpen] = useState(false);
  const [cancelledOrders, setCancelledOrders] = useState(0);
  const [toast, setToast] = useState<ToastState | null>(null);

  const historyRef = useRef<Partial<Record<Asset, number[]>>>({});
  const toastTimer = useRef<ReturnType<typeof setTimeout>>();
  const hadFirstConnect = useRef(false);

  // ---- theme class toggle (real light palette, not a filter hack) ----
  useEffect(() => {
    document.documentElement.classList.toggle('light', theme === 'light');
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  // ---- 1s local clock: countdowns, staleness ages, reconnect countdown ----
  useEffect(() => {
    const iv = setInterval(() => setNowSec(Date.now() / 1000), 1000);
    return () => clearInterval(iv);
  }, []);

  // ---- 401 handling: one client module owns the redirect ----
  useEffect(() => {
    onUnauthorized(() => setAuthed(false));
  }, []);

  // ---- cookie session check: a passkey login carries no localStorage token,
  //      so confirm any existing session cookie with the server on mount ----
  useEffect(() => {
    if (IS_MOCK || authed) return;
    let cancelled = false;
    authStatus().then((s) => {
      if (!cancelled && s.authed) {
        setAuthed(true);
        setEpoch((e) => e + 1);
      }
    });
    return () => {
      cancelled = true;
    };
    // run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showToast = useCallback((msg: string, error = false) => {
    setToast({ msg, error });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  }, []);

  // ---- initial + epoch data load ----
  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    (async () => {
      try {
        const [st, lv, eq, cfg] = await Promise.all([api.status(), api.live(), api.equity(), api.config()]);
        if (cancelled) return;
        setStatus(st);
        setLive(Object.fromEntries(lv.map((a) => [a.asset, a])));
        setEquity(eq);
        setAssetConfig(cfg.assets);
        setRecentTrades([]);
        historyRef.current = primeSpotHistory();
      } catch {
        // 401 already routed to login by the client; other errors surface on retry
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed, epoch]);

  // ---- WS lifecycle ----
  useEffect(() => {
    if (!authed) return;
    const handle = connectLiveSocket({
      onMessage: (m) => {
        setLastMsgAt(Date.now() / 1000);
        switch (m.type) {
          case 'snapshot': {
            setLive((prev) => ({ ...prev, [m.asset]: m.data }));
            const spot = m.data.snapshot?.spot ?? null;
            if (spot !== null) {
              const arr = historyRef.current[m.asset] ?? (historyRef.current[m.asset] = []);
              if (arr[arr.length - 1] !== spot) {
                arr.push(spot);
                if (arr.length > 240) arr.shift();
              }
            }
            break;
          }
          case 'status':
            setStatus(m.data);
            break;
          case 'trade':
            setRecentTrades((prev) => [m.data, ...prev].slice(0, 50));
            break;
          case 'settlement':
            api.equity().then(setEquity).catch(() => {});
            break;
        }
      },
      onState: (s) => {
        setWsConnected(s.connected);
        setRetryAt(s.retryAt);
        if (s.connected) {
          if (hadFirstConnect.current) {
            // resync after a drop
            api
              .live()
              .then((lv) => setLive(Object.fromEntries(lv.map((a) => [a.asset, a]))))
              .catch(() => {});
            showToast('WS reconnected — resyncing snapshots');
          }
          hadFirstConnect.current = true;
        }
      },
    });
    return () => handle.close();
  }, [authed, showToast]);

  // ---- actions ----
  const signIn = useCallback(
    (token: string) => {
      setToken(token);
      setAuthed(true);
      setEpoch((e) => e + 1);
      showToast('session established — bearer token stored');
    },
    [showToast],
  );

  const signInPasskey = useCallback(async () => {
    await passkeyLogin();
    setAuthed(true);
    setEpoch((e) => e + 1);
    showToast('signed in with passkey');
  }, [showToast]);

  const registerPasskey = useCallback(
    async (token: string) => {
      await passkeyRegister(token);
      setAuthed(true);
      setEpoch((e) => e + 1);
      showToast('passkey enrolled — Face ID / Touch ID ready');
    },
    [showToast],
  );

  const toggleTheme = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), []);

  const setScenario = useCallback((s: Scenario) => {
    setMockScenario(s);
    setScenarioState(s);
    setEpoch((e) => e + 1);
  }, []);

  const engageKill = useCallback(async () => {
    try {
      const r = await api.kill();
      setCancelledOrders(r.cancelled_orders);
      setStatus((s) => (s ? { ...s, kill_switch_engaged: r.engaged } : s));
      setKillModalOpen(false);
      showToast(`POST /api/control/kill → engaged, ${r.cancelled_orders} orders cancelled`);
    } catch (e) {
      showToast(`POST /api/control/kill failed — ${(e as Error).message}`, true);
    }
  }, [showToast]);

  const disengageKill = useCallback(async () => {
    try {
      const r = await api.kill();
      setStatus((s) => (s ? { ...s, kill_switch_engaged: r.engaged } : s));
      showToast('kill switch disengaged — loops resuming');
    } catch (e) {
      showToast(`POST /api/control/kill failed — ${(e as Error).message}`, true);
    }
  }, [showToast]);

  const setAssetPaused = useCallback(
    async (sym: Asset, paused: boolean) => {
      const revertLive = live[sym]?.paused ?? false;
      const revertCfg = assetConfig[sym];
      // optimistic
      setLive((prev) => {
        const cur = prev[sym];
        return cur ? { ...prev, [sym]: { ...cur, paused } } : prev;
      });
      setAssetConfig((prev) => {
        const cur = prev[sym];
        return cur ? { ...prev, [sym]: { ...cur, paused } } : prev;
      });
      try {
        await (paused ? api.pause(sym) : api.resume(sym));
        showToast(`POST /api/control/${paused ? 'pause' : 'resume'}/${sym} → ok`);
      } catch (e) {
        // rollback
        setLive((prev) => {
          const cur = prev[sym];
          return cur ? { ...prev, [sym]: { ...cur, paused: revertLive } } : prev;
        });
        setAssetConfig((prev) => (revertCfg ? { ...prev, [sym]: revertCfg } : prev));
        showToast(`POST /api/control/${paused ? 'pause' : 'resume'}/${sym} failed — ${(e as Error).message}`, true);
      }
    },
    [live, assetConfig, showToast],
  );

  const getSpotHistory = useCallback((sym: Asset) => historyRef.current[sym] ?? [], []);

  const devSimulateWsDrop = useCallback(() => {
    if (import.meta.env.DEV) simulateWsDrop();
  }, []);

  const restingOrders = useMemo(
    () => Object.values(live).filter((a) => a && a.position !== null).length,
    [live],
  );

  const reconnectIn = retryAt !== null ? Math.max(0, Math.ceil(retryAt - nowSec)) : 0;
  const lastDataAge = Math.max(0, Math.floor(nowSec - lastMsgAt));

  const store: AppStore = {
    authed,
    signIn,
    signInPasskey,
    registerPasskey,
    theme,
    toggleTheme,
    scenario,
    setScenario,
    epoch,
    status,
    live,
    assetConfig,
    equity,
    recentTrades,
    getSpotHistory,
    nowSec,
    wsConnected,
    reconnectIn,
    lastDataAge,
    killEngaged: status?.kill_switch_engaged ?? false,
    killModalOpen,
    openKillModal: () => setKillModalOpen(true),
    closeKillModal: () => setKillModalOpen(false),
    engageKill,
    disengageKill,
    cancelledOrders,
    restingOrders,
    setAssetPaused,
    toast,
    showToast,
    devSimulateWsDrop,
  };

  return <Ctx.Provider value={store}>{children}</Ctx.Provider>;
}

export function useApp(): AppStore {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useApp must be used within AppProvider');
  return ctx;
}
