# emBEADings

<img src="https://raw.githubusercontent.com/CantrellJax/embeadings/main/assets/brand/embeadings-mark.svg" width="72" alt="Two epic molecules reaching toward an amber collision point">

[![CI](https://github.com/CantrellJax/embeadings/actions/workflows/ci.yml/badge.svg)](https://github.com/CantrellJax/embeadings/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/CantrellJax/embeadings?include_prereleases)](https://github.com/CantrellJax/embeadings/releases/tag/v0.5.1)

Find duplicate and related issues in Beads and Linear, even when they're worded differently, plus the
open issues your merged pull requests already fixed—without changing the tracker or sending issue text
to an embedding API.

**Website:** [embeadings.jacksoncantrell.com](https://embeadings.jacksoncantrell.com), with guides to
[duplicate issues in Beads](https://embeadings.jacksoncantrell.com/beads-duplicate-issues/),
[Linear backlog cleanup](https://embeadings.jacksoncantrell.com/linear-backlog-cleanup/),
[closing issues after a merge](https://embeadings.jacksoncantrell.com/close-issues-after-merge/), and
[instructions for coding agents](https://embeadings.jacksoncantrell.com/for-agents/). emBEADings is listed in the
[Beads community tools](https://beads.gascity.com/community-tools).

emBEADings is a read-only coordination CLI for
[Beads](https://github.com/gastownhall/beads) and Linear. It combines typed tracker relationships,
local semantic retrieval, explicit code pointers, and genuine Git worktree changes into a bounded,
deterministic review queue.

Dependencies answer “what blocks this?” emBEADings asks “what else should I inspect before these
changes merge?” It provides evidence, not an automatic verdict.

> **Status:** v0.4 technical preview. The CLI and GitHub release are public; the bundled Codex and
> Claude Code plugin is still a local developer preview.

## Quick start

Python 3.11 or later is required. The default Beads source also requires an installed `bd` CLI.

```bash
pipx install embeadings
# or: uv tool install embeadings

# Check the environment without loading issue text
embead doctor

# Produce a bounded coordination packet
embead triage

# Find active work touching the same files or modules
embead collisions

# Inspect semantic neighbors for one record
embead neighbors ISSUE_ID --include-closed

# Find the stragglers: similar records with no structural link
embead neighbors ISSUE_ID --orphans-only

# Several seeds in one load; rank and reverse rank beat raw scores
embead neighbors ISSUE_A ISSUE_B --ids-file more-seeds.txt

# Records whose text says "absorbed by X" or "duplicate of X" with no typed link
embead mentions

# Live issues whose parent is closed or missing (structural, no model)
embead orphans

# Nearest existing records for text that is not a bead yet (no placeholder record)
embead match --title "Persist login across restarts" --body-file draft.txt

# Live records that today's merges may already have done (reads git, never writes)
embead superseded --since 2026-10-07

# One deduplicated line per neighbor across many seeds, with owner and in-progress guards
embead neighbors --ids-file sprint.txt --exclude-siblings --respect-soft-links --format table
```

Without `pipx` or `uv`, the standard library is enough:

```bash
python3 -m venv .venv
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install embeadings
```

For an immutable GitHub fallback, install the verified release wheel directly:

```bash
python -m pip install \
  "https://github.com/CantrellJax/embeadings/releases/download/v0.5.1/embeadings-0.5.1-py3-none-any.whl"
```

The first semantic command downloads the pinned
[`minishlab/potion-base-8M`](https://huggingface.co/minishlab/potion-base-8M) model. Embedding happens
locally. `collisions` does not load an embedding model.

Release assets include a source archive and `SHA256SUMS`. See the
[v0.5.1 release](https://github.com/CantrellJax/embeadings/releases/tag/v0.5.1) for versioned artifacts and
checksums.

## What a lead looks like

![Synthetic terminal example of an observed exact-file collision](https://raw.githubusercontent.com/CantrellJax/embeadings/v0.5.1/assets/brand/synthetic-collision-evidence.svg)

This shortened example is derived from the committed synthetic collision fixture:

```json
{
  "issue_id": "demo-1",
  "related_issue_id": "demo-2",
  "kind": "exact-file",
  "confidence": "observed",
  "shared_paths": ["src/cache/index.py"],
  "evidence_sources": ["active-worktree-diff"],
  "what_to_verify": "Verify whether concurrent work will modify the shared file paths before implementation or merge."
}
```

The full report also records repository provenance, revision relation, hub suppression, warnings, and
the read-only policy. It contains pointers rather than source snippets. See
[`examples/collisions.json`](https://github.com/CantrellJax/embeadings/blob/v0.5.1/examples/collisions.json)
and the
[example guide](https://github.com/CantrellJax/embeadings/blob/v0.5.1/examples/README.md).

## Why trust it?

| Evidence | Result | Boundary |
| --- | --- | --- |
| Concurrent-worktree release gate | Recovered all 3 known exact-file collisions across 4 genuine active worktrees | One repository; not universal recall |
| Dogfooding | Found 2 real association/scope defects before v0.4.0 | Demonstrates workflow value, not broad precision |
| Ruff scale surrogate | 17/20 top-packet pairs were at least contextually useful across 8,143 public issues | Converted GitHub corpus; no native Beads graph or worktrees |
| Release validation | Full CI passed across Linux, macOS, Windows, Python 3.11 and 3.14; wheel/sdist checksums and provenance published | Supply-chain and test evidence, not semantic quality |
| Repeatability | Evaluation outputs were byte-stable and non-mutating | Determinism does not make a weak lead correct |

Read the [dogfood release-gate story](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/articles/dogfooding-v040-worktree-gate.md),
[aggregate v0.4.0 worktree gate](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/research/code-surface-v040-release-gate.md),
[Ruff scale review](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/research/ruff-scale-surrogate-01.md),
and the
[research index](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/research/README.md)
for methods, failure patterns, and limitations.

## How it works

```text
Beads or one Linear team
        │
        ├── typed relationships and lifecycle
        ├── explicit paths and observed worktree changes
        └── local whole-record and field-level embeddings
                              │
                              ▼
                   bounded candidate union
                              │
                              ▼
                evidence receipts + review packet
```

`triage` is the opinionated front door. It admits at most 20 semantic candidates by default, includes
code-surface analysis when genuine local Git evidence exists, and writes a complete audit report to
external user state. Use `sweep` for experimental policy controls and `neighbors` for one-record
inspection. The default is a reviewer-capacity budget, not corpus coverage; see the
[review-budget decision](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/decisions/review-budget-default.md).

For `triage`, `sweep`, and `batch`, use `--output-dir DIRECTORY` when you want the complete JSON,
Markdown, and per-batch artifact set. Use `--output report.json` or `--output report.md` for one
primary report file; the extension chooses the file format independently of `--json` stdout.
Any other `--output PATH` remains a backward-compatible directory spelling. `neighbors`,
`collisions`, `mentions`, `orphans`, `match`, and `superseded` always treat `--output` as one atomic report file.

`collisions` reviews `open`, `in_progress`, and `blocked` work by default. It associates Git worktrees
when a branch spells exactly one issue ID: the full ID, a prefix-less short form such as `abc12.4` or
`abc12-4`, or an unambiguous `bead-N` suffix. Teach it another convention, or explicitly map an
otherwise unassociated worktree:

```bash
embead collisions --branch-pattern 'ticket/(?P<id>[a-z0-9.]+)'
embead collisions --worktree-map embead-42=../feature-worktree
```

On a busy tracker most leads are `explicit` (two records mention the same path in prose). Keep only
leads with worktree evidence with `--min-confidence corroborated`, or both sides observed with
`--min-confidence observed`.

Never fabricate a mapping: observed evidence must describe genuine active implementation work.
Shared paths are coordination evidence, not proof that two tasks conflict.

## Linear

Create a personal API key in Linear's Security & access settings and load it without placing the value
directly in a shell-history entry:

```bash
LINEAR_API_KEY="$(python -c 'import getpass; print(getpass.getpass("Linear API key: "))')"
export LINEAR_API_KEY

embead --source linear --linear-team ENG triage
embead --source linear --linear-team ENG collisions
```

`LINEAR_ACCESS_TOKEN` accepts an OAuth token instead; set only one credential. The CLI queries one
selected team through Linear GraphQL and does not reuse credentials held by an MCP or agent host. See
the [Linear adapter contract](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/linear.md).

## Companion: emBEADify

emBEADings finds the leads and never writes. When a human or coordinator has reviewed them and wants
to act, [emBEADify](https://github.com/CantrellJax/embeadify) is the write-side companion: it turns
reviewed decisions into parallel, dry-run-by-default, undoable Beads updates, and it can draft a
commented decisions file from an `embead` report. Keeping the two apart is deliberate, so emBEADings
stays read-only.

## Privacy and data boundary

- Tracker adapters contain no mutation operations.
- The default model embeds issue text locally; issue text is not sent to Hugging Face.
- Linear mode sends tracker queries only to Linear itself.
- Models and vectors use the platform user cache; reports use the platform user state directory.
- Neither cache nor reports are written into the analyzed repository by default.
- Collision reports contain code pointers, not source snippets.
- A human or coordinator must verify every lead before changing tracker or source state.

The first model download is network activity. Prepare it before loading private issues when evaluating
under OS-level network denial. See the
[safe offline evaluation guide](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/evaluation.md).

## Good fit / poor fit

emBEADings is most useful when a tracker is too large for repeated full-context review, several people
or agents work concurrently, and the team values a reproducible offline shortlist.

It is less useful for a small tracker that one reviewer can read directly, repositories without
meaningful tracker-to-code evidence, or teams seeking automatic issue mutation, orchestration, a
dashboard, or a general memory system. Typed dependencies remain tracker truth; semantics complement
them rather than re-deriving authority.

## Development

Every clone or Git worktree must own its virtual environment:

```bash
python3 scripts/worktree_env.py
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python scripts/validate.py
```

The bootstrap refuses to reuse an active environment from another checkout. Validation checks the
editable `embead` import target before formatting, lint, tests, and release checks. Read
[`CONTRIBUTING.md`](https://github.com/CantrellJax/embeadings/blob/v0.5.1/CONTRIBUTING.md)
before submitting fixtures or reports; private tracker content
must never be committed.

## Agent plugin preview

[`plugins/embeadings`](https://github.com/CantrellJax/embeadings/blob/v0.5.1/plugins/embeadings/README.md)
packages `triage`, `collisions`, and `evaluate`
skills for local Codex and Claude Code development. It delegates to the installed CLI, forces schema-v1
JSON, and verifies the read-only policy. It is not yet a marketplace release and grants no tracker-write
authority.

## Documentation

- [Documentation index](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/README.md)
- [CLI and product specification](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/spec.md)
- [Consumer and schema contract](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/consumer-contract.md)
- [Performance and scale evaluation](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/performance.md)
- [Research and evaluation ledger](https://github.com/CantrellJax/embeadings/blob/v0.5.1/docs/research/README.md)
- [Versioned JSON Schemas](https://github.com/CantrellJax/embeadings/tree/v0.5.1/schemas/v1) and
  [synthetic examples](https://github.com/CantrellJax/embeadings/blob/v0.5.1/examples/README.md)

## Principles

- **Read-only means read-only.** Analysis never closes, edits, labels, or reprioritizes work.
- **The tracker remains authoritative.** Structure and lifecycle stay tracker data.
- **Local-first and private.** The default semantic provider sends no issue content to a network API.
- **Bounded and auditable.** A stable receipt explains what entered or was omitted from the queue.
- **Agent-neutral.** Core analysis does not depend on Codex, Claude Code, Cursor, or another runtime.

MIT licensed. emBEADings is not affiliated with Beads or Linear.
