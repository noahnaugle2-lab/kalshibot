import { AlertTriangle } from 'lucide-react';

/**
 * Inline "failed to load" strip with a RETRY button. Views swallow fetch
 * errors into a bare/blank layout otherwise, which reads as "the bot recorded
 * nothing" — dangerous for a monitoring tool. Render this when a fetch fails.
 */
export function FetchError({ label, onRetry }: { label: string; onRetry: () => void }) {
  return (
    <div className="border border-amberline bg-amberbg rounded-md px-3.5 py-2.5 mb-3.5 flex items-center gap-2.5 text-[10px]">
      <AlertTriangle size={12} className="text-amber shrink-0" />
      <span className="text-amber font-bold tracking-[0.06em]">
        Failed to load {label} — the server or tunnel may be reconnecting.
      </span>
      <span className="flex-1" />
      <button
        onClick={onRetry}
        className="border border-amberline text-amber rounded px-2.5 py-1 text-[9px] font-bold tracking-[0.08em] hover:border-amber cursor-pointer"
      >
        RETRY
      </button>
    </div>
  );
}
