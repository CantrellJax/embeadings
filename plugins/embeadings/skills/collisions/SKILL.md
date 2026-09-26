---
name: collisions
description: Find active Beads or Linear tasks that may touch the same files, symbols, modules, or genuine Git worktree edits. Use before implementation, during parallel agent work, or when checking whether active tasks can trip over each other.
---

# Review code-surface coordination

This is a read-only evidence review. Do not edit source, trackers, worktrees, dependencies, or branch
state while using it.

## Run from the right repository

1. Run `python <plugin-root>/scripts/run_embeadings.py check` using the plugin root in this skill's installed
   path.
2. Change to the implementation repository being coordinated. Repository provenance and worktree
   observation derive from the invocation repository, so running from the emBEADings checkout is not
   a substitute.
3. Run one of:

```sh
# Beads
python <plugin-root>/scripts/run_embeadings.py collisions

# Linear; replace TEAM with the selected team ID, key, or exact name
python <plugin-root>/scripts/run_embeadings.py --source linear --linear-team TEAM collisions
```

Use `--worktree-map ISSUE_ID=PATH` only for a registered worktree containing genuine active
implementation changes. Never associate an idle, administrative, stale, or reconstructed worktree
just to increase coverage.

Branches that spell an issue ID in full or short form (`abc12.4`, `abc12-4`) associate on their own.
For another naming convention, pass `--branch-pattern REGEX` with the ID in a named group `id`.
On a large tracker, `--min-confidence corroborated` keeps only leads with worktree evidence on at
least one side; the report counts what the filter dropped.

## Interpret conservatively

- Observed exact-file evidence is the strongest collision lead.
- Explicit path references can describe inspection, documentation, or policy rather than intended
  edits; read the task intent before treating them as collisions.
- Shared-module evidence is coordination context, not proof of a conflict.
- Hub omissions intentionally prevent broad files and directories from flooding the queue.
- A run with fewer than two genuinely active observed worktrees is a partial diagnostic, not an
  observed-to-observed release gate.

Rate each retained lead `2` for a likely edit collision, `1` for useful sequencing or ownership
coordination, and `0` for misleading shared-surface evidence. Report counts by confidence and kind,
the strongest actionable leads, false-positive patterns, worktree coverage, provenance warnings, and
known misses. Do not include issue bodies or source snippets in the response.
