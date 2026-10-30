"""Privileged approval broker — the separate service that OWNS the
governed-shell signing key + pending + consumed state.

Empire re-seal 2026-05-30 (finish the broker boundary after 357524bd):

  * The connected broker peer is AUTHENTICATED after connect via OS
    credentials against ONE explicitly provisioned service principal
    ($AIDOCS_APPROVAL_BROKER_PRINCIPAL) — never "any foreign uid".
  * The endpoint path is rejected if it is a symlink, has an insecure
    (symlinked / group/other-writable) ancestor; post-connect peer-cred auth
    closes the residual TOCTOU window (a swapped path connects to a different
    server whose creds then fail the principal check).
  * The broker's state tree (dir/key/pending/consumed) is atomically created
    private (0700/0600) and its owner/mode authority is PROVEN at startup;
    a world/group-writable or non-owned tree refuses to serve.
  * verify_and_consume compares the BROKER-OWNED pending tuple (path+sha)
    against the consumed card — pending-record drift is refused.
  * TTL is capped server-side; connections are bounded + timed (stalled
    clients cannot wedge the server).
  * Windows ships a named-pipe service with TOKEN peer-auth (client SID via
    GetNamedPipeClientProcessId; server SID via the pipe owner) so Windows is
    no longer fail-closed-only.

No endpoint / no provisioned principal / unauthentic peer → None → approvals
fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import socket
import stat as _stat
import struct
import time
from pathlib import Path

_MAX_TTL = 600
_MAX_REQUEST = 1_000_000
_CONN_TIMEOUT = 10.0
_MAX_CONN_BYTES = _MAX_REQUEST


# ── Broker CORE (runs INSIDE the privileged service) ────────────────


class BrokerStateError(RuntimeError):
    """The broker state tree could not be proven private/owned → refuse."""


class LocalApprovalBroker:
    """The broker's owned state + logic, run by the privileged SERVICE. Its
    state tree is created atomically-private and its authority is proven at
    startup (refuse to serve a writable/non-owned tree)."""

    def __init__(self, state_dir: Path, *, service_sid: str | None = None):
        self._dir = Path(state_dir)
        self._posix = os.name != "nt"
        self._fd = self._pfd = self._cfd = None
        # The PROVISIONED service principal SID (Windows) used for the positive
        # DACL allowlist — explicit, not a process-shape assumption. Defaults to
        # the running principal when not provided.
        self._service_sid = service_sid
        dirs = (self._dir, self._dir / "pending", self._dir / "consumed")
        if self._posix:
            for d in dirs:
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._chmod(d, 0o700)  # mkdir mode is umask-masked
            # Prove the FULL ancestor chain (no symlink / group-other-write up
            # to root) BEFORE binding handles.
            if not _posix_chain_secure(str(self._dir)):
                raise BrokerStateError(f"{self._dir}: insecure ancestor chain")
            # HANDLE-BIND: pre-open no-follow directory fds. All subsequent
            # key/pending/consumed ops use openat/unlinkat via these fds, so an
            # ancestor rename-swap AFTER this point cannot redirect them (the fd
            # is bound to the directory INODE, not the path).
            dflags = os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            dflags |= getattr(os, "O_CLOEXEC", 0)
            try:
                self._fd = os.open(str(self._dir), dflags)
                self._pfd = os.open("pending", dflags, dir_fd=self._fd)
                self._cfd = os.open("consumed", dflags, dir_fd=self._fd)
            except OSError as exc:
                raise BrokerStateError(f"cannot bind state dir handles: {type(exc).__name__}")
        else:
            # Explicit DACL creation (service principal/SYSTEM/Admins only).
            from . import governed_shell_broker_win as _win

            for d in dirs:
                _win.create_private_dir(str(d))
        self._key = self._load_or_create_key()
        ok, why = self.verify_state_authority()
        if not ok:
            raise BrokerStateError(why)

    def __del__(self):
        for fd in (getattr(self, "_pfd", None), getattr(self, "_cfd", None), getattr(self, "_fd", None)):
            try:
                if fd is not None:
                    os.close(fd)
            except Exception:
                pass

    @staticmethod
    def _chmod(p: Path, mode: int) -> None:
        try:
            os.chmod(p, mode)
        except Exception:
            pass

    # ── handle-bound state primitives (POSIX: openat/unlinkat via dir fds;
    #    Windows: path-based under the DACL-protected tree) ──────────────
    def _create_excl(self, dirfd, name: str, win_path: Path, data: bytes) -> None:
        if self._posix:
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_NOFOLLOW, 0o600, dir_fd=dirfd)
        else:
            fd = os.open(str(win_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def _read_text(self, dirfd, name: str, win_path: Path) -> str:
        if self._posix:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dirfd)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                return fh.read()
        # Windows: refuse a reparse point (symlink/junction) at the leaf — no
        # redirect of an authority-sensitive read.
        from . import governed_shell_broker_win as _win

        if _win.is_reparse_point(str(win_path)):
            raise OSError("refusing reparse-point state file")
        return _read_nofollow(win_path)

    def _exists(self, dirfd, name: str, win_path: Path) -> bool:
        if self._posix:
            try:
                os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False
        return win_path.is_file()

    def _unlink(self, dirfd, name: str, win_path: Path) -> None:
        try:
            if self._posix:
                os.unlink(name, dir_fd=dirfd)
            else:
                win_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _list_pending(self):
        if self._posix:
            return sorted(os.listdir(self._pfd))
        return sorted(p.name for p in (self._dir / "pending").iterdir())

    def _load_or_create_key(self) -> bytes:
        name = "signing.key"
        win = self._dir / name
        if self._exists(self._fd, name, win):
            return bytes.fromhex(self._read_text(self._fd, name, win).strip())
        self._create_excl(self._fd, name, win, _secrets.token_hex(32).encode("utf-8"))
        if not self._posix:
            self._chmod(win, 0o600)
        return bytes.fromhex(self._read_text(self._fd, name, win).strip())

    def verify_state_authority(self) -> tuple[bool, str]:
        """PROVE the state tree is owned by THIS service principal and not
        writable by group/other. Fail closed."""
        entries = [
            self._dir,
            self._dir / "pending",
            self._dir / "consumed",
            self._dir / "signing.key",
        ]
        if os.name != "nt":
            euid = os.geteuid()
            for e in entries:
                try:
                    st = os.lstat(e)
                except OSError as exc:
                    return (False, f"{e}: cannot stat ({type(exc).__name__})")
                if _stat.S_ISLNK(st.st_mode):
                    return (False, f"{e}: is a symlink")
                if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
                    return (False, f"{e}: group/other-writable")
                if st.st_uid != euid:
                    return (False, f"{e}: not owned by the broker principal (uid {st.st_uid})")
            return (True, "broker state owned by the service principal, not group/other-writable")
        # Windows: (1) full-chain NO-REPARSE proof (a junction/symlink ancestor
        # cannot redirect the protected state tree); (2) POSITIVE per-entry DACL
        # proof — every write-granting ACE must name ONLY the PROVISIONED
        # service principal / SYSTEM / Administrators / OWNER / CREATOR OWNER.
        from . import governed_shell_broker_win as _win

        sid = self._service_sid or _win.current_user_sid()
        if not _win.win_state_chain_secure(str(self._dir), sid):
            return (False, f"{self._dir}: state ancestor chain is agent-writable / reparse-redirected")
        for e in entries:
            if not e.exists():
                return (False, f"{e}: missing")
            if not _win.acl_state_authority_ok(str(e), sid):
                return (False, f"{e}: DACL grants write to a non-provisioned trustee")
        return (True, "broker state: positive DACL (provisioned principal) + no-reparse chain")

    def _token(self, path: str, sha: str, nonce: str, expiry: int) -> str:
        msg = f"{path}|{sha}|{nonce}|{expiry}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def cleanup_expired(self, max_items: int = 200) -> int:
        """Bounded reaping of expired pending records + their consumed markers,
        so a flood of issued-but-never-consumed cards cannot grow state without
        limit. Scans at most `max_items` entries per call."""
        now = int(time.time())
        removed = 0
        try:
            names = self._list_pending()[:max_items]
        except Exception:
            return 0
        for name in names:
            if not name.endswith(".json"):
                continue
            stem = name[:-5]
            try:
                rec = json.loads(
                    self._read_text(self._pfd, name, self._dir / "pending" / name) or "{}"
                )
                if int(rec.get("expiry") or 0) < now:
                    self._unlink(self._pfd, name, self._dir / "pending" / name)
                    self._unlink(self._cfd, stem, self._dir / "consumed" / stem)
                    removed += 1
            except Exception:
                continue
        return removed

    def issue_card(self, provider_path: str, sha256: str, ttl_seconds: int) -> dict | None:
        self.cleanup_expired()  # bounded reaping on each issue
        ttl = max(1, min(int(ttl_seconds or _MAX_TTL), _MAX_TTL))  # SERVER-SIDE cap
        nonce = _secrets.token_hex(16)
        expiry = int(time.time()) + ttl
        rec = {"provider_path": provider_path, "sha256": sha256, "expiry": expiry}
        name = f"{nonce}.json"
        self._create_excl(
            self._pfd, name, self._dir / "pending" / name, json.dumps(rec).encode("utf-8")
        )
        return {
            "provider_path": provider_path,
            "sha256": sha256,
            "nonce": nonce,
            "issued_at": int(time.time()),
            "expiry": expiry,
            "token": self._token(provider_path, sha256, nonce, expiry),
        }

    def verify_and_consume(self, card: dict) -> tuple[bool, str]:
        path = str(card.get("provider_path") or "")
        sha = str(card.get("sha256") or "").lower()
        nonce = str(card.get("nonce") or "")
        token = str(card.get("token") or "")
        try:
            expiry = int(card.get("expiry") or 0)
        except Exception:
            return (False, "malformed expiry")
        if not (path and sha and nonce and token and expiry):
            return (False, "incomplete card")
        if not _nonce_ok(nonce):
            return (False, "malformed nonce")
        if not hmac.compare_digest(self._token(path, sha, nonce, expiry), token):
            return (False, "card signature invalid (not broker-issued)")
        if int(time.time()) > expiry:
            return (False, "card expired")
        pname = f"{nonce}.json"
        pwin = self._dir / "pending" / pname
        if not self._exists(self._pfd, pname, pwin):
            return (False, "unknown approval nonce")
        # Compare the FULL BROKER-OWNED pending tuple (path + sha + expiry)
        # against the card — refuse ANY drift, including expiry drift.
        try:
            rec = json.loads(self._read_text(self._pfd, pname, pwin) or "{}")
        except Exception:
            return (False, "unreadable pending record")
        try:
            rec_expiry = int(rec.get("expiry") or 0)
        except Exception:
            rec_expiry = -1
        if (
            str(rec.get("provider_path") or "") != path
            or str(rec.get("sha256") or "").lower() != sha
            or rec_expiry != expiry
        ):
            return (False, "pending-record drift (card does not match broker's record)")
        try:
            self._create_excl(self._cfd, nonce, self._dir / "consumed" / nonce, b"")
        except FileExistsError:
            return (False, "approval already consumed (replay refused)")
        except Exception as exc:
            return (False, f"could not claim approval: {type(exc).__name__}")
        return (True, "approval consumed")


def _nonce_ok(nonce: str) -> bool:
    """A nonce is a 32-hex token; reject anything else (path-traversal-safe)."""
    return len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce)


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _read_nofollow(path: Path) -> str:
    """Read a file WITHOUT following a final symlink (atomic no-follow)."""
    fd = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


# ── Wire protocol (length-prefixed JSON) ────────────────────────────


def _pack(obj: dict) -> bytes:
    data = json.dumps(obj).encode("utf-8")
    return struct.pack("!I", len(data)) + data


def _send(conn: socket.socket, obj: dict) -> None:
    conn.sendall(_pack(obj))


def _recv(conn: socket.socket) -> dict | None:
    head = _recv_n(conn, 4)
    if head is None:
        return None
    (n,) = struct.unpack("!I", head)
    if n <= 0 or n > _MAX_REQUEST:
        return None
    body = _recv_n(conn, n)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _recv_n(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(min(65536, n - len(buf)))
        if not chunk:
            return None
        buf += chunk
        if len(buf) > _MAX_CONN_BYTES:
            return None
    return buf


def _handle_dict(broker: LocalApprovalBroker, req: dict) -> dict:
    """Transport-agnostic op dispatch → response dict (reused by AF_UNIX and
    the Windows named-pipe transport)."""
    op = req.get("op")
    if op == "ping":
        return {"ok": True}
    if op == "issue_card":
        card = broker.issue_card(
            str(req.get("provider_path") or ""),
            str(req.get("sha256") or ""),
            int(req.get("ttl_seconds") or _MAX_TTL),
        )
        return {"ok": True, "card": card}
    if op == "verify_and_consume":
        ok, why = broker.verify_and_consume(req.get("card") or {})
        return {"ok": ok, "reason": why}
    return {"ok": False, "reason": f"unknown op {op!r}"}


def _handle(broker: LocalApprovalBroker, conn: socket.socket, req: dict) -> None:
    _send(conn, _handle_dict(broker, req))


# ── POSIX AF_UNIX server with SO_PEERCRED peer auth ─────────────────


def _peer_uid(conn: socket.socket) -> int | None:
    try:
        SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
        creds = conn.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except Exception:
        return None


def serve_unix(endpoint: str, broker: LocalApprovalBroker, allowed_uids, *, one_shot=False):
    """Serve over AF_UNIX. Each connection is AUTHENTICATED by SO_PEERCRED;
    only an allowed uid is served. Connections are timed (stalled clients are
    dropped) and request size is bounded."""
    try:
        os.unlink(endpoint)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(endpoint)
    try:
        os.chmod(endpoint, 0o600)
    except Exception:
        pass
    srv.listen(8)
    allowed = set(allowed_uids)
    try:
        while True:
            conn, _ = srv.accept()
            try:
                conn.settimeout(_CONN_TIMEOUT)
                uid = _peer_uid(conn)
                if uid is None or uid not in allowed:
                    _send(conn, {"ok": False, "reason": "peer not authorized"})
                    continue
                req = _recv(conn)
                if not isinstance(req, dict):
                    _send(conn, {"ok": False, "reason": "bad request"})
                    continue
                _handle(broker, conn, req)
            except (TimeoutError, OSError):
                pass
            finally:
                conn.close()
            if one_shot:
                break
    finally:
        srv.close()
        try:
            os.unlink(endpoint)
        except OSError:
            pass


def _posix_endpoint_secure(endpoint: str) -> bool:
    """Reject a symlinked endpoint or any symlinked / group-other-writable
    ancestor (insecure-ancestor / symlink TOCTOU pre-filter). Post-connect
    peer-cred auth is the real guarantee against a swap."""
    p = Path(endpoint)
    try:
        st = os.lstat(endpoint)
    except OSError:
        return False
    if _stat.S_ISLNK(st.st_mode):
        return False
    cur = p.parent
    while True:
        try:
            ast_ = os.lstat(cur)
        except OSError:
            return False
        if _stat.S_ISLNK(ast_.st_mode):
            return False
        if ast_.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
            return False
        if cur == cur.parent:
            break
        cur = cur.parent
    return True


class UnixBrokerClient:
    """ApprovalBroker proxy over AF_UNIX. After connect, AUTHENTICATES the
    SERVER peer (SO_PEERCRED → server uid) against the ONE provisioned service
    principal. A foreign-uid / impostor socket is refused."""

    def __init__(self, endpoint: str, principal_uid: int):
        self._endpoint = endpoint
        self._principal_uid = int(principal_uid)

    def _rpc(self, req: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(_CONN_TIMEOUT)
            c.connect(self._endpoint)
            # AUTHENTICATE the connected peer (server) by OS credential.
            server_uid = _peer_uid(c)
            if server_uid is None or server_uid != self._principal_uid:
                return {"ok": False, "reason": "broker peer is not the provisioned principal"}
            # The server closes the connection the instant it refuses an
            # unauthorized peer, so the send/recv here can race a closed pipe
            # (BrokenPipeError / ConnectionResetError → OSError). A dropped
            # connection IS a refusal — treat it as not-ok, never crash.
            try:
                _send(c, req)
                resp = _recv(c)
            except OSError as exc:
                return {"ok": False, "reason": f"broker connection dropped ({type(exc).__name__})"}
            return resp if isinstance(resp, dict) else {"ok": False, "reason": "no response"}

    def issue_card(self, provider_path: str, sha256: str, ttl_seconds: int) -> dict | None:
        resp = self._rpc(
            {
                "op": "issue_card",
                "provider_path": provider_path,
                "sha256": sha256,
                "ttl_seconds": ttl_seconds,
            }
        )
        return resp.get("card") if resp.get("ok") else None

    def verify_and_consume(self, card: dict) -> tuple[bool, str]:
        resp = self._rpc({"op": "verify_and_consume", "card": card})
        return (bool(resp.get("ok")), str(resp.get("reason") or ""))


# ── control-plane config source (NOT agent-mutable env authority) ───
# The broker endpoint + service principal are resolved from an OPERATOR-only
# installed config file, whose authority is the FILE's ownership/ACL — never
# an env var. $AIDOCS_BROKER_CONFIG may POINT at the file, but a forged
# pointer is worthless: the file (and its full ancestor chain) must prove
# operator ownership, or the config is ignored (→ fail closed).
_DEFAULT_CONFIG_POSIX = "/etc/aidocs/governed_shell_broker.json"
_CONFIG_PTR_ENV = "AIDOCS_BROKER_CONFIG"


def _config_path() -> str:
    if os.name == "nt":
        # OS-derived operator-controlled source (HKLM / known-folder), NOT the
        # mutable %ProgramData% / env path.
        from . import governed_shell_broker_win as _win

        return _win.installed_config_path() or ""
    # POSIX: a fixed operator-only path; an env POINTER is allowed but the
    # FILE's root-owned ancestry (not the env) is the authority.
    ptr = os.environ.get(_CONFIG_PTR_ENV, "").strip()
    return ptr or _DEFAULT_CONFIG_POSIX


def _posix_chain_secure(path: str, *, require_root_owner: bool = False) -> bool:
    """The path AND its full ancestor chain to the root: no symlink anywhere,
    no group/other write, and (for operator-only sources) root-owned."""
    cur = Path(path)
    while True:
        try:
            st = os.lstat(cur)
        except OSError:
            return False
        if _stat.S_ISLNK(st.st_mode):
            return False
        if st.st_mode & (_stat.S_IWGRP | _stat.S_IWOTH):
            # A world/group-writable ancestor is allowed ONLY if it carries the
            # sticky bit (the /tmp /var/tmp pattern: others cannot rename or
            # delete entries they do not own, so the chain still can't be
            # swapped). A non-sticky world/group-writable dir is refused.
            if not (st.st_mode & _stat.S_ISVTX):
                return False
        if require_root_owner and getattr(st, "st_uid", -1) != 0:
            return False
        if cur == cur.parent:
            break
        cur = cur.parent
    return True


def _config_authority_ok(path: str) -> bool:
    """Prove the config FILE is operator-installed (authority is ownership,
    not env). POSIX: root-owned, no symlink/group-other-write on the full
    chain. Windows: ACL-proven via the win helper."""
    if not os.path.exists(path):
        return False
    if os.name != "nt":
        return _posix_chain_secure(path, require_root_owner=True)
    try:
        from . import governed_shell_broker_win as _win

        # Full parent chain: no reparse point + agent-cannot-write any entry.
        return bool(_win.win_config_chain_secure(path))
    except Exception:
        return False


def load_broker_config():
    """Resolve {endpoint, principal} from the operator-only control-plane
    config, or None (→ fail closed) if it is missing / not authority-proven /
    malformed."""
    path = _config_path()
    if not _config_authority_ok(path):
        return None
    try:
        raw = _read_nofollow(Path(path)) if os.name != "nt" else Path(path).read_text("utf-8")
        cfg = json.loads(raw or "{}")
    except Exception:
        return None
    endpoint = str(cfg.get("endpoint") or "").strip()
    principal = str(cfg.get("principal") or "").strip()
    if not endpoint or not principal:
        return None
    return {"endpoint": endpoint, "principal": principal}


# ── connect (the AGENT side) ────────────────────────────────────────


def connect_broker():
    """Return an AUTHENTICATED privileged broker client, or None (→ approvals
    fail closed). Endpoint + principal come ONLY from the operator-only
    control-plane config; the peer's OS credentials are authenticated against
    that one provisioned principal after connect."""
    cfg = load_broker_config()
    if cfg is None:
        return None
    endpoint, principal = cfg["endpoint"], cfg["principal"]
    if os.name == "nt":
        return _connect_windows(endpoint, principal)
    # POSIX
    try:
        principal_uid = int(principal)
    except ValueError:
        return None
    if not os.path.exists(endpoint):
        return None
    if not _posix_endpoint_secure(endpoint):
        return None
    return UnixBrokerClient(endpoint, principal_uid)


# ── Windows named-pipe broker (token peer-auth) ─────────────────────
# Lazy ctypes — only touched on Windows. The module imports cleanly on POSIX.


def _connect_windows(endpoint: str, principal_sid: str):
    try:
        from . import governed_shell_broker_win as _win
    except Exception:
        return None
    return _win.connect(endpoint, principal_sid)


def serve_windows(pipe_name: str, broker: LocalApprovalBroker, allowed_sids, *, one_shot=False):
    from . import governed_shell_broker_win as _win

    return _win.serve(pipe_name, broker, allowed_sids, one_shot=one_shot)
