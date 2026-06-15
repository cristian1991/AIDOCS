/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Castle palette — semantic accents, not theme switches.
        // Each color carries a doctrine-anchored meaning the dashboard
        // uses consistently across pages.
        castle: {
          // Background tones (deepest at 950, lightest at 50)
          bg: "#050a08",        // outer page background
          panel: "#07110d",     // primary surface
          card: "#0c1812",      // raised card
          line: "rgba(255,255,255,0.08)", // hairline borders
          // Semantic accents
          allow: "#34d399",     // emerald — running, allow, ok
          deny: "#f87171",      // red — blocked, deny, fail
          warn: "#fbbf24",      // amber — danger zone, restart, T0
          info: "#67e8f9",      // cyan — graph, conductor, relationship
          flow: "#c084fc",      // violet — operator override, modified
          mute: "#94a3b8",      // muted text
        },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      letterSpacing: {
        // Engineer-aesthetic uppercase labels.
        widish: "0.06em",
        widest: "0.18em",
      },
      boxShadow: {
        "castle-glow": "0 0 28px rgba(52, 211, 153, 0.16)",
        "castle-warn": "0 0 28px rgba(251, 191, 36, 0.13)",
        "castle-info": "0 0 28px rgba(103, 232, 249, 0.14)",
      },
    },
  },
  plugins: [],
};
