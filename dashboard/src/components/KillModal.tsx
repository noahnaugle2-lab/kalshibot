import { useEffect, useState } from 'react';
import { useApp } from '../store/store';

const MODE_NOTE: Record<string, string> = {
  SHADOW: 'simulated orders only',
  DEMO: 'demo exchange orders only',
  LIVE: 'REAL-MONEY orders will be cancelled',
};

/** Kill-switch confirmation modal. In LIVE mode, confirming requires typing
 *  "KILL" (non-negotiable per the brief; not present in the prototype). */
export function KillModal() {
  const { killModalOpen, closeKillModal, engageKill, status, restingOrders } = useApp();
  const [confirmText, setConfirmText] = useState('');
  const mode = status?.mode ?? 'SHADOW';
  const needsTyping = mode === 'LIVE';
  const canConfirm = !needsTyping || confirmText.trim().toUpperCase() === 'KILL';

  useEffect(() => {
    if (killModalOpen) setConfirmText('');
  }, [killModalOpen]);

  if (!killModalOpen) return null;

  return (
    <>
      <div className="fixed inset-0 z-[100]" style={{ background: 'var(--scrim)' }} onClick={closeKillModal} />
      <div className="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[420px] max-w-[92vw] bg-panel border border-redline rounded-lg z-[101] p-[22px]">
        <div className="text-[13px] font-extrabold text-red tracking-[0.08em] mb-2.5">ENGAGE KILL SWITCH</div>
        <div className="text-[10px] text-dim leading-[1.7] mb-4">
          Cancels all resting orders and halts every trading loop across all nine assets. Mode is{' '}
          <span
            className={
              'font-bold ' + (mode === 'LIVE' ? 'text-red' : mode === 'DEMO' ? 'text-amber' : 'text-indigosoft')
            }
          >
            {mode}
          </span>{' '}
          — {MODE_NOTE[mode]}.{' '}
          {needsTyping
            ? 'Type KILL below to confirm.'
            : 'In LIVE mode this action requires typing KILL to confirm.'}
        </div>
        {needsTyping && (
          <div className="mb-4 flex flex-col gap-1.5">
            <span className="text-[8px] tracking-[0.12em] text-faint">TYPE "KILL" TO CONFIRM</span>
            <input
              autoFocus
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder="KILL"
              className="bg-panel2 border border-linestrong text-fg text-[11px] px-2.5 py-2 rounded outline-none focus:border-red w-full box-border"
            />
          </div>
        )}
        <div className="flex gap-2.5 justify-end">
          <button
            onClick={closeKillModal}
            className="text-[10px] font-bold px-4 py-2 rounded cursor-pointer border border-linestrong text-dim bg-transparent hover:text-fg"
          >
            CANCEL
          </button>
          <button
            onClick={() => canConfirm && engageKill()}
            disabled={!canConfirm}
            className={
              'text-[10px] font-extrabold tracking-[0.08em] px-4 py-2 rounded border border-transparent ' +
              (canConfirm ? 'bg-red text-[#0B0D10] cursor-pointer hover:opacity-90' : 'bg-redbg text-redline cursor-not-allowed')
            }
          >
            ENGAGE — CANCEL {restingOrders} ORDERS
          </button>
        </div>
      </div>
    </>
  );
}
