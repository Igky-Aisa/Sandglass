# Archived entries from prompt_history.md

Moved here by `sandglass rotate-logs` on 2026-08-11. Newest of these entries is immediately followed, chronologically, by the oldest entry still in the live file.

## [Sandglass work] add a rule in Claude.md that allows to check progress frome…

**Executed:** 2026-08-09 — claude-opus-4-8 (via `sandglass execute`)

```
============================

add a rule in Claude.md that allows to check progress frome the main idea in master_plan\MASTER_ARQ_SYSTEM_MAP.md
, gives a an idea of how many prompt has been done and how many remaining in prompt_tools\future_prompts.md
the progress has to be updated in master_plan\Progress.md

and save all that in the folders and file that "sandglass new-claude-project" creates

============================
```

## log quota-interruption marker in prompt_history.md (rewind/keep-history/reset-waiting)

**Executed:** 2026-07-23 — sonnet (via `interactive chat (future prompts)`)

```
============================

if the work stop cus it hit a token limit, the rewind the code until the last promt, and keet the prompt in prompt history and reset the waiting mode, so then when the token have been refresh it starts from cero from the las prompt.

============================
```

## ntfy.sh notifications for quota-wait/resume/complete/stop lifecycle events

**Executed:** 2026-07-23 — sonnet (via `interactive chat (future prompts)`)

```
============================

set the sandglass to send a ntfy when starts working beacause token are been refresh, when finish fully, when finish partially and it waits till the next refresh.

============================
```

## route prompt_history.md correctly, verify + fix [Sandglass work] labeling accuracy

**Executed:** 2026-07-23 — sonnet (via `interactive chat (future prompts)`)

```
============================

model:sonnet
i made a mistake in the initial reques and intead of leaving the promts log in history_prompts.md it should be in prompt_history.md as the normal chat does it , but label it as Sandglass work please.

[NOTE: the destination (prompt_history.md) and "[Sandglass work]" labeling were already correct from an earlier headless run. But checking this prompt properly surfaced a real bug: prepend_to_history() unconditionally hardcoded the "[Sandglass work]"/"via sandglass execute" tag regardless of caller -- so every time *I* (interactive chat) called it directly to process a future_prompts.md queue myself, my own entries got mislabeled as headless sandglass execute runs, which is exactly the distinction CLAUDE.md says must stay clear. Fixed: prepend_to_history() now takes via=/label= keyword params (defaulting to the correct sandglass execute labeling, since that is its one real caller in execution_engine.py); this entry and the two immediately below it in this file are now correctly labeled as interactive-chat entries instead.]

============================
```

## hourglass 7-9 lines with rounded caps, and the missing )*( neck

**Executed:** 2026-07-23 — sonnet (via `interactive chat (future prompts)`)

```
============================

model:sonnet
i like the hourglass created and the animation , can you make 7  to 9 lines , you keep what already exist, maybe in the middle you could add ")*(", but close the top and the bottom as rounded that is posible.

[NOTE: initially misjudged this as fully already done from an earlier headless run (line count + rounded caps were), but that earlier run's own work_log entry explicitly said "drain/fill/neck-flicker logic untouched" -- the ")*(" neck motif this prompt also asked for had never actually been implemented. Caught before finalizing and fixed in this same pass: the neck now renders as ")*(" / ").(" (parens as the pinched waist, flickering grain between them) instead of a single "*"/".". Corrected this entry accordingly rather than leaving the original wrong "already done, skipped" note standing.]

============================
```

## add a 2-minute safety delay between quota refresh and retry

**Executed:** 2026-07-23 — sonnet (via `interactive chat (future prompts)`)

```
============================

model: sonnet 
could you add a delay of 2 minutes between the refresh quota and the prompt insertion to add a safe gap, in order to avoid errors.

============================
```

## [Sandglass work] i like the hourglass created and the animation , can you ma…

**Executed:** 2026-07-23 — claude-opus-4-8 (via `sandglass execute`)

```
============================

model:sonnet
i like the hourglass created and the animation , can you make 7  to 9 lines , you keep what already exist, maybe in the middle you could add ")*(", but close the top and the bottom as rounded that is posible.

============================
```

## add a short "what has been done" list to execute summary

**Executed:** 2026-07-23 — sonnet (via `sandglass execute`)

```
============================

 model: sonnet 

 when the script finish and gice the resumen of what has been done: 

 example:
 ✅ 0 prompt(s) executed
  ✖ 1 failed
  💰 0 tokens used
  ⏱️ 4.9 minutes
  📁 Responses saved to .quotashield\responses

  it should add a short list of what has been done like this:
  ✅ 2 prompt(s) executed
  ✖ 1 failed
  💰 0 tokens used
  ⏱️ 4.9 minutes
  what has been done:
    - font size adapted to the box
    - button updated has been added
    - fuction schede
  📁 Responses saved to .quotashield\responses

============================
```

## change the name of the entire script to sandglass

**Executed:** 2026-07-23 — opus (ran on sonnet 5 — user opted to proceed without switching) (via `sandglass execute`)

```
============================

model: opus 

change the name of the entire script to "sandglass"
so the commands are 'sandglass execute' and the instalation everything 

============================
```

## coudl you fix the fact that quotashield execute...

**Executed:** 2026-07-23 — sonnet (via `quotashield execute`)

```
============================

model: sonnet

coudl you fix the fact that quotashield execute , seems to me that is only executing the first prompt in future_prompts.md and then stops with no reason, and not the all prompt list in the queue as it should be.

============================
```

## at the end  of user_manual.md write : "this is the end3 of …

**Executed:** 2026-07-22 — haiku (via `quotashield execute`)

```
============================

model: haiku

at the end  of user_manual.md write : "this is the end3 of user manual" 
 

============================
```

## at the end of user_manual.md write : "this is the end2 of u…

**Executed:** 2026-07-22 — sonnet (via `quotashield execute`)

```
============================

model: sonnet

at the end of user_manual.md write : "this is the end2 of user manual" 

============================
```
