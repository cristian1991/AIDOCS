"""Gate modules — each one is small, testable, and snapshot-based.

Each gate exposes a single function:

    evaluate(state: GateState) -> StepResult

`state` is the controller's snapshot at the start of this step
(TOCTOU rule). `StepResult` carries the verdict, reason, and any
audit events the gate wants emitted.

Adding a new gate:
  1. New module in this package.
  2. Append the gate name to CANONICAL_PIPELINE in controller.py.
  3. Register the gate instance in EnforcementController.__init__.
  4. Add fixtures in tests/fixtures/enforcement_scenarios/ covering
     the gate's verdict shape.

Gates land incrementally. Order is fixed (canonical pipeline);
gates that aren't yet implemented are SKIPPED with a trace entry,
not silently no-op. That keeps "not yet wired" auditable.
"""
