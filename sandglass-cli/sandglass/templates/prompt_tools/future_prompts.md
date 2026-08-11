<!--
QUEUED PROMPTS — one `====`-delimited block per deliverable.

Blocks below this comment run top-to-bottom, unattended, sharing one warm
session — so a block can see what earlier blocks did, but it is still a
separate task and is told so. Write each one as a standalone job: state what
it should do rather than assuming it will infer that from the block above.

  ┌── optional front matter, then a blank line ──┐
  model: sonnet          opus | sonnet | haiku, or a full model ID
  effort: low            low | medium | high | xhigh | max
  isolate: true          run with no memory of earlier blocks (rare)
  └──────────────────────────────────────────────┘

Writing a good block:
  · One block, one deliverable. "And then also" means it's two blocks.
  · Name the files it touches in `backticks` — `sandglass queue lint` checks
    they exist, and the next reader can find the work without grepping.
  · State the acceptance check: "done when X passes and Y appears".
  · Declare dependencies: "assumes block 3 created the repository class".
  · Don't restate architecture or current state — CLAUDE.md and the injected
    <project_state> brief already carry those into every run.
  · Prefer "change X to do Y" over "investigate X". Open-ended exploration is
    the most expensive block shape and the least likely to end in a diff.

Pick the cheapest model and effort that can actually do the job. Most blocks
are not `max`; a lot of them are `sonnet` at `medium`, and small mechanical
ones are `haiku` at `low`.

Run `sandglass queue lint` before a batch — it's free and catches missing
paths, empty blocks and bad effort values before they cost a run.

Completed blocks are cut out of this file and into prompt_history.md, so what
remains below is always exactly what's still pending.
-->
