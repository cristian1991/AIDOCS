# ══════════════════════════════════════════════════════════════════════════
#  ⚠️  DO NOT TOUCH WITHOUT EXPLICIT USER REQUEST  ⚠️
# ──────────────────────────────────────────────────────────────────────────
#  M2 tenant-isolation credential custody boundary. Fetches a JIT decrypted GitHub token from CodeNexus's internal S2S API keyed by the bound org (tenant_id); MUST stay fail-closed (None on any error), MUST NOT persist or log the token, and MUST NOT fall back to a shared platform token when a tenant is bound. Changing this weakens cross-org isolation. The line-30 `# gitleaks:allow` is intentional (env var NAME, not a secret).
#
# ══════════════════════════════════════════════════════════════════════════
"""Just-in-time per-org GitHub credential resolution for WebMCP private-repo import.

Custody model (M2, the "120% way"): CodeNexus owns the encryption key and the token
ciphertext. The gate NEVER holds the key and NEVER reads the credential row over the
read-only DSN. At clone time it asks CodeNexus's authenticated internal S2S endpoint
for a decrypted token scoped to the request's BOUND org (tenant_id), uses it for that
single clone, and never persists it. Org A's token is unreachable to org B — the same
tenant isolation the rest of the gate enforces, extended to credentials.

Fail-closed everywhere: any missing config, non-200, malformed body, or transport
error resolves to None (→ the importer refuses with repo_inaccessible). The token is
never logged.

Config (set ONLY on the live webmcp gate host; unset elsewhere so this is a no-op):
  * AIDOCS_CODENEXUS_INTERNAL_URL — base URL of the CodeNexus internal API
    (e.g. http://127.0.0.1:3001). Unset ⇒ resolution disabled.
  * AIDOCS_INTERNAL_S2S_SECRET   — shared bearer secret, identical on both processes.
    Unset ⇒ resolution disabled.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

_INTERNAL_URL_ENV = "AIDOCS_CODENEXUS_INTERNAL_URL"
_INTERNAL_SECRET_ENV = "AIDOCS_INTERNAL_S2S_SECRET"  # gitleaks:allow (env var NAME, not a value)
_TIMEOUT_S = 8.0


def resolve_org_github_token(tenant_id: str | None) -> str | None:
    """Return the bound org's decrypted GitHub token, or None (fail-closed).

    The token is held only by the caller for the duration of one clone and must never
    be persisted or logged.
    """
    if not tenant_id or not str(tenant_id).strip():
        return None
    base = os.environ.get(_INTERNAL_URL_ENV, "").strip()
    secret = os.environ.get(_INTERNAL_SECRET_ENV, "").strip()
    if not base or not secret:
        return None
    url = (
        base.rstrip("/")
        + "/api/internal/github-token?orgId="
        + urllib.parse.quote(str(tenant_id).strip(), safe="")
    )
    req = urllib.request.Request(  # noqa: S310 — fixed internal host from operator env
        url,
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None
