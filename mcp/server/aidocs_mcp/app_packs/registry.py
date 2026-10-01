"""Install-wide application-pack registry (RFC 0003 S2).

Keyed by ``(pack_id, contract_digest)`` (§5.2 pin, §6.2 digest). Every act
fails closed: an unknown key, a duplicate install, a sealed-v1 claim whose
digest is not the operator-pinned one, and any DEMO / NON-v1 bundle (an M1
internal/demo encoding, never a sealed v1 format) claiming the sealed-v1
identity (build plan "Gates": bundle identity) are refused.

This registry is NOT a namespace authority. §3.3: namespace ownership is
unique WITHIN ONE TOOLSPACE, a collision fails at BINDING ACTIVATION (S7),
and reuse across tenants and toolspaces is allowed; so packs and versions
sharing a namespace all install here.

Installation is a control-plane act (§5.1); this module is the in-process
authority only and exposes no AI-callable surface.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .bundle import PackBundle, PlatformCaps, load_bundle

__all__ = ["PackRegistry", "RegistryEntry", "RegistryRefused"]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RegistryRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code + (f": {detail}" if detail else ""))


@dataclass(frozen=True)
class RegistryEntry:
    pack: PackBundle
    sealed_v1: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.pack.pack_id, self.pack.contract_digest)


class PackRegistry:
    """``sealed_v1`` pins, per pack_id, the contract digest(s) that may be
    registered as the sealed v1 pack. It is operator input, never derived.

    A pack may pin SEVERAL digests (operator 2026-10-01): during an in-place
    contract upgrade the binding's current contract and the one it moves to
    are both installed, each still exactly pinned."""

    def __init__(
        self,
        sealed_v1: Mapping[str, str | Iterable[str]] | None = None,
        *,
        caps: PlatformCaps | None = None,
    ) -> None:
        pins: dict[str, frozenset[str]] = {}
        for pack_id, value in dict(sealed_v1 or {}).items():
            digests = [value] if isinstance(value, str) else value
            try:
                digests = frozenset(digests)
            except TypeError:
                raise ValueError(f"invalid sealed-v1 pin {pack_id!r} -> {value!r}") from None
            if (
                not isinstance(pack_id, str)
                or not pack_id
                or not digests
                or not all(isinstance(d, str) and _HEX64.match(d) for d in digests)
            ):
                raise ValueError(f"invalid sealed-v1 pin {pack_id!r} -> {value!r}")
            pins[pack_id] = digests
        self._sealed = MappingProxyType(pins)
        self._caps = PlatformCaps() if caps is None else caps  # load_bundle fails closed on bad caps
        self._entries: dict[tuple[str, str], RegistryEntry] = {}
        self._lock = threading.Lock()

    def install(self, data: Any, *, as_sealed_v1: bool = False) -> RegistryEntry:
        pack = load_bundle(data, caps=self._caps)  # BundleRefused propagates
        if as_sealed_v1:
            if pack.is_demo:
                raise RegistryRefused("demo_not_sealed_v1", pack.pack_id)
            if pack.contract_digest not in self._sealed.get(pack.pack_id, frozenset()):
                raise RegistryRefused("sealed_digest_mismatch", pack.pack_id)
        entry = RegistryEntry(pack=pack, sealed_v1=as_sealed_v1)
        with self._lock:
            if entry.key in self._entries:
                raise RegistryRefused("pack_duplicate", f"{entry.key[0]}@{entry.key[1]}")
            # No namespace check here: §3.3 uniqueness is per toolspace and is
            # enforced at binding activation (S7), never install-wide.
            self._entries[entry.key] = entry
        return entry

    def get(self, pack_id: str, contract_digest: str) -> RegistryEntry:
        with self._lock:
            entry = self._entries.get((pack_id, contract_digest))
        if entry is None:
            raise RegistryRefused("pack_unknown", f"{pack_id}@{contract_digest}")
        return entry

    def sealed_v1(self, pack_id: str) -> RegistryEntry:
        """THE sealed pack for ``pack_id`` -- only while exactly one is
        installed. Two (an upgrade in flight) is ambiguous: callers must then
        resolve by (pack_id, contract_digest) via :meth:`get`."""
        with self._lock:
            found = [e for e in self._entries.values() if e.sealed_v1 and e.pack.pack_id == pack_id]
        if not found:
            raise RegistryRefused("pack_unknown", f"{pack_id} (sealed v1)")
        if len(found) > 1:
            raise RegistryRefused("pack_ambiguous", f"{pack_id} has {len(found)} sealed contracts")
        return found[0]

    def entries(self) -> tuple[RegistryEntry, ...]:
        with self._lock:
            return tuple(self._entries.values())
