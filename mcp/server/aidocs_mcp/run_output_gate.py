"""Pass/fail-aware rate-limit gate for ai_run_output polling.

Decision rules (given elapsed_ms since spawn and aggregated stats):

  - confidence "none" → always allow. We have no data to gate on.
  - confidence "medium" → softer thresholds (0.5× min-floor) so the
    gate doesn't over-block on inferred data.
  - confidence "high" → tight thresholds (0.8× min-floor).

The min-floor is the earliest ANY outcome (pass / fail / timeout) was
ever observed finishing. If the run's elapsed time is below that,
there's no useful output to read — we refuse the poll and tell the
agent the earliest check-again time.

The warn-hung threshold is 1.5× max-observed. Past that, the run may
be truly stuck; the gate still ALLOWS the poll but annotates the
response with `likely_hung=True` and the observed max.

Gate never blocks:
  - when exact bucket has <MIN_SAMPLES_FOR_GATE samples AND parent
    chain has no aggregated data either (confidence=none).
  - when wait_seconds == 0 (the caller is doing a cheap tail-fetch,
    not a poll-blocking wait).
  - when the run is already done (the gate is about "is it done yet"
    polling; allow every output read once we know the answer).
"""

from __future__ import annotations

from typing import Any

# Threshold multipliers per confidence tier. Floor multiplier gates
# the block decision; ceiling multiplier flags likely-hung.
_CONFIDENCE_PROFILES = {
    "high": {"floor_mult": 0.8, "ceil_mult": 1.5},
    "medium": {"floor_mult": 0.5, "ceil_mult": 2.0},
    "none": {"floor_mult": 0.0, "ceil_mult": 0.0},
}


def evaluate_poll(
    elapsed_ms: int,
    stats_resolution: dict[str, Any],
    wait_seconds: float,
    run_is_done: bool,
) -> dict[str, Any]:
    """Decide whether a ai_run_output poll should be allowed.

    Args:
      elapsed_ms: how long the run has been alive.
      stats_resolution: output of `RunDurationBucketStore.resolve_stats`.
      wait_seconds: the caller's requested blocking wait. 0 = cheap
        tail-only read, bypasses the gate.
      run_is_done: if the caller already knows the run finished, the
        gate is moot — always allow.

    Returns a dict with:
      decision: "allow" | "block" | "allow_warn_hung"
      reason: str (human message)
      next_check_in_ms: int (only present on block)
      source_key: str (which bucket's stats informed the decision)
      confidence: "none" | "medium" | "high"
      likely_hung: bool

    """
    confidence = stats_resolution.get("confidence", "none")
    source_key = stats_resolution.get("source_key", "")
    stats = stats_resolution.get("stats", {}) or {}

    if run_is_done:
        return {
            "decision": "allow",
            "reason": "run finished",
            "source_key": source_key,
            "confidence": confidence,
            "likely_hung": False,
        }

    if wait_seconds <= 0:
        return {
            "decision": "allow",
            "reason": "non-blocking tail read",
            "source_key": source_key,
            "confidence": confidence,
            "likely_hung": False,
        }

    if confidence == "none" or not stats:
        return {
            "decision": "allow",
            "reason": ("no duration history for this bucket — allowing poll so we can learn"),
            "source_key": source_key,
            "confidence": confidence,
            "likely_hung": False,
        }

    profile = _CONFIDENCE_PROFILES[confidence]
    floor_mult = float(profile["floor_mult"])
    ceil_mult = float(profile["ceil_mult"])

    # Min-floor: earliest any outcome ever finished. Prefer the fail
    # row if it's faster (usually true); otherwise pass-min.
    candidate_mins = [
        int(row["min_ms"])
        for row in stats.values()
        if int(row.get("samples", 0)) > 0 and int(row.get("min_ms", 0)) > 0
    ]
    candidate_maxes = [
        int(row["max_ms"])
        for row in stats.values()
        if int(row.get("samples", 0)) > 0 and int(row.get("max_ms", 0)) > 0
    ]
    if not candidate_mins or not candidate_maxes:
        return {
            "decision": "allow",
            "reason": "stats present but incomplete — allowing",
            "source_key": source_key,
            "confidence": confidence,
            "likely_hung": False,
        }

    floor_ms = int(min(candidate_mins) * floor_mult)
    ceil_ms = int(max(candidate_maxes) * ceil_mult)

    if elapsed_ms < floor_ms:
        next_check_in_ms = floor_ms - elapsed_ms
        fastest_outcome, fastest_row = min(
            ((o, r) for o, r in stats.items() if int(r.get("samples", 0)) > 0),
            key=lambda or_: int(or_[1].get("min_ms", 10**9)),
            default=("unknown", {}),
        )
        slowest_outcome, slowest_row = max(
            ((o, r) for o, r in stats.items() if int(r.get("samples", 0)) > 0),
            key=lambda or_: int(or_[1].get("ewma_ms", 0)),
            default=("unknown", {}),
        )
        reason = (
            f"bucket '{source_key}' ({confidence} confidence): earliest "
            f"observed finish is {int(min(candidate_mins))}ms "
            f"(outcome={fastest_outcome}, samples="
            f"{int(fastest_row.get('samples', 0))}); you polled at "
            f"{elapsed_ms}ms. Check again in ~{next_check_in_ms}ms. "
        )
        if slowest_outcome != fastest_outcome and slowest_row:
            reason += (
                f"If the run is heading toward {slowest_outcome}, "
                f"ewma is {int(slowest_row.get('ewma_ms', 0))}ms."
            )
        return {
            "decision": "block",
            "reason": reason.strip(),
            "next_check_in_ms": next_check_in_ms,
            "source_key": source_key,
            "confidence": confidence,
            "likely_hung": False,
        }

    likely_hung = elapsed_ms > ceil_ms
    if likely_hung:
        return {
            "decision": "allow_warn_hung",
            "reason": (
                f"elapsed {elapsed_ms}ms exceeds bucket '{source_key}' "
                f"ceiling ({int(max(candidate_maxes))}ms × "
                f"{ceil_mult:.1f}); run may be hung"
            ),
            "source_key": source_key,
            "confidence": confidence,
            "likely_hung": True,
        }

    return {
        "decision": "allow",
        "reason": (
            f"elapsed {elapsed_ms}ms inside observed window for bucket "
            f"'{source_key}' ({confidence} confidence)"
        ),
        "source_key": source_key,
        "confidence": confidence,
        "likely_hung": False,
    }
