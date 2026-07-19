"""Live model-list resolution for worker backends (claude / codex / opencode).

ai_models() used to hardcode claude+codex and shell `opencode models` (which hung
on an interactive/network wait). This resolves a LIVE list per FULL host:

  claude   → Anthropic Models API   GET https://api.anthropic.com/v1/models
  codex    → OpenAI  Models API     GET https://api.openai.com/v1/models
  opencode → `opencode models`      (stdin closed + bounded — no hang)

…with a graceful FALLBACK to a known list (claude/codex) or a clear error
(opencode) so the dashboard model dropdown is never empty, hung, or stale.

Dependency-injected (env / http_get / run_opencode / which) so the logic is unit
tested fully offline (mcp/tests/host/test_backend_models.py). Never raises — a
broken live source degrades, it does not crash the caller.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Callable, Optional

# Known-good fallbacks — used when no API key is present or the live source fails.
# Never empty: the dashboard must always have something selectable.
_FALLBACK_CLAUDE = [
    "opus",
    "sonnet",
    "haiku",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]
_FALLBACK_CODEX = ["gpt-5", "gpt-4o", "o3", "o3-mini"]

_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
_HTTP_TIMEOUT = 10
_OPENCODE_TIMEOUT = 15

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _default_http_get(url: str, headers: dict[str, str]) -> Any:
    from urllib.request import Request, urlopen

    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 — trusted provider hosts
        return json.loads(resp.read().decode("utf-8"))


def _default_run_opencode(oc: str) -> subprocess.CompletedProcess:
    # stdin=DEVNULL kills the interactive-prompt hang (the reported bug); the
    # timeout caps a network stall so this can never block the conductor/dashboard.
    # #345: routed through audited_run (ledger row per spawn). Passthrough
    # lambda IS the registered AST callsite; kwargs pass through UNCHANGED.
    from .shell_egress_service import audited_run

    return audited_run(
        [oc, "models"],
        fingerprint=("backend_models.py", "_default_run_opencode", "subprocess.run"),
        reason="opencode-models-probe",
        # nosemgrep: aidocs-direct-subprocess-outside-shell-egress  # OL host-CLI probe: fixed argv `opencode models`, shell=False, read-only catalog query, not agent-controlled, DEVNULL stdin + bounded timeout; registered in shell_egress_service.LEGACY_SUBPROCESS_CALLSITES
        run=lambda *a, **kw: subprocess.run(*a, **kw),  # nosemgrep: aidocs-direct-subprocess-outside-shell-egress
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=_OPENCODE_TIMEOUT,
        creationflags=_WIN_NO_WINDOW,
    )


def _ids_from_data(payload: Any) -> list[str]:
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


def list_backend_models(
    backend: str,
    *,
    env: Optional[dict] = None,
    http_get: Optional[Callable[[str, dict], Any]] = None,
    run_opencode: Optional[Callable[[str], subprocess.CompletedProcess]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> dict[str, Any]:
    """LIVE model slugs for a worker backend, with graceful fallback.

    Returns {backend, models: [...], source, [error]}.
    """
    env = os.environ if env is None else env
    http_get = _default_http_get if http_get is None else http_get
    run_opencode = _default_run_opencode if run_opencode is None else run_opencode
    which = shutil.which if which is None else which

    if backend == "claude":
        return _provider_models(
            "claude",
            env.get("ANTHROPIC_API_KEY"),
            _ANTHROPIC_MODELS_URL,
            lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"},
            "anthropic /v1/models",
            _FALLBACK_CLAUDE,
            "ANTHROPIC_API_KEY",
            http_get,
        )
    if backend == "codex":
        return _provider_models(
            "codex",
            env.get("OPENAI_API_KEY"),
            _OPENAI_MODELS_URL,
            lambda k: {"Authorization": f"Bearer {k}"},
            "openai /v1/models",
            _FALLBACK_CODEX,
            "OPENAI_API_KEY",
            http_get,
        )
    if backend == "opencode":
        return _opencode_models(which, run_opencode)
    return {"backend": backend, "models": [], "error": f"unknown backend: {backend!r}"}


def _provider_models(
    backend: str,
    key: Optional[str],
    url: str,
    headers_for: Callable[[str], dict],
    source: str,
    fallback: list[str],
    key_name: str,
    http_get: Callable[[str, dict], Any],
) -> dict[str, Any]:
    if not key:
        return {"backend": backend, "models": list(fallback), "source": f"fallback (no {key_name})"}
    try:
        models = _ids_from_data(http_get(url, headers_for(key)))
        if models:
            return {"backend": backend, "models": models, "source": source}
        return {"backend": backend, "models": list(fallback), "source": "fallback (empty live response)"}
    except Exception as exc:
        return {"backend": backend, "models": list(fallback), "source": "fallback", "error": str(exc)}


def _opencode_models(
    which: Callable[[str], Optional[str]],
    run_opencode: Callable[[str], subprocess.CompletedProcess],
) -> dict[str, Any]:
    oc = which("opencode")
    if not oc:
        return {"backend": "opencode", "models": [], "error": "opencode CLI not found"}
    try:
        r = run_opencode(oc)
    except Exception as exc:
        return {"backend": "opencode", "models": [], "error": str(exc)}
    if r.returncode == 0:
        models = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        return {"backend": "opencode", "models": models, "source": "opencode models"}
    return {"backend": "opencode", "models": [], "error": (r.stderr or "")[-500:]}
