import { useState } from 'react';
import { ModeChip } from '../../components/Header';
import { useApp } from '../../store/store';

/** Full-screen 401 login. Any API 401 lands here (routed by the client
 *  module); signing in stores the bearer token and retries. */
export function LoginView() {
  const { signIn, status } = useApp();
  const [token, setToken] = useState('');

  const submit = () => {
    if (token.trim()) signIn(token.trim());
  };

  return (
    <div className="fixed inset-0 bg-bg z-[300] flex items-center justify-center font-mono">
      <div className="w-[360px] max-w-[92vw] flex flex-col gap-[18px]">
        <div className="flex items-center gap-2.5 justify-center">
          <span className="text-[18px] font-extrabold tracking-[0.06em] text-fg">KALSHIBOT</span>
          <ModeChip mode={status?.mode ?? 'SHADOW'} />
        </div>
        <div className="bg-panel border border-line rounded-lg p-[22px] flex flex-col gap-3.5">
          <div className="text-[10px] text-amber font-bold tracking-[0.08em]">
            SESSION EXPIRED — API RETURNED 401
          </div>
          <div className="flex flex-col gap-[5px]">
            <span className="text-[8px] tracking-[0.12em] text-faint">DASHBOARD TOKEN</span>
            <input
              type="password"
              autoFocus
              placeholder="paste bearer token"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && submit()}
              className="bg-panel2 border border-linestrong text-fg text-[11px] px-[11px] py-[9px] rounded w-full box-border outline-none focus:border-indigo"
            />
          </div>
          <button
            onClick={submit}
            className="text-center bg-indigo text-[#0B0D10] text-[10px] font-extrabold tracking-[0.1em] py-2.5 rounded cursor-pointer hover:opacity-90 border-0"
          >
            SIGN IN
          </button>
        </div>
        <div className="text-[9px] text-ghost text-center leading-[1.6]">
          single-user dashboard · sits behind Cloudflare Access + app auth in production. Every API call routes
          through one client; any 401 lands here.
        </div>
      </div>
    </div>
  );
}
