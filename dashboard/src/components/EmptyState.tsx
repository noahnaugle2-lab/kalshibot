/** Day-1 dashed empty-state box (leaderboard / history). */
export function EmptyState({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="border border-dashed border-linestrong2 rounded-md px-5 py-[60px] text-center">
      <div className="text-[13px] font-bold text-dim mb-2">{title}</div>
      <div className="text-[10px] text-faint">{subtitle}</div>
    </div>
  );
}
