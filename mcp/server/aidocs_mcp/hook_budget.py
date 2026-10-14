"""ONE source of truth for the hook round-trip budget (#489).

WHY THIS MODULE EXISTS. The budget was written down twice and drifted:

    hook_broker_client.evaluate_via_broker_with_reason  total_timeout = 2.0
    hook_broker.HookBroker                              _CONN_TIMEOUT_S = 5.0

So a request taking 2-5s was ABANDONED BY THE CLIENT while the broker kept
computing an answer nobody would ever read — wasted work on the hot path, and
an operator-visible `timed_out` whose cause looked like a broker fault. Two
numbers describing one deadline will always drift; there is now one.

DELIBERATELY STDLIB-ONLY AND IMPORT-FREE. The thin client's whole purpose is a
cheap cold start (#332), so this module must never grow an import of anything
in the package. It holds constants and two pure helpers, nothing else.

THE GRACE IS NOT A LONGER COMPUTE WINDOW. ``BROKER_CONN_TIMEOUT_S`` exceeds the
client budget only to cover socket read/write of a request the client is still
sending. It must never be read as licence to compute past the client's
deadline — that is what ``client_deadline_passed`` exists to prevent.
"""

from __future__ import annotations

import time

# The client's hard deadline for a full broker round trip. This is the
# ENFORCEMENT-hook budget: PreToolUse and friends fire on EVERY tool call, so a
# hung broker must cost close to nothing there — and on failure they fall back
# to the full local evaluator, which enforces correctly anyway. Keep it tight.
HOOK_ROUNDTRIP_BUDGET_S = 2.0

# UserPromptSubmit is different in kind and needs its own budget (#489).
#
# MEASURED 2026-07-26, authenticated, live broker: a real UPS evaluation (NLP +
# memory + doctrine + palace surfacing) takes 3.510s / 3.981s / 4.103s. Against
# the 2.0s enforcement budget EVERY prompt timed out, so the operator paid the
# full 2s AND got a degraded banner — strictly worse than waiting for the real
# answer. Fires once per prompt, not once per tool call, so the latency is
# affordable where the enforcement budget's is not.
#
# 10.0s (operator directive 2026-07-26) — ~2.5x the worst measurement, with
# headroom for a cold cache or a loaded box, still far under the host's 30s hook
# budget. This is a CEILING ON A KNOWN-SLOW PATH, NOT A TARGET.
#
# PROFILED CAUSE of the 4s (cProfile, UserPromptSubmit, 2026-07-26): the memory
# surfacer runs the FULL spaCy pipeline ONCE PER CANDIDATE KEYWORD —
# _keyword_lemma_in_prompt 1054 calls / 9.32s cum, aidocs_nlp analyze 1031
# calls, spacy Language.__call__ 1022 calls / 8.30s, plus lingua language
# detection 1031 calls. An N+1 over the keyword set, not an inherently
# expensive pipeline. Fix that and the budget can come back DOWN — do not treat
# this ceiling as the resting state.
#
# FIXED IN SOURCE 2026-08-01 (#688). Keyword lemmas are now computed ONCE when a
# memory route is registered and persisted in `keyword_lemmas` per (keyword,
# language) with the model that produced them; the prompt is parsed ONCE and the
# match is a set intersection. Reproduced and re-measured on 1346 live keywords:
#   lemma lane   9911 ms -> 1 ms      (identical hit set, real spaCy, 3 prompts)
#   invocations  1052    -> 1         (instrumented surfacing call, 1000 kws)
# The budget is DELIBERATELY LEFT AT 10.0 here: this file's own rule is that the
# ceiling is not a target, and lowering it is a separate decision that wants a
# post-deploy measurement of the whole round trip, not just this lane.
UPS_ROUNDTRIP_BUDGET_S = 10.0

_UPS_EVENT = "UserPromptSubmit"

# How long a UserPromptSubmit may WAIT for the project-wide prompt-submit
# transaction lock (prompt_submit_service.PromptSubmitTransactionLock) before
# giving up and running degraded (#489).
#
# MEASURED 2026-07-30, cProfile of ONE live UserPromptSubmit evaluation:
#     prompt_submit_service.py:229 acquire   10.017s of a 13.329s evaluation
# because PromptSubmitService.lock_timeout_seconds defaulted to a hard-coded
# 10.0 — the SAME number as UPS_ROUNDTRIP_BUDGET_S. One contended acquire
# therefore spent the operator's ENTIRE round-trip budget waiting, and the answer
# it eventually produced was delivered to a client that had already printed
# "warm hook broker did not answer (reason: timed_out) [degraded after
# ~10000ms]". Two numbers describing one deadline, again.
#
# A WAIT LONGER THAN A FRACTION OF THE BUDGET CANNOT SUCCEED: winning the lock at
# 9.9s of a 10s budget leaves no time to do the work, so the reply is discarded
# either way. Losing the race is already cheap and CORRECT — _SubmitTransaction.
# try_capture releases the lock, mutation stages fail CLOSED and advisory stages
# proceed degraded — so a short wait weakens no authority; it only stops the
# prompt from paying for a wait it cannot use.
#
# A QUARTER of the round trip: enough for a healthy holder (whose hold is now
# milliseconds, not seconds, after the #489 execution_events index fix) to finish
# and hand over, while leaving 7.5s to actually evaluate the prompt.
PROMPT_SUBMIT_LOCK_BUDGET_S = UPS_ROUNDTRIP_BUDGET_S / 4.0


def budget_for_event(event_name: object) -> float:
    """Client round-trip budget for a hook event.

    UserPromptSubmit gets the wider UPS budget; everything else gets the tight
    enforcement budget. An unknown or missing event name gets the TIGHT one —
    fail toward low latency, since an enforcement hook that waits too long is a
    worse outcome than a prompt that degrades.
    """
    if str(event_name or "").strip() == _UPS_EVENT:
        return UPS_ROUNDTRIP_BUDGET_S
    return HOOK_ROUNDTRIP_BUDGET_S

# Connect phase only. Loopback connect is sub-millisecond when the listener is
# healthy; a longer wait means nothing usable is behind the registration, and
# failing fast keeps `timed_out` meaning "reachable but did not answer".
HOOK_CONNECT_TIMEOUT_S = 0.05

# Socket-level timeout on the BROKER side. Grace over the client budget for
# read/write only — NOT extra compute time.
#
# It must cover the WIDEST client budget, not the tightest: the broker cannot
# know which event a connection carries until it has read the request, and
# timing out at 2.5s while a UPS client waits 8s would resurrect the original
# defect with the roles reversed (client still waiting, broker already gone).
_BROKER_READ_GRACE_S = 0.5
MAX_CLIENT_BUDGET_S = max(HOOK_ROUNDTRIP_BUDGET_S, UPS_ROUNDTRIP_BUDGET_S)
BROKER_CONN_TIMEOUT_S = MAX_CLIENT_BUDGET_S + _BROKER_READ_GRACE_S


def now_ms() -> int:
    """Wall-clock milliseconds since the epoch.

    Wall clock, not monotonic, BECAUSE it crosses a process boundary: the
    client stamps a send time that the broker (a different process) compares
    against. Monotonic clocks are not comparable across processes.
    """
    return int(time.time() * 1000)


def client_budget_exhausted(sent_at_ms: object, budget_s: object) -> bool:
    """True when the client that sent this request has already given up.

    The broker calls this BEFORE the expensive evaluation so it never burns the
    full NLP/memory/doctrine/palace pipeline for a caller that stopped
    listening. Fails OPEN (returns False) on any malformed input: refusing to
    work because a timestamp was unparseable would turn a diagnostics field
    into an outage.
    """
    try:
        sent = int(sent_at_ms)  # type: ignore[arg-type]
        budget = float(budget_s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if sent <= 0 or budget <= 0:
        return False
    elapsed_s = (now_ms() - sent) / 1000.0
    # A clock skew that puts the send in the future must not read as expired.
    if elapsed_s < 0:
        return False
    return elapsed_s >= budget
