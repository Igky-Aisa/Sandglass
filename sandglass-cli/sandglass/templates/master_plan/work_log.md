# Work Log

Newest entry at the bottom. One entry per completed task, in the format below.

**Keep entries short — target ~20 lines, hard ceiling 40.** Every agent that
comes after you reads this file, and it never shrinks, so length here is a cost
charged to every future session. Never paste code, diffs, or command output:
name the file and function and let the reader open it. Write the decision and
its reason — the thing that isn't already visible in `git log` or the diff.

Run `sandglass rotate-logs` once this passes ~10 entries; it archives the older
ones to `master_plan/archive/` and leaves a pointer behind.

```markdown
## [Date] - [Agent Name] - [Task Title]

### 1. Context Snapshot
- **Goal**: [1-sentence goal]
- **State**: [Current branch/module/file focus]
- **Previous Blocker**: [What was resolved or remains]

### 2. Work Done
- [Key decisions and why — one bullet per decision, not per file]

### 3. Next Steps (For the next agent)
- [Specific point of resumption: a file and a question, not "continue"]
```
