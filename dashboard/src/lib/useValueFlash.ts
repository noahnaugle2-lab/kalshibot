import { useEffect, useRef, useState } from 'react';

/** Flashes +1 (green) / -1 (red) for ~300ms when the watched value changes.
 *  Disabled (returns 0) for stale assets per the handoff. */
export function useValueFlash(value: number | null, enabled: boolean): -1 | 0 | 1 {
  const prev = useRef<number | null>(null);
  const [dir, setDir] = useState<-1 | 0 | 1>(0);

  useEffect(() => {
    const p = prev.current;
    prev.current = value;
    if (!enabled || value === null || p === null || value === p) return;
    setDir(value > p ? 1 : -1);
    const t = setTimeout(() => setDir(0), 300);
    return () => clearTimeout(t);
  }, [value, enabled]);

  return dir;
}
