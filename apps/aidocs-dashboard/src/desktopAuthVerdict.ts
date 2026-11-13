/** Desktop auth verdict — "could not ask" is never "answered no" (#509).
 *
 * A CLIENT MUST NEVER INFER INVALIDATION FROM UNREACHABLE EVIDENCE.
 *
 * The operator's ruling: after login, a token is valid for AUTOLOGIN for 30
 * DAYS unless INVALIDATED, and the only invalidation events are (a) the user is
 * removed from the project, (b) the user is banned by the platform, (c) the
 * user's permissions change. Every one of those is an ANSWER from the
 * authority. A probe that failed to complete is not an answer at all.
 *
 * This module exists because that distinction was lost THREE times, in three
 * languages, in the same auth path:
 *   * `hook_broker_client` returned a bare None from ten distinct failures, so
 *     a security discard read the same as "daemon not running" (#504);
 *   * `_token_is_valid` in main.rs mapped `Err(_) => false`, so a failed CLI
 *     spawn read as "your token is invalid" and the binding approval refused
 *     with a valid token in hand (#508);
 *   * `App.tsx`'s checkDesktopAuth caught a rejected probe and called
 *     setDesktopAuthed(false) — and because that callback keys on
 *     selectedProjectRoot, it re-ran on EVERY project switch and bounced the
 *     operator to the login form mid-session (#509, reported live).
 *
 * Keeping the rule in one tiny pure function makes it testable and makes the
 * next occurrence a failing test instead of a bug report.
 */

/** Outcome of one auth probe: either the authority ANSWERED, or we could not ask. */
export type AuthProbe =
  | { ok: true; authenticated: boolean }
  | { ok: false };

/**
 * Fold a probe outcome into the next authenticated state.
 *
 * `previous === null` means NOT YET PROBED — App.tsx renders a LoadingOverlay
 * in that state, which is why the three cases below are not two:
 *
 * - The authority ANSWERED -> take its answer verbatim, including NO. A real
 *   revocation, ban, or removal must still sign the operator out; fail-soft
 *   must never become sticky.
 * - Could not ask, and we HAVE a prior verdict -> KEEP it. This is the #509
 *   fix: never destroy an established session over a question we failed to ask.
 * - Could not ask, and there is NO prior verdict (cold start) -> `false`, i.e.
 *   show the login form. Fail CLOSED, and deliberately not `null`: preserving
 *   "unknown" here would strand the app on the loading overlay forever, which
 *   is a worse outcome than asking the operator to sign in.
 *
 * So the guarantee is narrow and precise: this never INVENTS a session, and it
 * never DESTROYS one — it only declines to guess.
 */
export function nextDesktopAuthed(previous: boolean | null, probe: AuthProbe): boolean {
  if (probe.ok) return probe.authenticated;
  return previous ?? false;
}
