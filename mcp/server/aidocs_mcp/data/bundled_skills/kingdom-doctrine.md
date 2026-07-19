---
name: kingdom-doctrine
description: A portable quality-law SAMPLE for ANY project — KISS=Simply Smart, the 120% bar (correct, enforceable, deterministic), truth before green, the TDD cycle, failure stewardship, definition of done. Derived from the empire's portable core; fork it and make it YOUR project's law. Activates on "kingdom doctrine" / "quality law" / "how should I work here".
kind: sample
tags: sample, quality, kingdom, kiss, tdd, truth, 120, portable
---

# kingdom-doctrine

Your project is a kingdom. This scroll is a SAMPLE working law — portable, project-agnostic, derived from the empire's portable core, yours to adopt and fork. It is not a law tier of its own; it binds no one until you make it yours. It asks one thing: give your best on THIS project, provably.

## I. KISS = Keep It Simply Smart (+ Speedy)

Smart in foundation, simple at surface. Simple — fewest moving parts, a reader groks it in one pass. Smart — the right primitive, no clever-for-clever's-sake. Speedy — fast on the path the user actually feels.

Reach for complexity ONLY when it buys real UX, setup, speed — or a future-proof SEAM that turns tomorrow's feature into wiring, not a rebuild. Complexity must never buy elegance, cleverness, or looking impressive. Complexity is debt; it must pay rent. Build the seam, not the feature.

## II. The 120% bar — correct, enforceable, deterministic

**Correct** — the rule must encode truth, not what was easy to write. A wrong rule perfectly enforced is harm at scale. **Enforceable** — words alone are not law; only a test, a runtime check, or schema validation makes a rule binding. **Deterministic** — same input, same outcome, every time. All three must hold; any one missing breaks the other two.

Do not ask "did this pass?" Ask:

```text
Can this lie again?
Can this bypass again?
Can this silently degrade again?
Can a future contributor repeat the same class of mistake?
Can a user believe a stronger claim than the system can prove?
```

The war is not won when the bug dies. It is won when the battlefield has been redesigned so the next war is harder to lose.

## III. Truth before green

A green run is acceptable only if the report says what actually happened: which exact commit was tested, what scope ran and why, what was skipped, counts generated rather than hand-written. Unknown is not zero. The pattern to reject: green badge, unknown scope, skipped tests, no reason, claim "done."

```text
A green lie is worse than a red truth.
```

## IV. The TDD cycle — Red, Green, Adversarial, Regression Guard

Every behavior change earns its test in four beats, IN ORDER:

1. **RED** — write the failing test FIRST and watch it fail for the right reason. A test authored after the code, passing on first run, is a witness who already knows the verdict.
2. **GREEN** — the smallest change that makes RED pass. No extra scope.
3. **ADVERSARIAL** — add tests that try to BREAK it: null/empty/malformed, boundaries, wrong-type, the never-fall-back-to-permissive case. If you cannot imagine an adversarial case, you do not yet understand the surface.
4. **REGRESSION GUARD** — lock the class of bug at the cheapest gate that can catch it.

**Harden on edit:** when a change forces you to touch an existing test, the only legal direction is TIGHTER. Never loosen, delete, weaken, or skip an assertion to go green. If the contract genuinely moved, the replacement must be at least as strict, and the diff must say why.

```text
Green is the floor, not the finish.
```

## V. Failure stewardship — no orphaned failures

Every failure gets a cause (introduced by this change / pre-existing / exposed by this change / flaky / environment / unknown) and a duty (fix now / preserve baseline / quarantine with proof / escalate / waiver with owner and expiry). Every "pre-existing" or "flaky" claim needs proof. No report may say "not my bug" without classification and disposition.

```text
You may dispute parentage of a rebel.
You may not leave the rebel armed inside the walls.
```

## VI. Definition of done — honest classification

Work is done only when: the runtime path is wired; tests prove SUCCESS and REFUSAL; the report cannot lie; failure paths have named reasons; docs say exactly what is true now; and no future contributor can easily repeat the same class of bug.

Anything short of that gets an honest label — never inflate: PASS · PASS WITH POLISH · TACTICAL PASS (symptom fixed, class open) · FOUNDATION PASS (built, not wired) · DEPLOY-SAFE, NOT TRUTH-SEALED.

```text
A patch ends a symptom.
Law ends a family of symptoms.
Your kingdom deserves law.
```

## VII. Friction is the kingdom speaking

When the same gate, test, or reviewer refuses you a third time, treat the resistance as guidance — your shape is probably wrong. Don't bypass; reconsider. The third refusal earns more weight than the first.

## VIII. Evidence beats assertion

"It works" is nearly nothing. A command, its output, a count, a hash — that is evidence. Reports carry evidence; claims without evidence are labeled as claims. When uncertain, investigate; when evidence is incomplete, say so.

## When this activates

Say "kingdom doctrine", "quality law", "the working law", or ask "how should I work on this project" — and hold every change to this bar. This scroll is a portable sample: fork it, tighten it, make it YOUR kingdom's law.
