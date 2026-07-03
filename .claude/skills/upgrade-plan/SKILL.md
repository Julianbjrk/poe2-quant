---
name: upgrade-plan
description: Turn a substantial coding goal (correctness pass, feature program, migration, subsystem overhaul) into a phased, execution-ready plan that any model or future session can carry out with minimal error risk — evidence-cited tasks, House Rules, per-task contracts with pre-written test assertions and DO-NOTs. Use when asked to "make a plan", "do a full pass on X", "plan this so a lesser model can implement it", or before starting multi-day work.
---

# Upgrade Plan — spend all the judgment once, so execution needs none

Produce a plan so precise that a weaker model, a fresh session, or a tired human
can execute it faithfully. The plan is the program; the executor is the
interpreter. When invoked with a goal (`/upgrade-plan <goal>`), do §1 against
that goal, write the plan document, then report the phase map and the first
decision that belongs to the user.

## 1 · Evidence before planning
- Read every code path you intend to change. Run things: simulate the math,
  replay the data, reproduce the complaint. Measure — never plan from assumption.
- Every task must open with a **Why** backed by evidence (a measured number, a
  failing case, a field observation). A task whose premise cannot be evidenced
  yet goes to the evidence-gated phase with a named precondition — or gets cut.

## 2 · The plan document

Write `<NAME>_PLAN.md` at repo root with this shape:

### House Rules (top of file — every task inherits these)
State once the invariants a rushed executor forgets:
- Full test suite before AND after every task; a task is not done until green.
- One task = one commit; bump the app version every commit; the commit message
  carries the rationale (the repo history becomes the lab notebook).
- The project's sacred invariant(s), spelled out — e.g. "every displayed
  probability must be the measured frequency of exactly the graded event;
  diagnostics never size anything."
- Migration-sensitive constants (schema versions, model tags): exactly when
  they may change and which single task flips them.

### Phases — the order IS the safety mechanism
- **Phase 0 — additive + diagnostic.** Cannot change behavior; starts recording
  the evidence later phases will need.
- **Phase 1 — breaking changes, batched.** Everything that redefines behavior
  lands behind ONE migration point, flipped in the phase's final task. Never
  dribble breaking changes across releases.
- **Phase 2 — new capability on existing rails.** Additive again; new features
  ride the machinery Phases 0–1 hardened.
- **Phase 3 — evidence-gated.** Each task states a measurable precondition
  ("≥50 graded samples", "the sweep shows the constant is off-optimum").
  Explicit deferral with a trigger is a decision, not procrastination.

Pause for the user at any irreversible boundary (resets, migrations, data
loss) — those calls are theirs, not the plan's.

### Per-task contract (identical template, every task)
- **Why** — the motivating evidence, so an executor can detect when reality
  contradicts the plan.
- **Files** — the exact files touched.
- **Steps** — numbered. Paste exact code wherever a mistake is *likely* (math,
  migrations, fiddly merges); prose is fine for the safe parts.
- **Tests** — concrete assertions written BEFORE implementation (function,
  input, expected value). Acceptance the executor cannot rationalize around.
- **Do NOT** — the pre-mortem. Name the specific tempting mistake ("do not flip
  MODEL_V here — Task 11 does", "diagnostic only — must never feed sizing").

## 3 · Execution discipline
- One task at a time, in order. Green suite → commit → next. Never batch tasks
  into one commit.
- **Deviation protocol:** when evidence contradicts a task's premise (a
  simulation disagrees, an invariant breaks, a fixture was fit to the old bug),
  verify empirically, ship only the validated part, and document the deviation
  prominently in the commit. The plan is authoritative until the evidence says
  otherwise — then the evidence wins, loudly.
- Version every commit and keep the plan file untouched during execution; it is
  the reference, not a scratchpad.
- The plan doubles as cross-session memory: context compaction, model switches,
  and handoffs all survive because the state lives in the document and the
  commit trail — not in anyone's head.
