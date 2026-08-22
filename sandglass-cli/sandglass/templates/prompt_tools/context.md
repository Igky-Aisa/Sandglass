# Context — cold-start window

The cheapest useful read in this repo. If you arrived **cold** — a block routed
to a non-Claude model, a fresh CLI, a new machine — start here, not with
`work_log.md`. Maintenance rules live in `CLAUDE.md` / `AGENTS.md` under "the
cold-start window". **Newest entry first** in this file, which is the opposite of
`work_log.md` (newest-last): do not carry the habit across.

Open these only when this file isn't enough: `CLAUDE.md`/`AGENTS.md` (rules) ·
`master_plan/work_log.md` (last 2 entries) · `master_plan/Progress.md` (status).

---

## What this repo is

_One short paragraph: what the project is, the language/framework, where the
code lives, anything about the machine an agent would otherwise guess wrong._

## Where things are

- _`path/to/file` — what it owns, in one line._

## Invariants — violating one of these is a real bug

- _The rules that are expensive to rediscover. Security boundaries, ordering
  conventions, anything that has already caused an incident once._

## Now — _YYYY-MM-DD_

- _What is being worked on and why. What is deliberately not built yet._
- _Known gaps a cold agent would otherwise waste time discovering._

## Window — last 5 tasks, newest first

### _YYYY-MM-DD — task title_
_Four lines maximum. The decision and its reason, not the file list — git
already has the file list. Delete the sixth entry when you add one._
