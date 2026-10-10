"""NLP consumers — modules that turn an analyzed Doc into actionable
signals (grant emissions, rage flags, memory candidates, ...).

Each consumer has the same shape:
  def consume(prompt: str, service: NLPService, **kwargs) -> Result

The Result type is consumer-specific. Consumers return their cleanest
"no signal" value when service.analyze_substance returns None — they
do NOT raise. The application layer decides what an absent NLP layer
means for its workflow.

Empire doctrine 2026-05-12: NLP authorizes, access_gate enforces.
Consumers in this package ONLY emit intent signals (grant sets,
classification labels) — they NEVER touch file I/O or write policy.
"""

from __future__ import annotations
