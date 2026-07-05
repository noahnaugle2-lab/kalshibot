/** KalshiBot mark: a friendly EVE-style robot — white capsule body, dark
 *  visor, two cyan eyes. Inline SVG so it stays crisp at any size and needs
 *  no asset pipeline. */
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 88"
      fill="none"
      aria-hidden="true"
      className="block shrink-0"
    >
      <defs>
        <linearGradient id="kb-body" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="100%" stopColor="#c9d2de" />
        </linearGradient>
        <linearGradient id="kb-head" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="100%" stopColor="#d8dfe9" />
        </linearGradient>
      </defs>

      {/* arms */}
      <ellipse cx="9" cy="52" rx="6" ry="16" fill="url(#kb-body)" />
      <ellipse cx="55" cy="52" rx="6" ry="16" fill="url(#kb-body)" />

      {/* body: egg capsule */}
      <path
        d="M32 30 C 45 30 50 44 50 60 C 50 76 42 84 32 84 C 22 84 14 76 14 60 C 14 44 19 30 32 30 Z"
        fill="url(#kb-body)"
      />

      {/* head */}
      <ellipse cx="32" cy="16" rx="16" ry="13" fill="url(#kb-head)" />

      {/* visor */}
      <ellipse cx="32" cy="16.5" rx="12" ry="9" fill="#101318" />

      {/* eyes: happy arcs */}
      <path
        d="M23 17.5 q 3.4 -4.4 6.8 0"
        stroke="#35c6e8"
        strokeWidth="2.6"
        strokeLinecap="round"
        fill="none"
      />
      <path
        d="M34.2 17.5 q 3.4 -4.4 6.8 0"
        stroke="#35c6e8"
        strokeWidth="2.6"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}
