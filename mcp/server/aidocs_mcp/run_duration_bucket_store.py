"""Per-bucket observed-duration statistics for the ai_run_output rate-
limit gate.

One row per (bucket_key, outcome). Same SQLite DB as the rest of the
index (`.MEMORY/.index/aidocs.sqlite3`). Observations come from
`code_runner_detached._monitor_run` when a run transitions to done.
The gate reads aggregated pass/fail stats for a bucket + its parent
chain to decide whether a polling call is too soon.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

# EWMA coefficient. 0.3 means a new observation contributes 30%; older
# ewma carries 70%. Converges in ~5-10 samples while staying responsive
# to genuine shifts (suite got slower post-refactor).
_EWMA_ALPHA = 0.3

# Minimum samples per (bucket, outcome) row before the gate trusts that
# row's stats as authoritative. Below this, the gate falls back to
# parent-chain aggregation.
MIN_SAMPLES_FOR_GATE = 3


class RunDurationBucketStore(SQLiteIndexStoreBase):
    """sqlite-backed per-outcome duration stats for run buckets."""

    def init_db(self, project_root: Path) -> None:
        with self.session(project_root) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_duration_buckets (
                    bucket_key TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    min_ms INTEGER NOT NULL DEFAULT 0,
                    max_ms INTEGER NOT NULL DEFAULT 0,
                    ewma_ms INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT,
                    PRIMARY KEY (bucket_key, outcome)
                );
                CREATE INDEX IF NOT EXISTS idx_run_duration_buckets_prefix
                    ON run_duration_buckets (bucket_key);
                """,
            )

    def record_observation(
        self,
        project_root: Path,
        bucket_key: str,
        outcome: str,
        duration_ms: int,
    ) -> None:
        """Fold a finished run's duration into the (bucket, outcome) row.

        Outcome must be one of pass | fail | timeout. Unknown outcomes
        are rejected so we don't bias the gate with crashed-spawn noise.
        """
        if outcome not in ("pass", "fail", "timeout"):
            return
        if duration_ms < 0:
            return
        self.init_db(project_root)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with self.session(project_root) as conn:
            row = conn.execute(
                "SELECT sample_count, min_ms, max_ms, ewma_ms "
                "FROM run_duration_buckets "
                "WHERE bucket_key = ? AND outcome = ?",
                (bucket_key, outcome),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO run_duration_buckets "
                    "(bucket_key, outcome, sample_count, min_ms, max_ms, "
                    " ewma_ms, last_seen_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (bucket_key, outcome, duration_ms, duration_ms, duration_ms, now_iso),
                )
                return
            sample_count = int(row["sample_count"]) + 1
            new_min = min(int(row["min_ms"]), duration_ms)
            new_max = max(int(row["max_ms"]), duration_ms)
            prev_ewma = int(row["ewma_ms"])
            if prev_ewma <= 0:
                new_ewma = duration_ms
            else:
                new_ewma = int(_EWMA_ALPHA * duration_ms + (1 - _EWMA_ALPHA) * prev_ewma)
            conn.execute(
                "UPDATE run_duration_buckets SET "
                "sample_count = ?, min_ms = ?, max_ms = ?, ewma_ms = ?, "
                "last_seen_at = ? "
                "WHERE bucket_key = ? AND outcome = ?",
                (sample_count, new_min, new_max, new_ewma, now_iso, bucket_key, outcome),
            )

    def get_exact_stats(
        self,
        project_root: Path,
        bucket_key: str,
    ) -> dict[str, dict[str, int]]:
        """Read the per-outcome stats for a single bucket_key.

        Returns `{outcome: {samples, min_ms, max_ms, ewma_ms}}`.
        Outcomes with zero samples are omitted. Empty dict when no
        row exists.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT outcome, sample_count, min_ms, max_ms, ewma_ms "
                "FROM run_duration_buckets WHERE bucket_key = ?",
                (bucket_key,),
            ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out[str(row["outcome"])] = {
                "samples": int(row["sample_count"]),
                "min_ms": int(row["min_ms"]),
                "max_ms": int(row["max_ms"]),
                "ewma_ms": int(row["ewma_ms"]),
            }
        return out

    def get_aggregated_stats(
        self,
        project_root: Path,
        bucket_prefix: str,
    ) -> dict[str, dict[str, int]]:
        """Aggregate per-outcome stats across all bucket_keys that
        start with `bucket_prefix`. Used for parent-chain fallback
        when the exact bucket has <MIN_SAMPLES_FOR_GATE samples.

        Simple aggregation: sum samples, min of mins, max of maxes,
        sample-weighted mean of ewmas. Weighted mean prevents a
        single-sample sibling from dominating the parent's ewma.
        """
        self.init_db(project_root)
        with self.session(project_root) as conn:
            rows = conn.execute(
                "SELECT outcome, sample_count, min_ms, max_ms, ewma_ms "
                "FROM run_duration_buckets "
                "WHERE bucket_key = ? OR bucket_key LIKE ?",
                (bucket_prefix, bucket_prefix + "%"),
            ).fetchall()
        by_outcome: dict[str, dict[str, int]] = {}
        by_outcome_ewma_num: dict[str, float] = {}
        by_outcome_ewma_den: dict[str, int] = {}
        for row in rows:
            out = str(row["outcome"])
            samples = int(row["sample_count"])
            if samples <= 0:
                continue
            slot = by_outcome.setdefault(
                out,
                {"samples": 0, "min_ms": 0, "max_ms": 0, "ewma_ms": 0},
            )
            if slot["samples"] == 0:
                slot["min_ms"] = int(row["min_ms"])
                slot["max_ms"] = int(row["max_ms"])
            else:
                slot["min_ms"] = min(slot["min_ms"], int(row["min_ms"]))
                slot["max_ms"] = max(slot["max_ms"], int(row["max_ms"]))
            slot["samples"] += samples
            by_outcome_ewma_num[out] = (
                by_outcome_ewma_num.get(out, 0.0) + float(int(row["ewma_ms"])) * samples
            )
            by_outcome_ewma_den[out] = by_outcome_ewma_den.get(out, 0) + samples
        for out, slot in by_outcome.items():
            den = by_outcome_ewma_den.get(out, 0)
            if den > 0:
                slot["ewma_ms"] = int(by_outcome_ewma_num[out] / den)
        return by_outcome

    def resolve_stats(
        self,
        project_root: Path,
        bucket_key: str,
        parent_chain: list[str],
    ) -> dict[str, Any]:
        """Find the best stats source for a bucket.

        Rules:
          1. Exact bucket with ≥MIN_SAMPLES_FOR_GATE total samples →
             use exact, confidence="high".
          2. Walk parent_chain; first parent whose aggregated stats
             total ≥MIN_SAMPLES_FOR_GATE samples → use aggregated,
             confidence="medium".
          3. No data anywhere → confidence="none", stats empty.

        Returns:
          {
            "confidence": "high" | "medium" | "none",
            "source_key": str,    # the key whose stats we used
            "stats": {outcome: {samples, min_ms, max_ms, ewma_ms}},
          }

        """
        exact = self.get_exact_stats(project_root, bucket_key)
        total_exact = sum(s["samples"] for s in exact.values())
        if total_exact >= MIN_SAMPLES_FOR_GATE:
            return {
                "confidence": "high",
                "source_key": bucket_key,
                "stats": exact,
            }
        for parent in parent_chain:
            agg = self.get_aggregated_stats(project_root, parent)
            total = sum(s["samples"] for s in agg.values())
            if total >= MIN_SAMPLES_FOR_GATE:
                return {
                    "confidence": "medium",
                    "source_key": parent,
                    "stats": agg,
                }
        return {
            "confidence": "none",
            "source_key": bucket_key,
            "stats": exact,
        }
