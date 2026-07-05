import { useApp } from '../store/store';

/** Bottom-center confirmation toast; indigo for success, red for failures. */
export function Toast() {
  const { toast } = useApp();
  if (!toast) return null;
  return (
    <div
      className={
        'fixed bottom-6 left-1/2 -translate-x-1/2 z-[120] text-[10px] font-bold px-[18px] py-[9px] rounded-md tracking-[0.04em] border ' +
        (toast.error ? 'bg-redbg border-red text-redsoft' : 'bg-indigobg border-indigo text-indigosoft')
      }
    >
      {toast.msg}
    </div>
  );
}
