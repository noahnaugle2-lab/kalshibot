/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    fontFamily: {
      mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
    },
    extend: {
      colors: {
        // All tokens resolve through CSS variables so the light theme is a real
        // palette applied by a `.light` class on <html> (no filter hacks).
        bg: 'var(--bg)',
        panel: 'var(--panel)',
        panel2: 'var(--panel2)',
        track: 'var(--track)',
        line: 'var(--line)',
        linesub: 'var(--linesub)',
        linestrong: 'var(--linestrong)',
        linestrong2: 'var(--linestrong2)',
        fg: 'var(--fg)',
        dim: 'var(--dim)',
        faint: 'var(--faint)',
        ghost: 'var(--ghost)',
        bright: 'var(--bright)',
        indigo: 'var(--indigo)',
        indigosoft: 'var(--indigo-soft)',
        indigobg: 'var(--indigo-bg)',
        indigoline: 'var(--indigo-line)',
        green: 'var(--green)',
        greenbg: 'var(--green-bg)',
        greenbg2: 'var(--green-bg2)',
        greenline: 'var(--green-line)',
        red: 'var(--red)',
        redsoft: 'var(--red-soft)',
        redbg: 'var(--red-bg)',
        redline: 'var(--red-line)',
        reddeep: 'var(--red-deep)',
        amber: 'var(--amber)',
        amberbg: 'var(--amber-bg)',
        amberline: 'var(--amber-line)',
        headerbg: 'var(--header-bg)',
        expandbg: 'var(--expand-bg)',
        drawerbg: 'var(--drawer-bg)',
        graychip: 'var(--gray-chip)',
      },
      keyframes: {
        pulseslow: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.55' },
        },
        borderpulse: {
          '0%, 100%': { borderColor: 'var(--red)' },
          '50%': { borderColor: 'rgba(240, 82, 95, 0.25)' },
        },
      },
      animation: {
        pulseslow: 'pulseslow 2s ease-in-out infinite',
        borderpulse: 'borderpulse 1.6s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
