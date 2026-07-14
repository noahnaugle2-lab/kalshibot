import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, ShieldAlert } from 'lucide-react';
import { api } from '../../api/client';
import type { LiveDryRunResponse, LiveProposalStatus } from '../../api/types';
import { EmptyState } from '../../components/EmptyState';
import { FetchError } from '../../components/FetchError';
import { cents, fmtUtcTime } from '../../lib/format';
import { useApp } from '../../store/store';

const statusClass: Record<LiveProposalStatus, string> = {
  dry_run: 'bg-indigobg text-indigosoft border-indigo',
  risk_veto: 'bg-amberbg text-amber border-amber',
  blocked_reconciliation: 'bg-redbg text-red border-red',
};

export function LiveDryRunView() {
  const { epoch } = useApp();
  const [data, setData] = useState<LiveDryRunResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api.liveDryRun()
      .then((response) => {
        if (!cancelled) {
          setData(response);
          setFailed(false);
        }
      })
      .catch(() => !cancelled && setFailed(true));
    return () => { cancelled = true; };
  }, [epoch, retry]);

  const proposalTotal = useMemo(
    () => Object.values(data?.summary.proposal_counts ?? {}).reduce((sum, n) => sum + (n ?? 0), 0),
    [data],
  );
  const reconciliationOk = data?.reconciliation?.status === 'ok';

  return (
    <div className="px-5 py-[18px] max-w-[1440px] mx-auto flex flex-col gap-4">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-[13px] font-extrabold tracking-[0.06em] text-fg">PRODUCTION DRY RUN</span>
        <span className="text-[10px] text-dim">READ-ONLY REHEARSAL · NO ORDERS SUBMITTED</span>
      </div>

      {failed && <FetchError label="production dry run" onRetry={() => setRetry((n) => n + 1)} />}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <Metric label="RECONCILIATION" value={data?.reconciliation?.status?.toUpperCase() ?? 'PENDING'} good={reconciliationOk} />
        <Metric label="PROPOSALS" value={String(proposalTotal)} good />
        <Metric label="LIVE ORDERS" value={String(data?.summary.live_orders ?? 0)} good={(data?.summary.live_orders ?? 0) === 0} />
        <Metric label="OPEN LIVE POSITIONS" value={String(data?.summary.open_live_positions ?? 0)} good={(data?.summary.open_live_positions ?? 0) === 0} />
      </div>

      {data?.reconciliation && (
        <div className={`border rounded p-3 text-[10px] flex items-center gap-2 ${reconciliationOk ? 'border-green/40 bg-greenbg2 text-green' : 'border-red bg-redbg text-red'}`}>
          {reconciliationOk ? <CheckCircle2 size={14} /> : <ShieldAlert size={14} />}
          <span className="font-bold">{reconciliationOk ? 'RECONCILIATION PASSED' : 'RECONCILIATION BLOCKED'}</span>
          <span className="text-dim">last checked {fmtUtcTime(data.reconciliation.run_ts)}</span>
        </div>
      )}

      {!data ? null : data.proposals.length === 0 ? (
        <EmptyState
          title="NO DRY-RUN PROPOSALS YET"
          subtitle="the supervisor is reconciled and observing; qualifying signals appear here without submitting an exchange order"
        />
      ) : (
        <div className="border border-line rounded overflow-x-auto">
          <table className="w-full min-w-[900px] text-left text-[10px]">
            <thead className="bg-panel2 text-faint tracking-[0.08em]">
              <tr>{['TIME', 'ASSET', 'INTENT', 'LIMIT', 'REQUESTED', 'RISK SIZE', 'STATUS', 'REASON'].map((h) => <th key={h} className="px-3 py-2 font-bold">{h}</th>)}</tr>
            </thead>
            <tbody>
              {data.proposals.map((proposal) => (
                <tr key={proposal.proposal_id} className="border-t border-line hover:bg-panel2/50">
                  <td className="px-3 py-2 text-dim whitespace-nowrap">{fmtUtcTime(proposal.created_ts)}</td>
                  <td className="px-3 py-2 font-bold">{proposal.asset}</td>
                  <td className="px-3 py-2">{proposal.intent.replace('_', ' ')}</td>
                  <td className="px-3 py-2">{cents(proposal.limit_price)}</td>
                  <td className="px-3 py-2">{proposal.requested_contracts}</td>
                  <td className="px-3 py-2">{proposal.risk_contracts}</td>
                  <td className="px-3 py-2"><span className={`border rounded px-1.5 py-0.5 font-bold ${statusClass[proposal.status]}`}>{proposal.status.replace('_', ' ').toUpperCase()}</span></td>
                  <td className="px-3 py-2 text-dim max-w-[420px] truncate" title={proposal.reason ?? ''}>{proposal.reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="border border-line rounded bg-panel p-3">
      <div className="text-[9px] tracking-[0.1em] text-faint">{label}</div>
      <div className={`mt-1 text-[16px] font-extrabold ${good ? 'text-green' : 'text-amber'}`}>{value}</div>
    </div>
  );
}
