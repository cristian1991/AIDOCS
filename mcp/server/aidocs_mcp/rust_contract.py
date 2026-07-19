"""Everything-migrates-to-Rust contract harness (aidocs-doctrine §XXIX).

One contract, many tongues. A §XXIX contract test is DRIVER-PARAMETRIZED:

  * the ``python`` driver ALWAYS runs — it proves the seam's contract holds for
    today's implementation;
  * a ``rust`` driver runs the SAME contract against a built Rust artifact WHEN
    one exists — and is simply NOT GENERATED when it doesn't. An absent Rust
    implementation therefore produces NO test case: no failure, no xfail, no
    "non-existent rust implementation" red. The tests WAIT; they never fail.

As Rust modules land, the existing contract tests light up against them
automatically — zero test rewriting. No full adapter and no stubs are needed to
start: this thin driver-lookup (``[python]`` now, ``[python, rust]`` once built)
is the entire base.

Usage in a contract test (BEHAVIOR/INVARIANT/REFUSAL-SHAPE assertions only —
never Python internals):

    import pytest
    from aidocs_mcp.rust_contract import contract_drivers

    @pytest.mark.rust_contract
    @pytest.mark.parametrize(
        "driver",
        contract_drivers("deploy_gate", python=_run_python_gate, rust=_make_rust_gate),
        ids=lambda d: d.name,
    )
    def test_gate_refuses_before_ship_on_blocking_failure(driver):
        result = driver(scenario="blocking_failure")   # normalized ContractResult
        assert result.refused_before_ship is True

``python=`` is the callable that drives the seam in Python and returns the
normalized result. ``rust=`` (optional) is a factory ``(binary_path) -> callable``
that shells the Rust binary and normalizes to the SAME result shape; when omitted
a default subprocess driver is used once a binary is present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Driver:
    """A named implementation of a seam under a §XXIX contract test."""

    name: str  # "python" | "rust"
    run: Callable

    def __call__(self, *args, **kwargs):
        return self.run(*args, **kwargs)


def _rust_target_dir() -> Path:
    """Well-known local Rust build output root (cargo). Absent on a pure-Python
    box, which is exactly why an unbuilt seam yields no rust driver."""
    # <repo>/rust/target/release/<seam>[.exe]; this module lives at
    # <repo>/mcp/server/aidocs_mcp/rust_contract.py -> parents[3] == <repo>.
    return Path(__file__).resolve().parents[3] / "rust" / "target" / "release"


def rust_binary(seam: str) -> str | None:
    """Path to the built Rust binary for ``seam``, or None if it is not built yet.

    Resolution order (fail-soft, None on any miss):
      1. ``AIDOCS_RUST_<SEAM>`` env var — an explicit path to the binary.
      2. the well-known cargo release dir ``<repo>/rust/target/release/<seam>``.

    None is the normal, non-error state on a pure-Python checkout: the rust
    driver is then never generated, so no contract test can fail for its absence.
    """
    seam = str(seam or "").strip()
    if not seam:
        return None
    env = os.environ.get(f"AIDOCS_RUST_{seam.upper()}", "").strip()
    if env and Path(env).exists():
        return env
    base = _rust_target_dir()
    for cand in (base / seam, base / f"{seam}.exe"):
        try:
            if cand.exists():
                return str(cand)
        except OSError:
            continue
    return None


def contract_drivers(
    seam: str,
    *,
    python: Callable,
    rust: "Callable[[str], Callable] | None" = None,
) -> "list[Driver]":
    """Drivers to parametrize a §XXIX contract test over.

    ALWAYS returns the ``python`` driver. Adds a ``rust`` driver ONLY when BOTH
    hold: the seam's Rust binary exists (``rust_binary(seam) is not None``) AND the
    test supplied a ``rust=`` factory that knows how to drive it. Otherwise the rust
    case is never generated — nothing can fail for a missing/undriveable impl.

    Why the factory is required (not a generic subprocess default): a seam's Rust
    form is invoked differently per seam (CLI exit-code vs FFI call vs socket), and
    the driver must normalize to the SAME result the python driver returns. The
    generic harness does driver SELECTION only; each seam's contract test owns the
    one-time ``rust=`` glue. Written once, it then auto-activates when the binary
    appears — zero per-run rewriting, and no shell path baked into the shipped
    package (doctrine §XXI).

    ``python``: the callable that drives the seam in Python.
    ``rust``:   optional factory ``(binary_path) -> callable`` normalizing to the
                same result shape; omit it to keep the seam python-only for now.
    """
    drivers = [Driver("python", python)]
    if rust is not None:
        bin_path = rust_binary(seam)
        if bin_path is not None:
            drivers.append(Driver("rust", rust(bin_path)))
    return drivers
