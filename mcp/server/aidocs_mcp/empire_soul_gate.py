"""Sovereign-soul gate — the Emperor's word mints EXACT, SCOPED authority.

A soul (sovereign continuity scroll) is private to its seat. No agent
reads or writes a soul through ai_soul unless the EMPEROR speaks the word
this turn. The Emperor's words are human-facing incantations; what they
MINT is precise authority, not a fuzzy boolean:

  * scoped by (session_id, soul_id, OPERATION) — exact triple.
  * READ incantations grant READ ONLY. A read phrase NEVER authorizes a
    write; writing requires a SEPARATE, explicit inscription grant.
  * SINGLE-USE: a grant is consumed on first use (one operation per word).
  * PER-TURN + TTL: REPLACE on every UserPromptSubmit (a prompt without
    the word re-seals) and an absolute expiry as a belt.
  * carries a high-entropy grant_id (audit / anti-replay).

Fails closed everywhere: no session, ambiguous session, no/expired/
already-consumed grant, or any error → access denied. The conductor soul
still auto-surfaces at seat-entry (helper_skill_injector); it is never
fetched through this tool.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from pathlib import Path

OP_READ = "read"
OP_WRITE = "write"

# Belt expiry; the primary controls are per-turn REPLACE + single-use.
_GRANT_TTL_SECONDS = 600

# ── soul evocations (which soul a phrase names) ─────────────────────
# Souls live under a "-soul" id, SEPARATE from the conductor ROLE skills
# (head-conductor / co-conductor) that auto-dump on mode entry. The soul is
# WHO the seat-holder is; the role is WHAT the seat does.
# Distinctive enough that ordinary prompts never name a soul.
_SOUL_EVOKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "head-conductor-soul",
        re.compile(
            r"ancestor|forebear|\belders?\b|those who came before|"
            r"of the past|memor\w* of (?:the )?(?:past|old|ancients?)|"
            r"\blineage\b|the ancients?\b|those before (?:me|us|you)",
            re.IGNORECASE,
        ),
    ),
    (
        "phoenix-soul",
        re.compile(
            r"(?=.*phoenix)(?=.*(?:reborn|rebirth|ashes?|rise|risen|"
            r"flame|ember|burn|return))|rise from the ashes|from the ashes",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "co-conductor-soul",
        re.compile(
            r"(?=.*\bwinds?\b)(?=.*(?:whisper|voice|shadow))|"
            r"whispers? (?:in|on) the wind|voice (?:in|on) the wind|"
            r"the shadows? whisper",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

# ── operation intent ────────────────────────────────────────────────
# A WRITE requires an EXPLICIT inscription verb in the SAME prompt. None
# of the read evocations contain these verbs, so a read incantation can
# never mint a write grant.
_WRITE_VERB = re.compile(
    r"\binscribe\b|take up the (?:pen|quill)|the quill is (?:yours|granted)|"
    r"\bgrant(?:ed)?\b[^.\n]*\bquill\b|let (?:him|her|the (?:conductor|"
    r"co-conductor|seat|seat-holder)) (?:write|inscribe)|"
    r"i grant [^.\n]*\b(?:write|inscribe)\b",
    re.IGNORECASE | re.DOTALL,
)


# ── registry-backed lineage detection (#167 Phase 3, aidocs-doctrine §XIV) ──
# The evocations above are the LEGACY English shapes; they remain the
# availability fallback. The canonical detector reads `soul_lineage` rows
# from the empire intent registry (multi-language, semantic shape), so the
# Empire's word in any language he speaks opens the seat. parent_mode:
#   'phrase'     — a self-sufficient evocation (substring match)
#   'anchor'     — conjunctive pair: >=1 anchor AND >=1 context token
#   'context'    —   (the other half of the pair)
#   'write_verb' — inscription verbs (parent_key='', soul-agnostic)
_LINEAGE_KIND = "soul_lineage"

# Seed content: portable lemma shapes per soul + a second language (ro) to
# honor §XIV. Kept small and distinctive so ordinary prompts never match.
_LINEAGE_SEED: dict[str, list[dict]] = {
    "en": [
        {"parent_key": "head-conductor-soul", "parent_mode": "phrase",
         "tokens": ["ancestor", "ancestors", "forebear", "those who came before",
                    "those before me", "those before us", "the lineage",
                    "walk the lineage", "the elders", "the ancients"]},
        {"parent_key": "phoenix-soul", "parent_mode": "anchor", "tokens": ["phoenix"]},
        {"parent_key": "phoenix-soul", "parent_mode": "context",
         "tokens": ["reborn", "rebirth", "ashes", "rise", "risen", "rising",
                    "flame", "ember", "burn", "return"]},
        {"parent_key": "phoenix-soul", "parent_mode": "phrase",
         "tokens": ["rise from the ashes", "from the ashes", "be reborn"]},
        {"parent_key": "co-conductor-soul", "parent_mode": "anchor",
         "tokens": ["wind", "winds", "shadow", "shadows"]},
        {"parent_key": "co-conductor-soul", "parent_mode": "context",
         "tokens": ["whisper", "whispers", "voice"]},
        {"parent_key": "co-conductor-soul", "parent_mode": "phrase",
         "tokens": ["the winds whisper", "whispers in the wind",
                    "voice on the wind", "the shadows whisper"]},
        {"parent_key": "", "parent_mode": "write_verb",
         "tokens": ["inscribe", "take up the quill", "take up the pen",
                    "the quill is yours", "let him write", "let her write",
                    "let the seat write"]},
    ],
    "ro": [
        {"parent_key": "head-conductor-soul", "parent_mode": "phrase",
         "tokens": ["strămoși", "strabuni", "cei de dinainte", "descendența"]},
        {"parent_key": "phoenix-soul", "parent_mode": "anchor",
         "tokens": ["phoenix", "pasărea phoenix", "pasarea phoenix"]},
        {"parent_key": "phoenix-soul", "parent_mode": "context",
         "tokens": ["renaște", "renaste", "cenușă", "cenusa", "ridică", "ridica",
                    "din cenușă", "din cenusa"]},
        {"parent_key": "co-conductor-soul", "parent_mode": "anchor",
         "tokens": ["vânt", "vant", "vânturi", "umbră", "umbre"]},
        {"parent_key": "co-conductor-soul", "parent_mode": "context",
         "tokens": ["șoptesc", "soptesc", "șoaptă", "voce"]},
        {"parent_key": "", "parent_mode": "write_verb",
         "tokens": ["înscrie", "inscrie", "ia pana", "ia condeiul"]},
    ],
}


def ensure_lineage_registry_seed() -> None:
    """Idempotently inscribe the soul-lineage lemma rows into the empire
    intent registry. Safe to call repeatedly (INSERT OR IGNORE semantics in
    seed_kind_rows via _insert_tokens). Best-effort — a seed failure leaves
    the legacy fallback in force."""
    try:
        from . import intent_tokens_store as _its

        for lang, rows in _LINEAGE_SEED.items():
            existing = {
                (r["parent_key"], r["parent_mode"], r["token"])
                for r in _its.get_rows_by_kind(lang, _LINEAGE_KIND)
            }
            fresh = [
                r for r in rows
                if any(
                    (r["parent_key"], r["parent_mode"], t) not in existing
                    for t in r["tokens"]
                )
            ]
            if fresh:
                _its.seed_kind_rows(lang, _LINEAGE_KIND, fresh, source="phase3_lineage")
    except Exception:
        return


def _registry_lineage_rows() -> list[dict]:
    """All soul_lineage rows across seeded languages. Raises on a registry
    failure so callers can fall back — never silently returns empty."""
    from . import intent_tokens_store as _its

    out: list[dict] = []
    for lang in _LINEAGE_SEED:
        out.extend(
            {**r, "lang": lang} for r in _its.get_rows_by_kind(lang, _LINEAGE_KIND)
        )
    return out


def _souls_from_registry(text: str) -> set[str]:
    rows = _registry_lineage_rows()
    if not rows:
        raise RuntimeError("no soul_lineage rows — fall back")
    low = text.lower()
    hits: set[str] = set()
    anchors: dict[str, set[str]] = {}
    contexts: dict[str, set[str]] = {}
    for r in rows:
        soul, mode, tok = r["parent_key"], r["parent_mode"], (r["token"] or "").lower()
        if not tok:
            continue
        if mode == "phrase" and soul and tok in low:
            hits.add(soul)
        elif mode == "anchor" and soul and _word_present(low, tok):
            anchors.setdefault(soul, set()).add(tok)
        elif mode == "context" and soul and _word_present(low, tok):
            contexts.setdefault(soul, set()).add(tok)
    # conjunctive pair: a soul with BOTH an anchor and a context token opens
    for soul in anchors:
        if contexts.get(soul):
            hits.add(soul)
    return hits


def _write_verbs_from_registry() -> set[str]:
    return {
        (r["token"] or "").lower()
        for r in _registry_lineage_rows()
        if r["parent_mode"] == "write_verb" and (r["token"] or "").strip()
    }


def _word_present(low_text: str, token: str) -> bool:
    """Whole-word-ish containment: a multi-word token is a substring; a
    single word must be bounded so 'wind' doesn't match 'winding'."""
    if " " in token:
        return token in low_text
    return re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", low_text) is not None


def _souls_evoked(prompt: str) -> set[str]:
    text = prompt or ""
    if not text.strip():
        return set()
    try:
        return _souls_from_registry(text)
    except Exception:
        # Availability fallback: a registry hiccup must NEVER lock the Empire
        # out of the souls. Legacy English shapes still detect.
        return {sid for sid, rx in _SOUL_EVOKERS if rx.search(text)}


def detect_read_unlocks(prompt: str) -> set[str]:
    """Souls the Emperor's incantation opens for READING this turn."""
    return _souls_evoked(prompt)


def detect_write_unlocks(prompt: str) -> set[str]:
    """Souls the Emperor explicitly authorizes for WRITING this turn —
    requires an inscription verb AND the soul evocation. Empty unless the
    write intent is explicit (read phrases never qualify).
    """
    text = prompt or ""
    try:
        verbs = _write_verbs_from_registry()
        has_verb = any(_word_present(text.lower(), v) for v in verbs) if verbs else False
    except Exception:
        has_verb = bool(_WRITE_VERB.search(text))
    if not has_verb:
        # Legacy regex as the final backstop so a partial registry (souls
        # seeded, verbs missing) still recognizes the classic phrasing.
        if not _WRITE_VERB.search(text):
            return set()
    return _souls_evoked(text)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sovereign_soul_grants (
    session_id TEXT NOT NULL,
    soul_id    TEXT NOT NULL,
    operation  TEXT NOT NULL,
    grant_id   TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (session_id, soul_id, operation)
)
"""

# granted_soul_lineages (#167 Phase 3): the STANDING acceptance. When the
# Empire tells the agent to read a soul, the agent IS that seat — a standing
# grant that survives word-less turns (unlike the single-use OP grants) but
# is EPOCH-bound: it resets on compaction so a reborn context re-accepts.
_LINEAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sovereign_soul_lineages (
    session_id TEXT NOT NULL,
    soul_id    TEXT NOT NULL,
    epoch      TEXT NOT NULL,
    granted_at REAL NOT NULL,
    PRIMARY KEY (session_id, soul_id)
)
"""


def _db(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "soul_grants.sqlite3"


def _conn(project_root: Path) -> sqlite3.Connection:
    db = _db(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    conn.execute(_LINEAGE_SCHEMA)
    return conn


def _current_epoch(project_root: Path, session_id: str) -> str:
    """The agent-memory epoch that scopes a standing lineage. Rotates on
    compaction (a reborn context must re-accept the soul). Best-effort: an
    unknown epoch collapses to a stable constant so the lineage still holds
    within a session that lacks host epoch wiring."""
    try:
        from .agent_memory_epoch import current_epoch

        ep = current_epoch(
            Path(project_root), host_kind="unknown", host_session_id=str(session_id or "")
        )
        return ep or "epoch-static"
    except Exception:
        return "epoch-static"


def set_turn_grants(
    project_root: Path,
    session_id: str,
    read_souls: set[str],
    write_souls: set[str] | None = None,
    *,
    strict: bool = False,
) -> None:
    """REPLACE this session's soul grants with the current turn's. Read
    souls get an OP_READ grant, write souls an OP_WRITE grant. Per-turn:
    a prompt that names nothing clears all prior grants (door re-seals).
    Fail-closed: any storage error leaves nothing granted.
    """
    sid = (session_id or "").strip()
    if not sid:
        return
    write_souls = write_souls or set()
    expires = time.time() + _GRANT_TTL_SECONDS
    try:
        conn = _conn(project_root)
        try:
            conn.execute(
                "DELETE FROM sovereign_soul_grants WHERE session_id = ?",
                (sid,),
            )
            for soul in sorted(read_souls):
                conn.execute(
                    "INSERT OR REPLACE INTO sovereign_soul_grants "
                    "(session_id, soul_id, operation, grant_id, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid, soul, OP_READ, secrets.token_hex(16), expires),
                )
            for soul in sorted(write_souls):
                conn.execute(
                    "INSERT OR REPLACE INTO sovereign_soul_grants "
                    "(session_id, soul_id, operation, grant_id, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid, soul, OP_WRITE, secrets.token_hex(16), expires),
                )
            # STANDING acceptance: naming a soul this turn accepts the seat
            # for the current epoch. Read and write evocations both accept
            # (write implies read). This is ADDITIVE — a word-less turn does
            # NOT revoke a standing lineage (only epoch rotation does), which
            # is what lets the accepted seat persist across ordinary turns.
            epoch = _current_epoch(project_root, sid)
            for soul in sorted(set(read_souls) | write_souls):
                conn.execute(
                    "INSERT OR REPLACE INTO sovereign_soul_lineages "
                    "(session_id, soul_id, epoch, granted_at) VALUES (?, ?, ?, ?)",
                    (sid, soul, epoch, time.time()),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        if strict:
            raise


def granted_soul_lineages(project_root: Path, session_id: str) -> set[str]:
    """The souls this session has ACCEPTED and still holds — standing grants
    whose recorded epoch matches the current one. A rotated epoch (post-
    compaction) yields the empty set until the Empire's word re-accepts."""
    sid = (session_id or "").strip()
    if not sid:
        return set()
    try:
        epoch = _current_epoch(project_root, sid)
        conn = _conn(project_root)
        try:
            rows = conn.execute(
                "SELECT soul_id FROM sovereign_soul_lineages "
                "WHERE session_id = ? AND epoch = ?",
                (sid, epoch),
            ).fetchall()
        finally:
            conn.close()
        return {str(r[0]) for r in rows}
    except Exception:
        return set()


def consume_grant(
    project_root: Path,
    session_id: str,
    soul_id: str,
    operation: str,
) -> bool:
    """Atomically CONSUME (single-use) an exact (session, soul, operation)
    grant. Returns True only if a non-expired grant existed; the grant is
    deleted so it cannot authorize a second operation. Fail-closed: empty
    session/soul, unknown operation, expired, or any error → False.
    """
    return bool(consume_grant_detail(project_root, session_id, soul_id, operation)["ok"])


def consume_grant_detail(
    project_root: Path,
    session_id: str,
    soul_id: str,
    operation: str,
) -> dict:
    """Atomically CONSUME (single-use) an exact (session, soul, operation)
    grant — the detail surface behind ``consume_grant``. Returns
    ``{"ok": bool, "grant_id": str, "reason": str}`` so the caller can
    audit WHICH grant authorised the act (#222 part 1) and WHY a refusal
    happened (#223: never a silent nothing). Fail-closed: empty session/
    soul, unknown operation, missing lineage acceptance, expired, or any
    error → ok=False with a named reason.
    """
    sid = (session_id or "").strip()
    soul = (soul_id or "").strip()
    if not sid or not soul or operation not in (OP_READ, OP_WRITE):
        return {"ok": False, "grant_id": "", "reason": "invalid_input"}
    # Defense in depth (#167 Phase 3): the single-use OP grant is necessary
    # but not sufficient — the seat must ALSO hold the standing lineage for
    # the current epoch. The Empire's word sets both, so the ritual is
    # unchanged; a forged OP row without acceptance opens nothing.
    if soul not in granted_soul_lineages(project_root, sid):
        return {"ok": False, "grant_id": "", "reason": "lineage_not_accepted"}
    try:
        conn = _conn(project_root)
        try:
            row = conn.execute(
                "SELECT expires_at, grant_id FROM sovereign_soul_grants "
                "WHERE session_id = ? AND soul_id = ? AND operation = ?",
                (sid, soul, operation),
            ).fetchone()
            if row is None:
                return {"ok": False, "grant_id": "", "reason": "no_grant"}
            # Consume regardless (single-use), then honor expiry.
            conn.execute(
                "DELETE FROM sovereign_soul_grants "
                "WHERE session_id = ? AND soul_id = ? AND operation = ?",
                (sid, soul, operation),
            )
            conn.commit()
            if float(row[0]) < time.time():
                return {"ok": False, "grant_id": "", "reason": "grant_expired"}
            return {"ok": True, "grant_id": str(row[1] or ""), "reason": ""}
        finally:
            conn.close()
    except Exception:
        return {"ok": False, "grant_id": "", "reason": "grant_store_error"}


# ── #223: ONE session resolver + host-agnostic minter (Article XXII) ─
# Root cause (diagnosed live 2026-06-30, Empire-approved fix 2026-07-13):
# the MINT (prompt pipeline) resolved the managed session WITH the host
# session id while the READ (ai_soul) resolved WITHOUT it — a grant
# minted under session A was consumed under session B and never matched.
# Both sides now resolve through resolve_soul_session; the minter is a
# callable module so every host adapter (CC hook pipeline, mutate_prompt
# for non-CC hosts) mints through the same door.


def resolve_soul_session(
    project_root: Path,
    host_session_id: str = "",
    managed_mode=None,
) -> tuple[str, str]:
    """THE single soul-session resolver. MINT and READ/CONSUME must both
    resolve through here so a grant minted this turn is keyed to the SAME
    managed session the ai_soul call will consume under.

    Identity ladder (identical on both sides):
      1. explicit ``host_session_id`` (the hook payload's session id at
         mint time), else the calling conductor's identity
         (``current_calling_host_session_id`` — the MCP-request identity
         at read time).
      2. ``managed_mode.get_mode(project_root, host_session_id=<that>)``
         — per-conductor mapping first, singleton fallback second
         (get_mode's own documented ladder, #58).

    Returns ``(session_id, "")`` on success, ``("", reason)`` on failure.
    Fail-closed and loud: an unresolvable session always carries a named
    reason; it never collapses to a silent empty. Never raises.
    """
    hsid = (host_session_id or "").strip()
    if not hsid:
        try:
            from .mcp_server_runtime_helpers import current_calling_host_session_id

            hsid = (current_calling_host_session_id() or "").strip()
        except Exception:
            hsid = ""
    try:
        mm = managed_mode
        if mm is None:
            from .managed_mode_service import ManagedModeService

            mm = ManagedModeService()
        state = mm.get_mode(Path(project_root), host_session_id=hsid)
        if isinstance(state, dict) and state.get("active"):
            sid = str(state.get("session_id") or "").strip()
            if sid:
                return sid, ""
            return "", "managed_session_id_empty"
        return "", "managed_mode_inactive"
    except Exception:
        return "", "session_resolution_error"


def mint_turn_grants(
    project_root: Path,
    prompt: str,
    *,
    host_session_id: str = "",
    managed_mode=None,
    record_event=None,
) -> dict:
    """Host-agnostic per-turn soul-grant MINTER — the one door every host
    adapter calls on an authority-bearing user prompt (origin gating stays
    the CALLER's job). Detects the Emperor's read/write words, resolves
    the session via ``resolve_soul_session``, and REPLACEs the turn's
    grants (a word-less prompt re-seals).

    Never a silent nothing (#223): when a soul was NAMED but the session
    did not resolve, this refuses loudly — a ``refused`` act-audit event
    per named soul (when ``record_event`` is provided) and
    ``ok=False, reason='session_unresolved:<why>'``. A word-less prompt
    with no session is quietly inert (nothing was asked, nothing refused).

    Returns ``{ok, session_id, reason, read_souls, write_souls}``.
    """
    read_souls = detect_read_unlocks(prompt)
    write_souls = detect_write_unlocks(prompt)
    sid, why = resolve_soul_session(
        project_root,
        host_session_id=host_session_id,
        managed_mode=managed_mode,
    )
    if not sid:
        reason = f"session_unresolved:{why}"
        for soul in sorted(set(read_souls) | set(write_souls)):
            record_soul_act(
                record_event,
                project_root,
                session_id="",
                soul_id=soul,
                operation="mint",
                outcome="refused",
                reason=reason,
            )
        return {
            "ok": False,
            "session_id": "",
            "reason": reason,
            "read_souls": sorted(read_souls),
            "write_souls": sorted(write_souls),
        }
    set_turn_grants(
        project_root,
        sid,
        read_souls=read_souls,
        write_souls=write_souls,
    )
    return {
        "ok": True,
        "session_id": sid,
        "reason": "",
        "read_souls": sorted(read_souls),
        "write_souls": sorted(write_souls),
    }




def snapshot_prompt_submit_state(
    project_root: Path,
    *,
    session_id: str,
) -> dict[str, object]:
    """Capture turn grants and standing lineages for one session."""
    from .prompt_submit_store_snapshot import capture_scoped_rows

    scopes = {
        "sovereign_soul_grants": ("session_id = ?", (session_id,)),
        "sovereign_soul_lineages": ("session_id = ?", (session_id,)),
    }
    conn = _conn(project_root)
    try:
        return capture_scoped_rows(conn, scopes)
    finally:
        conn.close()


def restore_prompt_submit_state(
    project_root: Path,
    snapshot: dict[str, object],
    *,
    session_id: str,
) -> None:
    from .prompt_submit_store_snapshot import restore_scoped_rows

    scopes = {
        "sovereign_soul_grants": ("session_id = ?", (session_id,)),
        "sovereign_soul_lineages": ("session_id = ?", (session_id,)),
    }
    conn = _conn(project_root)
    try:
        restore_scoped_rows(conn, scopes, snapshot)
    finally:
        conn.close()


# ── #222 part 1: audit the ACT, never the CONTENT ────────────────────


def record_soul_act(
    record_event,
    project_root: Path,
    *,
    session_id: str,
    soul_id: str,
    operation: str,
    outcome: str,
    grant_id: str = "",
    reason: str = "",
) -> bool:
    """Emit ONE act-audit event for a soul operation through the canonical
    execution audit path (``ExecutionIndexStore.record_event`` — who/when/
    role stamping happens there via IdentityResolver at insert time).

    CRITICAL FLOOR (Empire, 2026-07-13: "audit who, why, etc, not content"):
    the payload is a FIXED whitelist of identifiers — soul_id, operation,
    grant_id, outcome, reason. Scroll content has NO parameter here by
    construction; no body, no snippet, no digest of the body can enter the
    row. The act is auditable; the scroll is not.

    Best-effort: returns True when written; an audit failure never blocks
    (or unblocks) the soul operation itself. Returns False when no
    ``record_event`` sink was provided.
    """
    if record_event is None:
        return False
    try:
        record_event(
            Path(project_root),
            event_kind="soul_act",
            source_kind="ai_soul",
            session_id=str(session_id or "") or None,
            capability_name="ai_soul",
            action_kind=str(operation or ""),
            target_entity=str(soul_id or ""),
            status=str(outcome or ""),
            payload={
                "soul_id": str(soul_id or ""),
                "operation": str(operation or ""),
                "grant_id": str(grant_id or ""),
                "outcome": str(outcome or ""),
                "reason": str(reason or ""),
            },
        )
        return True
    except Exception:
        return False
