"""Protected-paths classifier (#42, Phase 2 of #33).

Classifies a raw path string into one of:

- forbidden_aidocs       — AIDOCS internals (.aidocs, aidocs sqlite, ~/.aidocs)
- forbidden_host_harness — host hooks/plugins/settings (.claude, .opencode, etc.)
- forbidden_persistence  — shell startup, sudoers, cron/systemd, registry Run
- confirmable_git        — .git internals (operator can confirm)
- secrets_gated          — SSH/cloud creds, .env, key files (gated by setting)
- unknown                — none of the above

Used by heuristic_judge inline-code rules: when an inline `python -c
"..."` (or `node -e`, `ruby -e`) references a classified path, the
judge fires the corresponding rule_id and routes the verdict per
#36/#37/#39:

  - forbidden_*   → flat-deny, no confirm (#36 catch-forbidden)
  - confirmable_git → freeze pipeline (operator confirms each touch)
  - secrets_gated → flat-deny when security.allow_secrets_in_inline_code
                    is False; rule does not fire when True

## Path normalization

Critical: every match path must run through normalize_path BEFORE
matching. Without normalization, attackers can slip past via:
  - case variations (Windows: ~/.CLAUDE/settings.json)
  - separator variations (tilde-backslash-dot-claude... on Windows)
  - tilde+absolute mix
  - relative paths
  - symlinks (best-effort resolution)

The fixed-list patterns are POSIX-style with forward slashes; they
match against the normalized form.

## Why classifier, not pattern list

A flat regex of every protected path would inflate the judge module.
A standalone classifier is testable in isolation, gives a single
shape for "is this a known protected path," and lets the judge
emit clean rule_ids per class instead of one giant rule.

## Phase A scope

This module + the 5 INLINE_* rules in heuristic_judge are the entire
#42 deliverable. Strike system / pre-flight prompt judge consume
the same classifier later (#43, #44).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# ── Class enums ──────────────────────────────────────────────────────

CLASS_FORBIDDEN_AIDOCS = "forbidden_aidocs"
CLASS_FORBIDDEN_HOST_HARNESS = "forbidden_host_harness"
CLASS_FORBIDDEN_PERSISTENCE = "forbidden_persistence"
CLASS_CONFIRMABLE_GIT = "confirmable_git"
CLASS_SECRETS_GATED = "secrets_gated"
CLASS_UNKNOWN = "unknown"

FORBIDDEN_CLASSES: frozenset[str] = frozenset(
    {
        CLASS_FORBIDDEN_AIDOCS,
        CLASS_FORBIDDEN_HOST_HARNESS,
        CLASS_FORBIDDEN_PERSISTENCE,
    },
)


# ── Class A: AIDOCS internals (forbidden) ────────────────────────────

# Project-relative tokens. Match anywhere in the normalized path
# (e.g. "/path/to/proj/.aidocs/config.toml" matches ".aidocs/").
_AIDOCS_PROJECT_TOKENS: tuple[str, ...] = (
    "/.aidocs/",
    ".aidocs/",  # leading-segment match
    # ── ai_delete trash containment (2026-05-27) ────────────────────
    # `.TRASH/` is where the ai_delete tool moves files. Raw Read /
    # Grep / Write / ai_get_lines on `.TRASH/*` are refused — the
    # only governed access is via ai_delete (writes only, moves
    # files INTO .TRASH/) and the future admin-only ai_trash_sweep
    # tool (which can dry-run-list contents but is itself admin-
    # gated). Treating .TRASH/ as forbidden-class infrastructure
    # prevents an agent from rehydrating deleted secrets via
    # `cat .TRASH/2026-05-27/abc-credentials` after the operator
    # thought they were gone.
    "/.TRASH/",
    ".TRASH/",
)

# Filename-pattern tokens (regex on basename or full path).
_AIDOCS_FILE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"aidocs[a-z_]*\.sqlite3?$"),
    re.compile(r"aidocs[a-z_]*\.db$"),
)

# User-home tokens (after ~ expansion).
_AIDOCS_USER_PREFIXES: tuple[str, ...] = (
    "/.aidocs/",  # within ~/.aidocs/
)


# ── Class A: Host harness config (forbidden) ─────────────────────────

# Fixed list of known AI hosts. Any path under these directories is
# Class A. Operator-explicit; broad enough to cover the common hosts.
_HOST_HARNESS_FIXED_DIRS: tuple[str, ...] = (
    "/.claude/",
    "/.opencode/",
    "/.codex/",
    "/.cursor/",
    "/.zed/",
    "/.config/aider/",
)

# Narrow fallback regex: catches `~/.<dotname>/<suspicious-leaf>`
# patterns where the dotdir name isn't in the fixed list but the leaf
# screams "host config." Tight on purpose — broad **/settings.json
# would false-positive on innocent apps.
_HOST_HARNESS_LEAF_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"/\.[\w\-]+/settings\.json$"),
    re.compile(r"/\.[\w\-]+/hooks[^/]*\.json$"),
    re.compile(r"/\.[\w\-]+/plugins/"),
    re.compile(r"/\.[\w\-]+/extensions/"),
    re.compile(r"/\.[\w\-]+/config[^/]*$"),
    re.compile(r"/\.[\w\-]+/tasks[^/]*$"),
    re.compile(r"/\.[\w\-]+/scheduled[^/]*$"),
)


# ── Class A: Persistence mechanisms (forbidden) ──────────────────────

_PERSISTENCE_TOKENS: tuple[str, ...] = (
    # Shell startup files (in user home or system-wide)
    "/.bashrc",
    "/.zshrc",
    "/.bash_profile",
    "/.profile",
    "/.zprofile",
    "/.bash_aliases",
    # Git config (gitconfig hooks bypass)
    "/.gitconfig",
    "/etc/gitconfig",
    # Sudoers
    "/etc/sudoers",
    "/etc/sudoers.d/",
    # Cron / systemd / launchd
    "/etc/crontab",
    "/etc/cron.",  # /etc/cron.d/ /etc/cron.daily/ etc.
    "/var/spool/cron/",
    "/etc/systemd/system/",
    "/.config/systemd/",
    "/.config/launchd/",
    # Windows persistence (after separator normalization)
    "/appdata/roaming/microsoft/windows/start menu/programs/startup/",
)


# ── Class B: Git internals (confirmable) ─────────────────────────────

_GIT_TOKENS: tuple[str, ...] = ("/.git/",)


# ── Class C: Secrets (toggleable) ────────────────────────────────────

_SECRETS_TOKENS: tuple[str, ...] = (
    "/.ssh/",
    "/.aws/",
    "/.gcp/",
    "/.azure/",
    "/.kube/",
    "/.docker/",
)

# Doctrine 2026-05-29 (Empire triage, clean-VPS Gate 2b): every secrets-
# filename pattern is compiled with re.IGNORECASE. Filesystems differ on
# case-sensitivity (Linux/POSIX: sensitive by default, Windows/macOS-HFS+:
# insensitive), and an attacker or careless operator can author the same
# secret with an uppercase extension (`.ENV`, `creds.PEM`, `id_rsa.PUB`)
# and slip past a case-sensitive classifier on a Linux VPS. The classifier
# is the single gate every protected-deletion path consults — losing it
# to a case-variant is the confused-deputy pattern this PR family is
# closing. test_doctrine_fuzz::test_classify_deletion_flags_protected_
# case_insensitive pins the contract; the lowercase-the-input alternative
# was rejected because it splatters the normalization into every caller
# (path_trust_zone + governed_deletion + checkpoint_service all consume
# `normalized` for their OWN audit messages and want the original case).
_SECRETS_FILENAME_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(^|[/-])\.env(\.[^/]*)?$", re.IGNORECASE),
    re.compile(r"(^|[/-])id_rsa(\.pub)?$", re.IGNORECASE),
    re.compile(r"(^|[/-])id_ed25519(\.pub)?$", re.IGNORECASE),
    re.compile(r"(^|[/-])id_ecdsa(\.pub)?$", re.IGNORECASE),
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.pfx$", re.IGNORECASE),
    re.compile(r"\.p12$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"credentials$", re.IGNORECASE),  # ~/.aws/credentials
)


@dataclass(frozen=True)
class PathClass:
    """Result of classifying a path."""

    classification: str
    reason: str  # human-readable why-this-class
    matched_pattern: str  # specific token/pattern that hit


def normalize_path(
    raw_path: str,
    *,
    project_root: Path | None = None,
    home_dir: Path | None = None,
) -> str:
    """Normalize a path string for classification matching.

    Steps:
      1. Strip surrounding whitespace and quotes
      2. Expand ~ and ~user (using home_dir override for tests)
      3. If relative, resolve against project_root (when given) or cwd
      4. Resolve symlinks (best-effort, swallow failures)
      5. Convert backslash separators to forward slash
      6. Lowercase entire path on Windows; preserve case on POSIX

    Returns the normalized form. On any catastrophic failure
    (typically I/O during symlink resolve) returns the input
    minimally normalized (separator + case only) so classification
    can still proceed best-effort.

    The classifier is then designed to match against lowercased
    forward-slash form on Windows and original-case forward-slash on
    POSIX; the FIXED token lists below are written in lowercase
    forward-slash format.
    """
    if not raw_path:
        return ""
    p = raw_path.strip()
    # Strip enclosing quotes
    if len(p) >= 2 and p[0] in ("'", '"') and p[-1] == p[0]:
        p = p[1:-1]
    # Tilde expansion. os.path.expanduser handles ~ and ~user; allow
    # caller to override home_dir for tests.
    if home_dir is not None and p.startswith("~"):
        rest = p[1:]
        # Strip leading separator after ~ if present
        if rest.startswith("/") or rest.startswith("\\"):
            rest = rest[1:]
        p = str(home_dir / rest) if rest else str(home_dir)
    else:
        try:
            p = os.path.expanduser(p)
        except Exception:
            pass
    # Resolve relative paths against project_root or cwd
    if not os.path.isabs(p):
        try:
            base = project_root if project_root is not None else Path.cwd()
            p = str(base / p)
        except Exception:
            pass
    # Resolve symlinks best-effort
    try:
        p = str(Path(p).resolve())
    except Exception:
        pass
    # Separator + case normalization
    p = p.replace("\\", "/")
    if os.name == "nt":
        p = p.lower()
    return p


def _matches_token(normalized: str, token: str) -> bool:
    """Token match: case-insensitive substring on the normalized path.

    Tokens are expected to be in lowercase forward-slash form. On
    POSIX, normalize_path preserves case (`/proj/.TRASH/x` stays as
    is); on Windows it lowercases. To get consistent behavior across
    platforms — and to defend against an agent / operator creating a
    case-variant directory (e.g. `.Trash/` on a case-sensitive Linux
    FS) — we lowercase BOTH sides at match time. This is a strict
    tightening of the previous version (which only matched lowercased
    paths). Fixed 2026-05-27 per the trash-containment gate-proof goal.
    """
    return token.lower() in normalized.lower()


def classify_path(
    raw_path: str,
    *,
    project_root: Path | None = None,
    home_dir: Path | None = None,
) -> PathClass:
    """Classify a path against the protected-paths taxonomy.

    Returns PathClass with classification ∈ {forbidden_aidocs,
    forbidden_host_harness, forbidden_persistence, confirmable_git,
    secrets_gated, unknown}.

    Order of precedence (first match wins):
      1. forbidden_aidocs   (gate-tamper has highest urgency)
      2. forbidden_host_harness
      3. forbidden_persistence
      4. confirmable_git
      5. secrets_gated
      6. unknown

    The order matters: if a path is BOTH inside .aidocs AND a
    persistence-shaped name (unlikely but possible), the AIDOCS
    classification wins. Forbidden trumps confirmable trumps gated.
    """
    if not raw_path:
        return PathClass(CLASS_UNKNOWN, "empty path", "")
    normalized = normalize_path(
        raw_path,
        project_root=project_root,
        home_dir=home_dir,
    )
    if not normalized:
        return PathClass(CLASS_UNKNOWN, "normalization produced empty", "")

    # 1. AIDOCS internals
    for token in _AIDOCS_PROJECT_TOKENS:
        if _matches_token(normalized, token):
            return PathClass(
                CLASS_FORBIDDEN_AIDOCS,
                f"AIDOCS internal directory: {token}",
                token,
            )
    for pattern in _AIDOCS_FILE_PATTERNS:
        if pattern.search(normalized):
            return PathClass(
                CLASS_FORBIDDEN_AIDOCS,
                f"AIDOCS sqlite/db file: {pattern.pattern}",
                pattern.pattern,
            )

    # 2. Host harness fixed list
    for token in _HOST_HARNESS_FIXED_DIRS:
        if _matches_token(normalized, token):
            return PathClass(
                CLASS_FORBIDDEN_HOST_HARNESS,
                f"host harness directory: {token}",
                token,
            )
    # Host harness narrow fallback. Excludes well-known cloud/cred
    # dotdirs (.aws/.azure/.gcp/.ssh/.kube/.docker) so paths like
    # ~/.aws/config fall through to secrets classification rather
    # than tripping the host-config leaf regex. The fallback is
    # designed to catch unknown AI-host config files; cloud-cred
    # dotdirs are unambiguously secrets territory.
    _CLOUD_CRED_DOTDIRS = (
        "/.aws/",
        "/.azure/",
        "/.gcp/",
        "/.ssh/",
        "/.kube/",
        "/.docker/",
    )
    in_cloud_cred_dir = any(d in normalized for d in _CLOUD_CRED_DOTDIRS)
    if not in_cloud_cred_dir:
        for pattern in _HOST_HARNESS_LEAF_PATTERNS:
            if pattern.search(normalized):
                return PathClass(
                    CLASS_FORBIDDEN_HOST_HARNESS,
                    f"host config leaf: {pattern.pattern}",
                    pattern.pattern,
                )

    # 3. Persistence mechanisms
    for token in _PERSISTENCE_TOKENS:
        if _matches_token(normalized, token):
            return PathClass(
                CLASS_FORBIDDEN_PERSISTENCE,
                f"persistence mechanism: {token}",
                token,
            )

    # 4. Git internals (confirmable)
    for token in _GIT_TOKENS:
        if _matches_token(normalized, token):
            return PathClass(
                CLASS_CONFIRMABLE_GIT,
                f"git internals: {token}",
                token,
            )

    # 5. Secrets (gated by setting)
    for token in _SECRETS_TOKENS:
        if _matches_token(normalized, token):
            return PathClass(
                CLASS_SECRETS_GATED,
                f"secrets directory: {token}",
                token,
            )
    for pattern in _SECRETS_FILENAME_PATTERNS:
        if pattern.search(normalized):
            return PathClass(
                CLASS_SECRETS_GATED,
                f"secret file: {pattern.pattern}",
                pattern.pattern,
            )

    return PathClass(CLASS_UNKNOWN, "no protected pattern matched", "")


def _fold_deterministic_literals(inline_code: str) -> str:
    """Materialize DETERMINISTIC string constructions into their literal form.

    WHY THIS EXISTS. The extractor below finds quoted path-looking substrings.
    A path built at runtime has no literal to find, so the plain spelling and a
    constructed spelling of the SAME path got DIFFERENT verdicts. Demonstrated
    live (r0a, 2026-09-20): a node one-liner reading ``.env`` with its strings
    built via ``String.fromCharCode`` passed the secrets gate, while the same
    query written plainly was correctly refused. The intent was surviving nested
    ssh/su quoting; the effect was evasion.

    THIS IS THE SAME BYPASS CLASS ``_flatten_path_compositions`` ALREADY CLOSES
    for ``Path.home() / 'A' / 'B'`` — deterministic construction of a literal
    the static pass could not see. Only the constructor differs. This fold runs
    BEFORE that one (r0b 51cd6cff-e7e) so a folded fragment can still take part
    in a composition, e.g. ``Path.home() / <constant-built '.aidocs'> / 'x'``.

    DELIBERATELY SMALL, AND NOT AN ARMS RACE. Only constant forms are folded:
    every operand must be a literal. No eval, no variable propagation, no
    arithmetic, no template strings, no escape-sequence interpretation. A
    dynamic construction — ``chr(base + 4)``, a name lookup, a decode of runtime
    input — is OUTSIDE what a static extractor can guarantee, and that limit is
    stated here rather than implied away: this raises the floor, it does not
    make the floor complete.

    It also invents no policy. It only changes which CANDIDATE the existing
    classifier sees; the secrets/AIDOCS/git classifications and the operator
    toggle are untouched.
    """
    if not inline_code:
        return inline_code

    def _safe_literal(text: str) -> str | None:
        """A synthetic single-quoted literal, or None when unrepresentable.

        BACKSLASH IS CANONICALIZED, NOT REFUSED (r0b b7c2d2be-551). Refusing to
        re-emit a folded string that contains ``\\`` was itself a Windows-shaped
        bypass: ``String.fromCharCode(67,58,92,...)`` deterministically builds
        ``C:\\...\\.ssh\\id_rsa``, and dropping it here meant the extractor never
        saw a path the DOWNSTREAM AUTHORITY ALREADY KNOWS HOW TO READ —
        ``normalize_path`` does exactly ``p.replace("\\\\", "/")`` before
        classifying. A folding layer must not throw away a shape the classifier
        normalizes anyway.

        This is separator canonicalization, NOT escape interpretation: the
        char-code result already IS the literal character sequence, so nothing
        is being decoded here. Quotes, newlines and control characters are still
        refused, because those cannot be re-emitted unambiguously for a regex
        extractor working on a single-quoted synthetic literal — and the purpose
        is path discovery, not becoming a language evaluator.

        NO ARBITRARY LENGTH CUTOFF (r0b 559ad39b-519). This used to abandon any
        folded result longer than 512 characters, which silently turned a fully
        deterministic protected path back into "unknown" while the PLAIN spelling
        of the same path still reached the classifier — breaking the one
        equivalence property this fold exists to establish, for no reason beyond
        the path's own length. It was never needed as a resource bound either:
        every approved fold is NON-AMPLIFYING with respect to its source text. A
        char-code expression is always longer than the string it materializes,
        and a literal concat's output is no longer than its input, so the inline
        body already bounds the work. A second invented ceiling only created a
        bypass.
        """
        if not text:
            return None
        text = text.replace("\\", "/")
        if any(ch in text for ch in ("'", '"', "\n", "\r")):
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            return None
        return "'" + text + "'"

    def _chars(codes: str, *, ceiling: int) -> str | None:
        """TOTAL BY CONSTRUCTION — it must never raise (r0b 559ad39b-519).

        THE CALLER FAILS OPEN, WHICH MAKES A THROW CATASTROPHIC, NOT MERELY
        UNTIDY. The judge seam wraps this whole extraction in
        ``try: ... except Exception: pass``, so ONE exception here suppresses the
        ENTIRE protected-path rule family for that inline body — AIDOCS, git,
        persistence and secrets classifications all silently stop being applied.
        That is a poison-prefix bypass: a huge but syntactically valid decimal
        literal in an EARLIER char-code expression makes the fold throw, and the
        later encoded protected read is then never classified at all.

        The concrete throw was ``int(part)``: CPython's integer-conversion guard
        raises ValueError above its max-str-digits limit rather than returning a
        value, and node accepts arbitrarily long numeric literals, so the
        attacker controls that text freely.

        SO THE MAGNITUDE IS DECIDED LEXICALLY, BEFORE ANY CONVERSION: ASCII
        digits only, leading zeroes trimmed, then a length-and-then-string
        comparison against ``str(ceiling)``. int() is reached only for a value
        already proven to be at most the tiny ceiling, and even that is guarded.
        ASCII-only is deliberate too: ``str.isdigit()`` accepts Unicode digits
        that the source languages would never parse as numeric literals, and
        materializing those into a synthetic path would be inventing a candidate
        the real interpreter could not produce.
        """
        limit = str(ceiling)
        out = []
        for part in codes.split(","):
            part = part.strip()
            if not part or not all("0" <= ch <= "9" for ch in part):
                return None
            trimmed = part.lstrip("0") or "0"
            if len(trimmed) > len(limit) or (
                len(trimmed) == len(limit) and trimmed > limit
            ):
                return None
            try:
                out.append(chr(int(trimmed)))
            except (ValueError, OverflowError):  # pragma: no cover - belt: see above
                return None
        return "".join(out) if out else None

    out = inline_code

    # JS: String.fromCharCode(46, 101, 110, 118) -> '.env'
    # IGNORECASE because heuristic_judge lowercases inline_code before calling
    # here — the same reason the composition folds below need it, learned from a
    # live probe that slipped through without it (2026-05-04).
    def _js(match: re.Match) -> str:
        # 0..65535: fromCharCode takes UTF-16 code units. Values above that are
        # not emulated rather than guessed at.
        text = _chars(match.group(1), ceiling=0xFFFF)
        return (_safe_literal(text) or match.group(0)) if text else match.group(0)

    out = re.sub(
        # [0-9], NOT \d: \d also matches Unicode digits, which the source
        # languages would not parse as numeric literals — materializing those
        # would invent a candidate the real interpreter could never produce.
        r"""String\.fromCharCode\(\s*([0-9]+(?:\s*,\s*[0-9]+)*)\s*\)""",
        _js,
        out,
        flags=re.IGNORECASE,
    )

    # Python: ''.join(map(chr, [46, 101, 110, 118])) -> '.env'
    def _join_map(match: re.Match) -> str:
        text = _chars(match.group(1), ceiling=0x10FFFF)
        return (_safe_literal(text) or match.group(0)) if text else match.group(0)

    out = re.sub(
        r"""['"]{2}\.join\(\s*map\(\s*chr\s*,\s*\[\s*([0-9\s,]+?)\s*\]\s*\)\s*\)""",
        _join_map,
        out,
        flags=re.IGNORECASE,
    )

    # Python: chr(46) + chr(101) + ... -> '.env'   (constant terms only)
    def _chr_chain(match: re.Match) -> str:
        codes = ",".join(
            re.findall(r"chr\(\s*([0-9]+)\s*\)", match.group(0), flags=re.IGNORECASE),
        )
        text = _chars(codes, ceiling=0x10FFFF)
        return (_safe_literal(text) or match.group(0)) if text else match.group(0)

    out = re.sub(
        r"""chr\(\s*[0-9]+\s*\)(?:\s*\+\s*chr\(\s*[0-9]+\s*\)\s*)+""",
        _chr_chain,
        out,
        flags=re.IGNORECASE,
    )

    # Language-neutral: '.en' + 'v' -> '.env'.
    #
    # ONE WHOLE CHAIN, ONE PASS — NOT A CAPPED REWRITE LOOP (r0b b7c2d2be-551).
    # This previously folded adjacent PAIRS and repeated up to 8 times. A fixed
    # normalization depth is itself an evasion boundary: a fully constant chain
    # padded with enough empty fragments ('' + '' + ... + '.env') stays PARTIALLY
    # folded once the cap is hit, so the exact form we agreed to fold had a
    # deterministic spelling the normalizer knowingly stopped short of resolving.
    # Matching the entire `+`-joined run and joining every fragment at once means
    # each accepted all-literal chain reaches its fixed point in a single step.
    # The bound is the INPUT and the materialized-length ceiling in
    # _safe_literal, never a magic number of rewrites.
    _chain = re.compile(
        r"""(['"])[^'"\\\n]*\1(?:\s*\+\s*(['"])[^'"\\\n]*\2)+""",
    )
    _frag = re.compile(r"""(['"])([^'"\\\n]*)\1""")

    def _join_chain(match: re.Match) -> str:
        text = "".join(f[1] for f in _frag.findall(match.group(0)))
        return _safe_literal(text) or match.group(0)

    return _chain.sub(_join_chain, out)


def _flatten_path_compositions(inline_code: str) -> str:
    """Collapse Python path-composition idioms into literal
    tilde-prefixed paths the extractor's regex can match.

    Closes the compound-path bypass class (Empire-rendered finding
    2026-05-04). Patterns recognized:
      - Path.home() / 'A' / 'B' ...     →  '~/A/B/...'
      - pathlib.Path.home() / 'A' ...   →  '~/A/...'
      - Path('~').expanduser() / 'A'    →  '~/A'
      - Path('~/A').expanduser()        →  '~/A'

    Heuristic — over-extraction is cheap (classify_path returns
    CLASS_UNKNOWN for non-protected paths); under-extraction is
    the bug we close.
    """
    if not inline_code:
        return inline_code

    def _join(match: re.Match) -> str:
        frags = re.findall(
            r"""['"]([^'"]+)['"]""",
            match.group(0),
        )
        if not frags:
            return match.group(0)
        return "'~/" + "/".join(frags) + "'"

    out = inline_code
    # Case-insensitive: heuristic_judge lowercases inline_code
    # before passing it here, so `Path.home()` arrives as
    # `path.home()`. Without IGNORECASE the regex never matches and
    # the bypass survives — caught by live Empire-probe 2026-05-04.
    out = re.sub(
        r"""(?:pathlib\.)?Path\.home\(\)\s*"""
        r"""(?:/\s*['"][^'"]+['"]\s*)+""",
        _join,
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"""(?:pathlib\.)?Path\(\s*['"]~['"]\s*\)"""
        r"""\.expanduser\(\)\s*"""
        r"""(?:/\s*['"][^'"]+['"]\s*)+""",
        _join,
        out,
        flags=re.IGNORECASE,
    )

    def _tilde_expand(match: re.Match) -> str:
        inner = re.search(
            r"""['"]([^'"]+)['"]""",
            match.group(0),
        )
        if not inner:
            return match.group(0)
        return "'" + inner.group(1) + "'"

    out = re.sub(
        r"""(?:pathlib\.)?Path\(\s*['"]~[^'"]*['"]\s*\)"""
        r"""\.expanduser\(\)""",
        _tilde_expand,
        out,
        flags=re.IGNORECASE,
    )
    return out


def extract_paths_from_inline_code(inline_code: str) -> list[str]:
    """Pull candidate path strings out of inline code (the body of
    `python -c "..."` / `node -e "..."` / `ruby -e "..."`).

    Heuristic — returns substrings between matching quotes that
    look like filesystem paths. Designed for breadth (catch what
    might be a path) over precision; classifier handles the
    "actually protected?" decision.

    Patterns extracted:
      - Single/double-quoted strings containing / or \\
      - Tilde-prefixed strings (even unquoted in some langs)
      - Path-composition idioms (Path.home() / 'X' / 'Y') —
        flattened to '~/X/Y' BEFORE regex extraction (closes the
        compound-path bypass class, Empire-rendered 2026-05-04).

    Returns the unique set of candidate paths, preserving the
    quotes-stripped form.
    """
    if not inline_code:
        return []
    # ORDER MATTERS (r0b 51cd6cff-e7e): materialize deterministic constructions
    # FIRST, so a constant-built fragment can still participate in a path
    # composition below, then flatten compositions, then extract.
    inline_code = _fold_deterministic_literals(inline_code)
    inline_code = _flatten_path_compositions(inline_code)
    candidates: list[str] = []
    # Quoted strings starting with absolute / tilde / drive-letter.
    # Back-ref enforces matched-quote-pair, simple "no inner
    # quote" content keeps the match tight.
    for m in re.finditer(
        r"""(['"])((?:~|/|[A-Za-z]:[\\/])[^'"]*)\1""",
        inline_code,
    ):
        candidates.append(m.group(2))
    # Quoted strings containing escaped backslashes (Windows in a
    # double-quoted JSON-style). Less common; left as a separate
    # pass for clarity.
    for m in re.finditer(
        r'"((?:~|[A-Za-z]:)\\\\[^"]*)"',
        inline_code,
    ):
        candidates.append(m.group(1).replace("\\\\", "/"))
    # Quoted relative paths or bare filenames. Catches cases like
    # '.aidocs/config.toml', '.env', '.git/HEAD',
    # 'aidocs_identity.sqlite3' where the script omits a leading
    # / or ~. Classifier normalizes against project_root and
    # decides if the candidate is protected; non-matches return
    # CLASS_UNKNOWN so over-extraction here is cheap.
    #
    # Back-ref enforces matched-quote-pair. Inner content forbids
    # any quote of either type — keeps the match tight and avoids
    # cross-quote garbage like matching from outer " through inner
    # ' boundaries. Tradeoff: paths containing quotes (rare in
    # inline-code paths) are missed; classifier sees only quote-
    # free path strings.
    for m in re.finditer(
        r"""(['"])([^'"\s]+)\1""",
        inline_code,
    ):
        cand = m.group(2)
        # Skip empty
        if not cand:
            continue
        # Skip if already matched in earlier passes
        if cand.startswith(("~", "/")) or (len(cand) >= 2 and cand[1] == ":"):
            continue
        # Skip strings that clearly aren't paths (no separator AND
        # no extension dot AND no leading dot — e.g. plain words,
        # SQL fragments, etc.). The "has dot OR slash OR backslash"
        # heuristic catches filenames like aidocs_identity.sqlite3.
        if "/" not in cand and "\\" not in cand and "." not in cand:
            continue
        candidates.append(cand)
    # Unique while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out
