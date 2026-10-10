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

__all__ = ["ManagedPackRegistry", "PackRegistry", "RegistryEntry", "RegistryRefused"]

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

    def _insert(self, pack: PackBundle, *, sealed_v1: bool) -> RegistryEntry:
        entry = RegistryEntry(pack=pack, sealed_v1=sealed_v1)
        with self._lock:
            if entry.key in self._entries:
                raise RegistryRefused("pack_duplicate", f"{entry.key[0]}@{entry.key[1]}")
            # No namespace check here: §3.3 uniqueness is per toolspace and is
            # enforced at binding activation (S7), never install-wide.
            self._entries[entry.key] = entry
        return entry

    def install(self, data: Any, *, as_sealed_v1: bool = False) -> RegistryEntry:
        pack = load_bundle(data, caps=self._caps)  # BundleRefused propagates
        if as_sealed_v1:
            if pack.is_demo:
                raise RegistryRefused("demo_not_sealed_v1", pack.pack_id)
            if pack.contract_digest not in self._sealed.get(pack.pack_id, frozenset()):
                raise RegistryRefused("sealed_digest_mismatch", pack.pack_id)
        return self._insert(pack, sealed_v1=as_sealed_v1)

    def _validate_managed_sealed(
        self,
        data: Any,
        *,
        expected_pack_id: str,
        expected_digest: str,
    ) -> PackBundle:
        """Re-validate one durable managed row before it can become live."""
        if type(data) is not bytes:
            raise RegistryRefused("managed_store_corrupt")
        try:
            pack = load_bundle(data, caps=self._caps)
        except Exception:
            raise RegistryRefused("managed_store_corrupt") from None
        if (
            pack.is_demo
            or pack.pack_id != expected_pack_id
            or pack.contract_digest != expected_digest
            or pack.canonical_bytes != data
        ):
            raise RegistryRefused("managed_store_corrupt")
        return pack

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


class ManagedPackRegistry:
    """Hot-reloading view over an append-only managed version store.

    A legacy env registry may be supplied during migration. Managed rows are
    loaded into that same underlying registry object, so existing tenant
    runtimes observe a newly imported sealed version on their next lookup.
    """

    def __init__(self, store: Any, *, legacy_registry: PackRegistry | None = None) -> None:
        self._store = store
        self._registry = legacy_registry or PackRegistry()
        self._refresh_lock = threading.RLock()
        self._generation = -1
        self._managed_keys: set[tuple[str, str]] = set()

    @staticmethod
    def _store_error(code: str = "managed_store_unavailable") -> RegistryRefused:
        return RegistryRefused(code)

    def _refresh(self) -> None:
        with self._refresh_lock:
            try:
                generation = self._store.generation()
            except Exception:
                raise self._store_error() from None
            if type(generation) is not int or generation < 0:
                raise self._store_error("managed_store_corrupt")
            if self._generation >= 0 and generation < self._generation:
                raise self._store_error("managed_store_corrupt")
            if generation == self._generation:
                return
            try:
                rows = tuple(self._store.versions())
            except Exception:
                raise self._store_error() from None

            validated: list[tuple[Any, PackBundle, RegistryEntry | None]] = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                try:
                    pack_id = row.pack_id
                    digest = row.contract_digest
                    data = row.bundle_bytes
                except Exception:
                    raise self._store_error("managed_store_corrupt") from None
                key = (pack_id, digest)
                if key in seen:
                    raise self._store_error("managed_store_corrupt")
                seen.add(key)
                pack = self._registry._validate_managed_sealed(
                    data, expected_pack_id=pack_id, expected_digest=digest
                )
                try:
                    existing = self._registry.get(pack_id, digest)
                except RegistryRefused as exc:
                    if exc.code != "pack_unknown":
                        raise
                    existing = None
                if existing is not None and (
                    existing.sealed_v1 is not True
                    or existing.pack.canonical_bytes != data
                ):
                    raise self._store_error("managed_store_corrupt")
                validated.append((row, pack, existing))

            # Managed authority is append-only. If a previously loaded managed
            # key disappears, do not continue serving stale in-memory authority.
            if not self._managed_keys.issubset(seen):
                raise self._store_error("managed_store_corrupt")

            # Validate the complete snapshot before installing any missing row.
            for row, pack, existing in validated:
                if existing is not None:
                    continue
                try:
                    self._registry._insert(pack, sealed_v1=True)
                except RegistryRefused as exc:
                    if exc.code != "pack_duplicate":
                        raise
                    current = self._registry.get(row.pack_id, row.contract_digest)
                    if (
                        current.sealed_v1 is not True
                        or current.pack.canonical_bytes != row.bundle_bytes
                    ):
                        raise self._store_error("managed_store_corrupt") from None

            self._managed_keys = seen
            self._generation = generation

    def install(self, data: Any, *, as_sealed_v1: bool = False) -> RegistryEntry:
        self._refresh()
        return self._registry.install(data, as_sealed_v1=as_sealed_v1)

    def get(self, pack_id: str, contract_digest: str) -> RegistryEntry:
        self._refresh()
        return self._registry.get(pack_id, contract_digest)

    def sealed_v1(self, pack_id: str) -> RegistryEntry:
        self._refresh()
        return self._registry.sealed_v1(pack_id)

    def entries(self) -> tuple[RegistryEntry, ...]:
        self._refresh()
        return self._registry.entries()
