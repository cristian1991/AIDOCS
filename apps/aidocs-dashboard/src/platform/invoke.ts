// Web-build replacement for @tauri-apps/api/core's `invoke` (wired via Vite alias, so the
// ~8 import sites compile unchanged). No Tauri runtime exists in the browser, so every
// command is routed to the gate / locked (conductor) / reported not-wired by the
// mode-aware transport.
import { routeInvoke } from "./transport";

export function invoke<T = unknown>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  return routeInvoke<T>(command, args);
}
