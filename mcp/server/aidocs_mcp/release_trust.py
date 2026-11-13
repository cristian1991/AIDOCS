"""Signed-release root-of-trust for the AIDOCS law package.

This is the crypto basis that lets a NON-editable install become
*remote*-trustworthy WITHOUT a TOFU bypass or a manual override. It answers
exactly one question, fail-closed:

    Is the code this interpreter is about to run the SAME code that an
    authorized builder signed, by identity, with the pinned release key?

The mechanism (asymmetric, offline-verifiable):

  * The pinned Ed25519 PUBLIC key(s) are a constant in this module (#1253). The
    copy at ``trust/release_pubkey.pem`` still ships for older clients and is never
    read here. The PRIVATE key lives OUTSIDE the repo and never touches the server.
  * A release manifest ``trust/release_manifest.json`` records the artifact
    identity: version, git commit, builder, created_at, and the aggregate
    ``compute_package_fingerprint`` of the running code (which folds in every
    .py + bundled data file AND the version string).
  * A detached signature ``trust/release_manifest.json.sig`` is the builder's
    Ed25519 signature over the EXACT manifest bytes.

``verify_release`` is the gate. It returns ``ok=True`` ONLY when all of:
  1. the manifest and signature are present (no artifacts ⟹ unsigned floor),
  2. the signature verifies against the pinned public key over the manifest
     bytes (a tampered manifest OR a wrong/rotated key ⟹ fail),
  3. the manifest version matches the running ``__version__`` (a stale manifest
     from an older/newer build ⟹ fail),
  4. the freshly recomputed package fingerprint EQUALS the signed fingerprint (a
     single tampered byte in any law/data file after signing ⟹ fail).

Every failure path returns ``ok=False`` with a truthful ``reason`` — the
"manual-copy / unsigned / tampered / stale / wrong-key all stay refused" floor.
The trust artifacts themselves are excluded from the fingerprint (see
``_FP_EXCLUDE_DIRS`` containing ``trust``) so the manifest can certify the code
without certifying itself.

The ``cryptography`` Ed25519 primitive is imported LAZILY so importing this
module (and the whole gate) stays pure-stdlib until a verification is actually
requested.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import package_integrity as _pi

MANIFEST_SCHEMA = "aidocs-release-manifest-v1"
_MANIFEST_NAME = "release_manifest.json"
_SIG_NAME = "release_manifest.json.sig"

#: THE ROOT OF TRUST (#1253): raw 32-byte Ed25519 public keys, in the code that is
#: ALREADY INSTALLED AND TRUSTED, not in the tree being judged. This module used to
#: load the key from <pkg>/trust, so a tree carrying its own key, manifest and
#: signature verified as ok, and pull_release runs this on a DOWNLOADED tree.
#:
#: ROTATION IS A CODE CHANGE. Add the new key here, ship that, re-sign, then remove
#: the old one. Nothing in an artefact can add a key, and there is deliberately no
#: argument, environment variable or config override. Tests replace this attribute
#: directly, which no production path does.
#:
#: Must equal the key committed at trust/release_pubkey.pem, which still ships for
#: older clients; tests/security/test_release_key_pin_1253.py compares the two.
#:
#: An empty set would verify nothing: it fails closed rather than open.
#: Never served by the gate: caches and anything a running install writes beside its
#: code. Shared with compute_uncovered_files, because the signer must name exactly
#: what the gate serves.
RELEASE_EXCLUDE_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})
RELEASE_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log"})

PINNED_RELEASE_KEYS: frozenset[bytes] = frozenset(
    {bytes.fromhex("5c28ba7c215818935a479a96ddbb0d80f5f7f00d05f610b8a1cec4233143bb14")}
)


@dataclass(frozen=True)
class ReleaseTrust:
    """Outcome of a signed-release verification. ``ok`` is the only thing the
    trust law keys on; the rest is identity recorded into the audit trail.
    """

    ok: bool
    reason: str
    commit: str | None = None
    version: str | None = None
    builder: str | None = None
    fingerprint: str | None = None
    sig_fingerprint: str | None = None
    created_at: str | None = None
    build_number: int | None = None
    #: #913 -- the manifest's OPTIONAL ``vendored`` map: {tree name -> signed
    #: fingerprint}. None means the release PREDATES vendored coverage, which is
    #: not the same as "this release has no vendored trees" and must never be
    #: read as a pass. Additive on purpose: verify_release fail-closes on an
    #: unrecognised schema, so bumping MANIFEST_SCHEMA to carry this would
    #: invalidate every already-signed release at once and stop updates dead.
    vendored: dict | None = None
    #: #1253 -- the manifest's OPTIONAL digest of the release-root pyproject.toml,
    #: as signed. Additive for the same reason as ``vendored``. Whether it is
    #: REQUIRED is the installer's question (release_pull refuses without it), not
    #: this one's: a running gate on a legacy manifest must keep verifying.
    build_context: object = None
    #: #1253 -- the manifest's signed map of every shipped file the fingerprint skips:
    #: {release-relative path -> "sha256:<hex>"}. Optional here and required by the
    #: installer, for the same reason as ``build_context``.
    uncovered_files: object = None

    def as_audit(self) -> dict:
        return {
            "trust_basis": "signed_release" if self.ok else "none",
            "release_commit": self.commit,
            "release_version": self.version,
            "release_builder": self.builder,
            "release_fingerprint": self.fingerprint,
            "release_sig_fingerprint": self.sig_fingerprint,
            "release_build_number": self.build_number,
            "release_vendored": dict(self.vendored) if self.vendored else None,
        }


def _fail(reason: str) -> ReleaseTrust:
    return ReleaseTrust(ok=False, reason=reason)


def build_context_of(pyproject_bytes: bytes) -> str:
    """The signed digest of the EXACT pyproject.toml bytes pip will read (#1253)."""
    return "sha256:" + hashlib.sha256(pyproject_bytes).hexdigest()


def compute_uncovered_files(
    pkg_dir: Path | str, release_root: Path | str, claimed_trees
) -> dict[str, str]:
    """Sorted {release-relative path -> digest} of what ships outside the fingerprint.

    compute_package_fingerprint is a suffix list outside its payload trees, and that
    rule stays as it is because every signed release rests on it. So the files it
    skips are signed here, one digest each. They are not inert: the vendored engine's
    instructions_cli prints instructions/*.md as packaged guidance.

    The set is what _release_archive_bytes actually serves, with the same exclusions,
    minus trust/. Disagreeing with the gate would make every client refuse.
    """
    from .vendored_contract import VENDORED_TREES

    trees = [(Path(pkg_dir), "server/aidocs_mcp", True)]
    trees += [
        (Path(release_root) / rel, rel, False) for name, rel in VENDORED_TREES if name in claimed_trees
    ]
    out: dict[str, str] = {}
    for base, prefix, in_package in trees:
        for path in base.rglob("*"):
            rel = path.relative_to(base).as_posix()
            parts = rel.split("/")
            if (
                not path.is_file()
                or any(part in RELEASE_EXCLUDE_DIRS for part in parts)
                or path.suffix.lower() in RELEASE_EXCLUDE_SUFFIXES
                or (in_package and parts[0] == "trust")
                or _pi.fingerprint_covers(rel)
            ):
                continue
            out[f"{prefix}/{rel}"] = build_context_of(path.read_bytes())
    return dict(sorted(out.items()))


def release_pyproject_for(pkg_dir: Path | str) -> Path:
    """The pyproject.toml a package at <root>/server/aidocs_mcp is built from.

    ALWAYS THE PACKAGE'S GRANDPARENT, on both layouts: <stage>/mcp/pyproject.toml
    where the deploy signs and <release>/pyproject.toml where the gate serves, the
    second a byte copy of the first (deploy_aidocs_gate.sh, Gate 4). Do NOT use
    vendored_contract.resolve_release_root here. On the signing layout it answers
    <stage>, which holds third_party/ and no pyproject.toml.
    """
    return Path(pkg_dir).parents[1] / "pyproject.toml"


def verify_release(pkg_dir: Path | str | None = None) -> ReleaseTrust:
    """Verify the pinned signed release against the running code. Fail-closed:
    ANY missing artifact, signature failure, version mismatch, or fingerprint
    drift yields ``ok=False``. Never raises — a crypto/IO error is a refusal,
    not an exception that could be swallowed into a default-allow.
    """
    base = Path(pkg_dir) if pkg_dir else _pi.package_dir()
    td = base / "trust"
    man_p = td / _MANIFEST_NAME
    sig_p = td / _SIG_NAME

    if not (man_p.is_file() and sig_p.is_file()):
        return _fail("unsigned: release trust artifacts absent")

    try:
        man_bytes = man_p.read_bytes()
        sig_bytes = sig_p.read_bytes()
    except OSError as e:
        return _fail(f"unreadable trust artifact: {e}")

    # Detached sigs are emitted hex-encoded for git-friendliness; accept raw too.
    sig_raw = _decode_sig(sig_bytes)
    if sig_raw is None:
        return _fail("malformed signature encoding")

    # 1) Signature over the EXACT manifest bytes, against the keys pinned IN THIS
    # MODULE. Whatever key file the tree carries is never opened.
    keys = PINNED_RELEASE_KEYS
    if not keys:
        return _fail("no pinned release key: this build can verify nothing")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        for raw_key in keys:
            try:
                Ed25519PublicKey.from_public_bytes(raw_key).verify(sig_raw, man_bytes)
                break
            except InvalidSignature:
                continue
        else:
            return _fail("bad signature: manifest tampered or wrong/rotated key")
    except ImportError:
        return _fail("cryptography unavailable — cannot verify signed release")
    except Exception as e:  # malformed key constant, etc. — refuse, never allow
        return _fail(f"signature verification error: {e}")

    sig_fp = "sha256:" + hashlib.sha256(sig_raw).hexdigest()[:16]

    # 2) Parse the now-authenticated manifest.
    try:
        man = json.loads(man_bytes)
    except json.JSONDecodeError as e:
        return _fail(f"signed manifest is not valid JSON: {e}")
    if man.get("schema") != MANIFEST_SCHEMA:
        return _fail(f"unknown manifest schema {man.get('schema')!r}")

    m_ver = man.get("version")
    m_fp = man.get("fingerprint")
    commit = man.get("commit")
    builder = man.get("builder")
    created = man.get("created_at")
    m_bn = man.get("build_number") if isinstance(man.get("build_number"), int) else None

    # 3) Version must match the running package — a stale manifest fails closed.
    run_ver = _pi.package_version()
    if str(m_ver) != str(run_ver):
        return ReleaseTrust(
            ok=False,
            reason=f"stale manifest: signed version {m_ver!r} != running {run_ver!r}",
            commit=commit,
            version=m_ver,
            builder=builder,
            fingerprint=m_fp,
            sig_fingerprint=sig_fp,
            created_at=created,
        )

    # 4) Recompute the fingerprint of the RUNNING code and compare. The version
    # is folded into the fingerprint, so this also catches a spoofed version.
    # NO EXCLUDE (#925d). This digests the package EXACTLY as the signer does,
    # `_vendor` included — that tree is executable code inside the signed
    # envelope, and a verifier that skipped it would accept whatever engine an
    # artefact happened to carry. The one-commit `FP_SIGNATURE_EXCLUDE` that
    # stood here is described in package_integrity.
    cur = _pi.compute_package_fingerprint(base, version=run_ver)
    if cur["fingerprint"] != m_fp:
        return ReleaseTrust(
            ok=False,
            reason="tampered: running code fingerprint differs from signed manifest",
            commit=commit,
            version=m_ver,
            builder=builder,
            fingerprint=m_fp,
            sig_fingerprint=sig_fp,
            created_at=created,
        )

    return ReleaseTrust(
        ok=True,
        reason="signed release verified",
        commit=commit,
        version=m_ver,
        builder=builder,
        fingerprint=m_fp,
        sig_fingerprint=sig_fp,
        created_at=created,
        build_number=m_bn,
        # #913 -- carried ONLY from the AUTHENTICATED manifest, i.e. after the
        # signature check above. A vendored claim read before verification would
        # be attacker-controlled, which is the opposite of the point.
        vendored=man.get("vendored") if isinstance(man.get("vendored"), dict) else None,
        build_context=man.get("build_context"),
        uncovered_files=man.get("uncovered_files"),
    )


def _decode_sig(sig_bytes: bytes) -> bytes | None:
    """A detached Ed25519 signature is 64 raw bytes. We store it hex-encoded
    (128 ASCII hex chars, optionally newline-terminated) so it diffs cleanly in
    git; accept raw 64-byte form too for robustness.

    The raw 64-byte form is checked BEFORE any strip(): a raw signature can
    legitimately begin/end with a byte equal to ASCII whitespace (0x09/0x0a/0x20/
    …), and stripping it would corrupt the signature into a spurious 'malformed'.
    Only the hex form is whitespace-trimmed.
    """
    if len(sig_bytes) == 64:
        return sig_bytes
    s = sig_bytes.strip()
    if len(s) == 64:
        return s
    try:
        raw = bytes.fromhex(s.decode("ascii"))
        return raw if len(raw) == 64 else None
    except (ValueError, UnicodeDecodeError):
        return None
