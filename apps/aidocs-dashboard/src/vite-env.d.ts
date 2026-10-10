/// <reference types="vite/client" />

// Injected by Vite `define` in BOTH builds (web build = true, desktop/Tauri build = false),
// so the bare token is always replaced — never an undeclared-global ReferenceError.
declare const __AIDOCS_WEB__: boolean;
declare const __APP_VERSION__: string;
// Commit the bundle was built from (git rev-parse --short=10 HEAD; "unknown"
// when git is unavailable). The drift guard's UI-visible half.
declare const __BUILD_SHA__: string;
