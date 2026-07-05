import type { ReactNode } from 'react';

/** Segmented-control button used across all view control rows. */
export function SegButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={
        'text-[9px] font-bold tracking-[0.08em] px-2.5 py-1 rounded border cursor-pointer transition-colors ' +
        (active
          ? 'border-indigo text-indigosoft bg-indigobg'
          : 'border-linestrong text-faint bg-transparent hover:text-fg hover:border-indigo')
      }
    >
      {children}
    </button>
  );
}

/** 8px uppercase micro-label used before control groups. */
export function MicroLabel({ children }: { children: ReactNode }) {
  return <span className="text-[9px] tracking-[0.1em] text-faint">{children}</span>;
}
