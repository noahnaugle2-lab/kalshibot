import { useEffect, useState } from 'react';
import { ModeChip } from '../../components/Header';
import { useApp } from '../../store/store';
import { authStatus, passkeySupported } from '../../api/client';

/** Full-screen 401 login. Passkey (Face ID / Touch ID) first; the bearer
 *  token is the fallback and the one-time source of trust for enrolling a
 *  passkey on a new device. */
export function LoginView() {
  const { signIn, signInPasskey, registerPasskey, status, showToast } = useApp();
  const [token, setToken] = useState('');
  const [registered, setRegistered] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const supported = passkeySupported();

  useEffect(() => {
    authStatus().then((s) => setRegistered(s.registered));
  }, []);

  const doPasskeyLogin = async () => {
    setBusy(true);
    try {
      await signInPasskey();
    } catch (e) {
      showToast(`passkey sign-in failed — ${(e as Error).message}`, true);
    } finally {
      setBusy(false);
    }
  };

  const doTokenSignIn = () => {
    if (token.trim()) signIn(token.trim());
  };

  const doEnroll = async () => {
    if (!token.trim()) {
      showToast('enter your dashboard token to enroll a passkey', true);
      return;
    }
    setBusy(true);
    try {
      await registerPasskey(token.trim());
    } catch (e) {
      showToast(`enrollment failed — ${(e as Error).message}`, true);
    } finally {
      setBusy(false);
    }
  };

  const tokenVisible = showToken || registered === false || !supported;

  return (
    <div className="fixed inset-0 bg-bg z-[300] flex items-center justify-center font-mono">
      <div className="w-[360px] max-w-[92vw] flex flex-col gap-[18px]">
        <div className="flex items-center gap-2.5 justify-center">
          <span className="text-[18px] font-extrabold tracking-[0.06em] text-fg">KALSHIBOT</span>
          <ModeChip mode={status?.mode ?? 'SHADOW'} />
        </div>
        <div className="bg-panel border border-line rounded-lg p-[22px] flex flex-col gap-3.5">
          {/* Primary: passkey login when a device is already enrolled */}
          {supported && registered && (
            <button
              onClick={doPasskeyLogin}
              disabled={busy}
              className="text-center bg-indigo text-[#0B0D10] text-[10px] font-extrabold tracking-[0.1em] py-3 rounded cursor-pointer hover:opacity-90 border-0 disabled:opacity-50"
            >
              {busy ? 'WAITING FOR AUTHENTICATOR…' : 'SIGN IN WITH FACE ID / TOUCH ID'}
            </button>
          )}

          {/* Enroll prompt when no passkey exists yet */}
          {supported && registered === false && (
            <div className="text-[9px] text-faint leading-[1.6]">
              No passkey on this account yet. Enter your dashboard token once to enroll Face ID /
              Touch ID on this device.
            </div>
          )}

          {/* Token field: fallback sign-in + enrollment source */}
          {tokenVisible && (
            <div className="flex flex-col gap-[5px]">
              <span className="text-[8px] tracking-[0.12em] text-faint">DASHBOARD TOKEN</span>
              <input
                type="password"
                autoFocus={registered === false}
                placeholder="paste bearer token"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && doTokenSignIn()}
                className="bg-panel2 border border-linestrong text-fg text-[11px] px-[11px] py-[9px] rounded w-full box-border outline-none focus:border-indigo"
              />
              <div className="flex gap-2 mt-1">
                <button
                  onClick={doTokenSignIn}
                  className="flex-1 text-center bg-panel2 border border-linestrong text-fg text-[10px] font-bold tracking-[0.1em] py-2.5 rounded cursor-pointer hover:border-indigo"
                >
                  SIGN IN
                </button>
                {supported && (
                  <button
                    onClick={doEnroll}
                    disabled={busy}
                    className="flex-1 text-center bg-indigo text-[#0B0D10] text-[10px] font-extrabold tracking-[0.08em] py-2.5 rounded cursor-pointer hover:opacity-90 border-0 disabled:opacity-50"
                  >
                    {busy ? '…' : 'ENROLL FACE ID'}
                  </button>
                )}
              </div>
            </div>
          )}

          {/* Reveal the token fallback when passkey is the primary path */}
          {supported && registered && !showToken && (
            <button
              onClick={() => setShowToken(true)}
              className="text-[9px] text-ghost text-center cursor-pointer bg-transparent border-0 hover:text-faint"
            >
              use dashboard token instead
            </button>
          )}
        </div>
        <div className="text-[9px] text-ghost text-center leading-[1.6]">
          single-user dashboard · passkey (Face ID / Touch ID) or bearer token · every API call
          routes through one client; any 401 lands here.
        </div>
      </div>
    </div>
  );
}
