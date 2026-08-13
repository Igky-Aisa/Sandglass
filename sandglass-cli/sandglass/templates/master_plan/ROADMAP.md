# <PROJECT> — ROADMAP

> **Companion to the architecture doc.** Phases, and what each one must prove.
> **Rule**: a phase is not "done" because the code runs. It is done when every item in its
> **Definition of Done** is demonstrably true, verified by a stated command or observation.
> **Rule**: no phase may break an earlier phase's DoD. Every DoD is a permanent regression
> contract.

---

## PHASE OVERVIEW

| # | Phase | Core outcome | Est. | Hard dependency |
|---|---|---|---|---|
| **F1** | <name> | <the one thing that is true when it ships> | 2–3 w | — |
| **F2** | <name> | <…> | 1–2 w | F1 |

<!-- PHASE-ESTIMATES
P1: 2-3
P2: 1-2
-->

**The comment block above is read by `prompt_tools/count_blocks.py`** to produce estimate C in the
dashboard's FORECAST panel — the plan's own bottom-up guess, as a counterweight to measured
velocity. Format is `P<n>: <low>-<high>` in weeks, or `P<n>: <weeks>` for a single figure. Keep it
in step with the table above; it is deliberately a separate machine-readable block, because a
parser that scrapes a prose table breaks the first time someone reformats the table, and does it
silently. Delete the block if you do not want estimate C — it is then simply not offered, which is
better than a fabricated date.

---

## PHASE 1 — <NAME>

### Objective
<What this phase proves, in one or two sentences. Not a task list.>

### Scope
- <the capabilities in this phase>

### Exact deliverables
```
<paths that must exist when the phase ships>
```

### Verifiable milestones
1. <an observation someone else can repeat and get the same answer>

### Definition of Done
- [ ] <a command with an expected result, or a stated observation>
- [ ] <…>

**Write these so they can FAIL.** "Works correctly", "is robust", "looks good" are not DoD items —
if you cannot state how it fails, the phase is not ready to be planned. A DoD that can only be
checked by the person who wrote the code is not a DoD.

### Risks & mitigations
| Risk | Mitigation |
|---|---|
| <what could make this phase slip or ship broken> | <what you will do about it> |
