# Changelog

## Unreleased

Field-report follow-ups from a native-Beads sweep of about 6,000 issues and 14 worktrees.

- Add `embead mentions`: records whose text names another as a duplicate, successor, absorber, or
  fix ("absorbed by X", "superseded by X", "duplicate of X", "fixed by X") with no typed link.
  Resolves full and prefix-less IDs, skips denials, scans notes and close reasons, and reports only
  IDs, the claim kind, and field names. Opt-in bare mentions, closed claimants, and linked claims.
- Associate worktrees whose branches use short bead forms (`abc12.4`, `abc12-4`), prefer a child
  over its parent, refuse branches that spell two unrelated issues, and add a repeatable
  `--branch-pattern REGEX` for repository conventions.
- Add `--min-confidence explicit|corroborated|observed` to `collisions` (and code-surface sweeps),
  counting the leads it drops as `pairs_omitted_by_confidence_filter`.
- Report each neighbor's `rank`, `reverse_rank`, and `text_claims`, and document that rank is a
  stronger signal than a model-specific score cutoff.
- Accept several seeds in `neighbors` (positional IDs and `--ids-file`), sharing one tracker, model,
  and vector load and emitting a `neighbors-batch` report.

## 0.4.3 — 2026-07-29

Field-report follow-ups: a broken headline command, and diagnostics you can act on.

- Name the failing stage's terms and the resulting unaccounted delta in dependency conservation
  errors, degradation receipts, and the human-readable warning, instead of reporting only that the
  funnel does not conserve.
- Add `neighbors --orphans-only` for high-similarity records with no recorded structural link,
  applying the filter before `--limit` so the budget bounds surviving neighbors.
- Add `collisions --explain-hub-guard [PAIRS]` to sample suppressed pairs alongside the hub surfaces
  that suppressed them, making the guard auditable rather than merely counted.
- Add a global `--fail-on-divergence` flag that exits 3 when live tracker data and the discoverable
  export disagree, after the report is written.
- Document the standard-library `venv` plus `pip` installation path for environments without `pipx`
  or `uv`.
- Conserve typed dependency edges separately from pair-level candidates, including multi-type,
  reciprocal, and mixed parent/child relationships.
- Degrade only the dependency lane when its conservation audit fails, retaining healthy semantic
  lanes with deterministic, privacy-safe diagnostic receipts.
- Distinguish complete artifact directories (`--output-dir`) from single JSON or Markdown report
  files (`--output`) while preserving legacy directory paths that do not use a report suffix.
- Add shadow-only freshness evaluation primitives with bounded bidirectional relationship context,
  conservative action labels, deterministic comparison packets, and no public behavior change.
- Separate sparse freshness actions from independently budgeted informational discovery, recognize
  audited explicit-lineage cues, and retain semantic-only uncertainty with finite admission receipts.
- Prevent typed parent/child relationships from being reintroduced as legacy untyped dependencies,
  restoring dependency-funnel conservation on the live self-dogfood tracker.
- Add fresh Ubuntu, macOS, and Windows distribution journeys for the canonical wheel through
  persistent `uv tool` and one-shot `uvx`; retain the decision to ship no secondary channel yet.

## 0.4.2 — 2026-07-16

Standard distribution and public proof.

- Publish verified wheel and source distributions through PyPI Trusted Publishing with short-lived
  GitHub OIDC credentials and a protected `pypi` environment.
- Build release artifacts once, preserve checksums, and pass the exact verified distributions to
  both the GitHub release and PyPI publishing jobs.
- Add `pipx` and `uv tool` installation paths while retaining the immutable GitHub-wheel fallback.
- Add a compact visual system and a bounded four-worktree dogfood story with explicit limitations.
- Document deferred-work inclusion and retain the 20-candidate triage default while a bounded
  corpus-aware budget remains an evaluation decision.

## 0.4.1 — 2026-07-16

Privacy, packaging, and public-repository hardening.

- Restrict POSIX cache/state roots and run directories to `0700`, and sensitive derived files to
  `0600`, including atomic replacements.
- Narrow source distributions to runtime source, schemas, and required package metadata; verify fresh
  installs from both wheel and source archive.
- Pin release workflow actions and build tooling, validate tagged source, and attest future release
  artifacts with GitHub build provenance.
- Replace the internal-reference README with an outcome-led quick start, representative output,
  evidence boundaries, and task-oriented documentation indexes.
- Add privacy-aware issue forms, a pull-request checklist, current security guidance, and accurate
  Linear client version metadata.

## 0.4.0 — 2026-07-16

First public technical preview.

- Read-only Beads and Linear adapters with deterministic structural and semantic review queues.
- Local code-surface collision evidence from explicit task pointers and genuine Git worktrees.
- Bounded triage packets, objective-specific experimental retrieval, checkpoints, and audit receipts.
- Dual Codex and Claude Code plugin foundation around the same guarded CLI contract.
- Public scale diagnostics and privacy-preserving evaluation protocols.

The release gate used four genuine active worktrees and retained all three known exact-file
collisions. That one-repository result does not establish precision or recall across other repository
layouts; broader ICP evaluation remains ongoing.
