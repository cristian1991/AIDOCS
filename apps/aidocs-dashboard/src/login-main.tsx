import React from "react";
import ReactDOM from "react-dom/client";
import { LoginPage } from "./LoginPage";
import { isAuthCallback, handleCallback } from "./platform/webAuth";
import "./styles.css";

// Standalone sign-in entry (separate from main.tsx so the dashboard bundle is never
// shipped to an unauthenticated visitor — the gate serves THIS page until a valid
// session cookie exists). Two jobs:
//   1. OAuth callback ("/?code=..."): finish the PKCE exchange. The /oauth/token
//      response sets the httpOnly session cookie; we then redirect to "/", which the
//      gate now serves as the real dashboard (cookie present).
//   2. Otherwise: render the LoginPage (Sign in -> beginLogin/PKCE).
async function boot() {
  const el = document.getElementById("root") as HTMLElement;
  if (isAuthCallback()) {
    try {
      if (await handleCallback()) {
        window.location.replace("/");
        return;
      }
    } catch (e) {
      // Surface nothing destructive — fall through to the LoginPage so the user retries.
      console.error("CodeNexus sign-in failed:", e);
    }
  }
  ReactDOM.createRoot(el).render(
    <React.StrictMode>
      <LoginPage />
    </React.StrictMode>,
  );
}

void boot();
