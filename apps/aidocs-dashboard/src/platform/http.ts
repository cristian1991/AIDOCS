// Web-build replacement for @tauri-apps/plugin-http (wired via Vite alias). There is no
// Tauri HTTP plugin in a browser; the dashboard web bundle is served BY the gate, so all
// gate calls are same-origin and the browser's own fetch is the right transport.
export const fetch = globalThis.fetch.bind(globalThis);
