import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// Self-heal stale chunks after a deploy: an old content-hashed chunk removed by the
// new build fires vite:preloadError on a lazy import -> full reload to fetch the fresh
// build, so a session open across a deploy never shows a blank/broken page.
// Timestamp-guarded so a genuinely-broken build can't cause a reload loop.
window.addEventListener("vite:preloadError", () => {
  const last = Number(sessionStorage.getItem("aidocs-chunk-reload-ts") || 0);
  if (Date.now() - last < 10000) return;
  sessionStorage.setItem("aidocs-chunk-reload-ts", String(Date.now()));
  window.location.reload();
});


function mount() {
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

// WEB build: finish an OAuth callback (?code=...) BEFORE mounting, so the app boots already
// connected. The webAuth module is dynamically imported so it never enters the desktop
// bundle. Desktop build mounts directly (login is the Tauri loopback flow).
if (__AIDOCS_WEB__) {
  void import("./platform/webAuth").then(async ({ isAuthCallback, handleCallback }) => {
    if (isAuthCallback()) {
      try {
        await handleCallback();
      } catch (e) {
        console.error("web auth callback failed:", e);
      }
    }
    mount();
  });
} else {
  mount();
}
