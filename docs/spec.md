# Product and technical specification

## 1. Summary

emBEADings is a standalone, read-only companion CLI for engineering work trackers. It incrementally embeds issue
text, retrieves semantically related records, identifies active work that resembles completed work,
and partitions a review population into small disposable neighborhoods suitable for humans or
read-only reviewer agents.

The output is evidence-discovery material, not tracker truth. Similarity must never directly close,
defer, reprioritize, relabel, or rewrite an issue.

### Specification status

This is a living specification for the v0.4 implementation. Unless a section is explicitly marked
**experimental** or **post-MVP**, it describes shipped behavior. The command inventory in section 6
is the current public CLI contract and takes precedence over older milestone language.

- **Shipped:** synchronous local analysis, Beads and Linear read adapters, `triage`, `neighbors`,
  `sweep`, its `batch` alias, `collisions`, readiness diagnostics, versioned reports, and the thin
  local agent-plugin foundation.
- **Experimental:** explicit review objectives and field-aware semantic retrieval. These are
  opt-in research surfaces, not the default ranking contract.
- **Post-MVP:** background/async runs, run-status commands, first-party agent dispatch, hosted
  embedding providers, and semantic code or AST indexes. These are roadmap concepts, not accepted
  commands or implied release commitments.

## 2. Problem

Dependency graphs encode explicit ordering and blocking relationships. They do not reliably reveal:

- parallel issues that became stale after the same architectural change;
- active records whose outcomes may already have shipped elsewhere;
- deferred work adjacent to an active implementation boundary;
- duplicate or overlapping scope written with different vocabulary;
- a review population that is too large for one agent context.

Keyword search helps only when authors use the same words. Durable semantic labels create another
taxonomy that must itself be maintained. The tool should instead compute local, disposable semantic
relationships from current issue text.

## 3. Current goals

1. Read a live Beads workspace or selected Linear team through supported read-only interfaces.
2. Embed new or changed records incrementally using a content-addressed cache.
3. Return nearest active and completed neighbors for an issue.
4. Surface high-similarity active/completed pairs as review candidates.
5. Produce deterministic, balanced review batches with a configurable target size.
6. Emit stable JSON for tools and concise Markdown for people and agents.
7. Keep all models, vectors, logs, and reports out of the analyzed repository by default.
8. Make privacy, offline operation, and non-mutation testable invariants.

### Post-MVP goals

- Consider non-blocking background runs with explicit queued/running/complete/failed state only if
  synchronous runs become an observed workflow constraint.
- Consider first-party agent dispatch only after the manifest contract proves insufficient for host
  integrations. The current plugin intentionally delegates to the installed synchronous CLI.

## 4. Non-goals

- Replacing tracker search, dependencies, readiness, labels, or lifecycle.
- Persisting clusters or semantic labels into a tracker.
- Automatically applying review findings.
- Acting as an issue editor, dashboard, kanban board, or project-management system.
- Hosting a daemon, vector database service, or general-purpose memory layer.
- Requiring a particular coding-agent runtime.
- Sending issue content to a hosted embedding API by default.
- Reading `.beads/issues.jsonl` as tracker truth.

## 5. User stories

### Maintainer

As a maintainer, I can run a weekly sweep and receive small related batches so reviewers examine
parallel stale work without interrupting normal development.

### Reviewer

As a reviewer, I receive issue IDs, current metadata, semantic neighbors, and a bounded evidence
rubric. I can inspect source control and project documentation, but the review process cannot mutate
the tracker.

### Tool author

As a tool author, I can consume versioned JSON manifests without depending on internal embedding
implementation details. A host may wrap that synchronous contract today; first-party dispatch is
post-MVP.

## 6. CLI contract

The shipped command inventory is:

```bash
embead triage [--review-budget 20]
embead neighbors ISSUE_ID [ISSUE_ID ...] [--ids-file FILE] [--limit N] [--include-closed] [--orphans-only]
                 [--exclude-seeds] [--exclude-siblings] [--respect-soft-links] [--format table]
embead superseded --since DATE|COMMIT [--repo DIR] [--ref REF] [--changes-file FILE.jsonl]
embead sweep [--size 9]
embead batch [--size 9]
embead collisions [--worktree-map ISSUE_ID=PATH] [--branch-pattern REGEX] [--min-confidence LEVEL]
embead mentions [--include-linked] [--include-closed] [--include-mentions] [--limit 50]
embead orphans [--include-parentless] [--include-ephemeral]
embead readiness [--offline]
embead doctor [--offline]
embead capabilities [--json]
embead schema [REPORT_TYPE] [--json]
```

All analysis commands are synchronous. `--json` selects machine-readable stdout. For the
multi-artifact `triage`, `sweep`, and `batch` commands, `--output-dir DIRECTORY` writes the complete
JSON, Markdown, and per-batch set; `--output REPORT.json` or `--output REPORT.md` writes only the
primary report in the extension-selected format. Extensionless `--output PATH` remains a
backward-compatible directory spelling, as does any other non-report suffix. `neighbors`,
`collisions`, `mentions`, `orphans`, `match`, and `superseded` use `--output FILE` for their
single atomic report, whose format follows `--json` (or `neighbors --format`). `triage` is the opinionated bounded front door,
while `sweep` exposes research and policy controls. `batch` is currently an alias for a synchronous
sweep, not a separate scheduler.

Common population controls are deliberately narrower than the original proposal. `triage`,
`sweep`, `batch`, and `collisions` accept stored status filters and optional epic inclusion where
applicable. `sweep` and `batch` also accept incremental timestamps or external checkpoints,
candidate-policy controls, explicit objectives, and optional code-surface analysis. The CLI does
not translate parent, ready, label, or arbitrary issue-ID expressions into tracker queries.

### `neighbors`

```bash
embead neighbors ISSUE_ID [ISSUE_ID ...] [--ids-file FILE] [--limit N] [--include-closed] [--orphans-only]
```

Returns the nearest records with similarity scores and structural context. Human output must label
scores as advisory. JSON output includes the embedding model and index generation.

Each neighbor carries its `rank` (1 is nearest) and `reverse_rank`: where the seed ranks among that
neighbor's own neighbors, over the same candidate pool. Rank is the stronger signal. Absolute cosine
scores are model-specific and compress: with a small static model, unrelated records in one domain
can score 0.78 to 0.83 while a true duplicate ranks first at 0.68. Read a rank-1 neighbor before
applying any score cutoff. A small reverse rank means the pair is mutual; a large one means the
neighbor is a hub that many records resemble. `sweep` already admits near-threshold pairs that
mutually rank within `--reciprocal-rank` when they fall within `--exception-margin` of the threshold;
widen the margin to admit more mutual pairs.

Each neighbor also carries `text_claims`: lineage claims either record's text makes about the other
(see `mentions`), as IDs and a claim kind only.

Several seeds (positional IDs, `--ids-file`, or both; duplicates are dropped in first-seen order) share
one tracker load, model load, and vector index. One seed emits the `neighbors` report; several emit a
`neighbors-batch` report whose `results` hold one `issue` and `neighbors` pair per seed.

`--orphans-only` keeps only neighbors whose structural context is `none recorded`: the
straggler question of high similarity with no tracker link. Because that property belongs to the
pair rather than to the ranking, the filter is applied before `--limit`, so the limit bounds
surviving neighbors rather than ranked ones. JSON output records the applied filter.

The other pair filters work the same way, before `--limit`, and each result counts what it dropped
under `dropped`. `--exclude-seeds` drops neighbors that are themselves seeds; it is on by default
with several seeds, because in a sprint sweep seed-to-seed hits are the sprint the caller already
knows about (`--no-exclude-seeds` restores them). `--exclude-siblings` drops neighbors that share the
seed's direct parent (the recorded parent, else the one a hierarchical ID such as `abc12.4` spells). `--respect-soft-links` drops pairs whose text already records a lineage claim
(`FOLDED into X`, `Folded in: X`, `Duplicate of X`, `Superseded by X`), so a folded pair does not
resurface in the next sweep.

Each neighbor carries `assignee`, `updated_at`, and `guards`: advisory triage flags computed from
labels, status, assignee, and title words. `label:<label>` marks an owner label (`--guard-label`,
default `owner-run` and `ask:owner`), `in-progress-assigned` marks work someone holds, and
`keyword:<word>` marks a title or label word such as `prod`, `published`, `privacy`, `security`, or
`money` (`--guard-keyword` replaces the list). Each result also carries `score_baseline`: the p50,
p90, and p99 of the seed's similarity to every record in scope, so a reader can tell a standout from
the neighbourhood on this model and corpus. `neighbors-batch` adds `merged_neighbors`, one row per
neighbor across all seeds with `best_similarity`, `best_seed`, `best_rank`, `seeds`, and
`seed_count`. `--format table` prints that view as one plain-text line per neighbor (score, seed
count, best seed, status, priority, assignee, guards, title) under a baseline line. `--format json`
is the same as `--json`.

`embead schema REPORT_TYPE` prints any report's fields one per line with their types and
descriptions, from the packaged JSON Schemas; `--json` prints the schema itself.

### `superseded`

```bash
embead superseded --since DATE|TIMESTAMP|COMMIT [--repo DIR] [--ref HEAD]
                  [--changes-file FILE.jsonl] [--limit 20] [--per-change 10]
                  [--include-epics] [--include-closed-since] [--guard-label LABEL]
                  [--include-ephemeral] [--json] [--output FILE]
```

Answers the question a coordinator asks after a sprint: which live records did the merged changes
already do? `neighbors` compares records with records; this compares records with changes.

Changes come from `git log --first-parent -m --name-only` on `--ref` in `--repo` (a bare date means
local midnight; a commit means `COMMIT..REF`), or from `--changes-file`, one JSON object per line
with `title`, `body`, `files` (paths or `{"path": ...}` objects, as `gh pr list --json` emits),
`number`, and `mergedAt`. A squash commit's `(#N)` suffix and a merge commit's `Merge pull request
#N from BRANCH` subject give the PR number. Each change's query text is its title, body, and changed
paths, embedded in memory with the same canonical-text rules as a record; it never reaches the
vector cache.

A squash merge that lists several commits (`* subject`, blank line, body) is also split into one
facet per commit, and each facet is ranked on its own. A fix bundled into a larger pull request
then still reaches the record it closes: on the 2026-10-07 onCall sweep the record a PR's second
commit fixed ranked 18th against the whole PR and first against that commit.

The report has two lists. `named` holds records in scope that a change names by ID in its title,
body, or branch: work the change probably finished. Those records are removed from the similarity
ranking, where they would take first place in their own change. `similar` holds the `--per-change`
nearest records of each facet, merged so each record keeps its best evidence, ordered by
`rank_in_change`, then `lead` (how far the record's score leads the next record for that change),
then score. Rank and lead are the primary signals because cosine scores are model-specific and a
long change sits close to many records. Code identifiers the change and record share (camelCase
and snake_case names, kebab-case and file stems, routes) that at most 0.5% of records carry add 0.02
each, up to three, to `combined_score`; they are listed as `shared_identifiers`.

Each row carries the record's `id`, `title`, `status`, `priority`, `labels`, `assignee`, `guards`,
`change_count`, and `evidence` (`change_id`, `pr_number`, `commit`, `merged_at`, change `title`,
`basis`, `facet`, `similarity`, `combined_score`, `rank_in_change`, `lead`, `shared_identifiers`).
Epics are excluded unless `--include-epics`. `--include-closed-since` also compares records closed
after the first change merged, which replays a sweep after its follow-up closes. The command reads
git and the tracker only; it closes, links, and comments on nothing.

### `triage`, `sweep`, and `batch`

```bash
embead triage [--review-budget 20] [--size 9] [--output-dir DIRECTORY]
embead sweep [--status STATUS] [--size 9] [--output-dir DIRECTORY]
embead batch [--status STATUS] [--size 9] [--output-dir DIRECTORY]
```

Builds deterministic, bounded semantic neighborhoods from a candidate population. Population
filters are applied before embeddings. `triage` writes the full sweep audit to external run state
and returns a smaller agent-ready packet carrying the same stable analysis fingerprint.

By default, semantic triage includes `open`, `in_progress`, `blocked`, and `deferred` review
primaries; repeat `--status` to narrow that scope. Deferred work remains included so stale or
already-completed echoes can surface during tracker hygiene. This differs intentionally from
`collisions`, whose default population is limited to work that may be concurrent now.

### `collisions`

```bash
embead collisions [--status STATUS] [--worktree-map ISSUE_ID=PATH] [--branch-pattern REGEX] [--min-confidence LEVEL]
```

Produces bounded local code-surface coordination leads without loading the embedding model. The same
evidence can be added to `sweep` or is enabled opportunistically by `triage`.

A worktree is associated with an active issue when its branch spells exactly one issue ID. Accepted
spellings are the full ID (`proj-abc12.4`), the prefix-less form (`abc12.4`), and either with dots
as dashes (`abc12-4`), read from any run of `/`, `_`, or `-` separated branch segments. A prefix-less
top-level hash needs at least four characters, so tokens such as `c10` never match. When a branch
spells both a parent and its child, the child wins. A branch that spells two unrelated issues stays
unassociated. `--branch-pattern REGEX` (repeatable) adds a repository convention: the named group
`id`, or group 1, is resolved through the same spellings. `--worktree-map` always takes precedence.

Confidence is `observed` when both sides changed the shared surface in a worktree, `corroborated`
when one side did and the other only mentions it in tracker text, and `explicit` when both only
mention it. `--min-confidence corroborated|observed` drops weaker leads after pairing and reports the
count as `pairs_omitted_by_confidence_filter`.

### `mentions`

```bash
embead mentions [--include-linked] [--include-closed] [--include-mentions] [--limit 50]
```

Finds records whose text names another record with a lineage verb (duplicate of, superseded by,
replaced by, absorbed by, folded into, moved to, carried by, fixed by, shipped in, supersedes, absorbs,
continued in) and reports the claims that no parent or dependency link backs. The object may sit a
few words after the verb within the same clause; denials ("not a duplicate of") are skipped. IDs
resolve through the same spellings as worktree association. All text fields are scanned, including
notes and the Beads close reason, but only IDs, the claim kind, and the field names are reported.
By default only active records' claims are listed; `--include-closed` adds closed claimants,
`--include-linked` adds claims a typed link already backs, and `--include-mentions` adds bare
mentions whose spelling carries a dash or dot. The report does not load an embedding model.

### `orphans`

```bash
embead orphans [--include-parentless] [--include-ephemeral]
```

A structural hygiene report that needs no embedding model. It lists live issues (every status
except `closed`: `open`, `in_progress`, `blocked`, and `deferred` all count) whose parent is closed
or absent from the tracker (`parent_status` is `closed` or `missing`). The whole workspace is loaded
with closed records included, because a parent that is deferred, in progress, or blocked is a live
parent: an open-only listing hides those parents and makes every one of their children look
orphaned.

Only the nearest link is judged, so each broken link appears once: a child of a live parent is not
reported even when that parent is itself orphaned. Rows carry `issue_id`, `issue_type`, `status`,
`priority`, `title`, `parent_id`, and `parent_status`, ordered by `parent_id` then `issue_id`, under
`dangling_parent`, with `summary` counts. `--include-parentless` additionally lists live issues with
no parent at all under a separate `parentless` field, grouped by `issue_type`, since top-level epics
are normal and are never mixed into the dangling-parent list. Ephemeral records are not reported
unless `--include-ephemeral` is set, but they still count as parents. The report only reads; it
reparents, reopens, and closes nothing.

### `match`

```bash
embead match --title TITLE [--body TEXT | --body-file FILE] [--candidate-id ID]
embead match --candidate-file FILE.json
embead match --candidates-file FILE.jsonl
             [--limit N] [--include-closed | --no-include-closed] [--min-similarity X]
             [--include-ephemeral] [--json] [--output FILE]
```

Finds the nearest existing records for candidate text that is not yet a record, where `neighbors`
needs a seed already in the snapshot. Exactly one input mode is used: `--title` with an optional
body; `--candidate-file` holding `{"title", "body"}` (optional `candidate_id`); or `--candidates-file`
with one JSON object per line, each carrying a caller-supplied `candidate_id`. `candidate_id`
defaults to `candidate-1` in the single-candidate modes, must be unique, and is echoed unchanged.

The candidate is embedded with the same pinned local model and canonical-text rules as a bead record
(the body plays the role of the description) and compared with the cached whole-record vectors.
Closed records are included by default, because a closed neighbor is "already done" evidence;
`--no-include-closed` drops them. Ephemeral records are excluded unless `--include-ephemeral`.
`--limit` defaults to 10 (minimum 1). `--min-similarity X` drops neighbors scoring below X.

The report (`report_type` `match`) has `candidates`, one entry per input in input order:
`candidate_id`; `content_hash` (SHA-256 over the exact title and body, so a caller can detect a
changed payload); `status` (`matches` or `no-match`); `no_match_reason` (`null`,
`no-records-in-scope`, or `below-min-similarity`); `records_compared`; `matches_above_threshold`;
`notice`; and `neighbors`. Each neighbor has `issue_id`, `status`, `issue_type`, `priority`, `title`,
`similarity`, `rank`, `is_closed`, `parent_id`, `parent_status` (`null`, `live`, `closed`, or
`missing`), and `resolution_evidence`: `{"kind": "close_reason", "text": ...}` for a closed neighbor
whose tracker recorded a close reason, otherwise `null`. It is never inferred. Neighbors are ordered
by similarity descending, then `issue_id`. `policy` adds `creates_records: false` to the usual
read-only constants. Similarity is a retrieval lead, not a duplicate verdict.

The command reads the tracker only through the allowlisted read-only calls, creates no placeholder
record, and keeps candidate text in memory: it is not written to the vector cache or any reports
directory, and the report carries the hash and ID, not the text. Candidate vectors are never cached.

### Readiness and capability inspection

```bash
embead readiness [--offline]
embead doctor [--offline]
embead capabilities [--json]
```

These commands inspect or prepare local prerequisites without reading tracker issue content where
their contract says so. `capabilities` lets consumers negotiate the report contract before invoking
analysis.

### Post-MVP async concept

`embead sweep --async` and `embead status RUN_ID` are reserved design sketches. They are not parsed
by v0.4. If implemented, async execution must preserve the same read-only analysis contract, expose
bounded queued/running/complete/failed state, and avoid introducing a required daemon.

## 7. Data acquisition

All acquisition implementations satisfy one tracker-neutral, read-only `load()` contract. The
default adapter invokes the installed `bd` binary with read-only and JSON flags. It must:

- discover the workspace using supported Beads context commands;
- request all fields needed for canonical text, lifecycle, parentage, labels, and dependencies;
- capture the Beads version and workspace identity in report metadata;
- fail closed if JSON is malformed or the installed version lacks required read-only behavior;
- never import, export, sync, update, create, close, reopen, label, or modify dependencies.

The core consumes an internal `IssueRecord` schema so future adapters can be added without coupling
the semantic algorithms to one Beads output version. The supported minimum and exact command/payload
contract are documented in [Beads compatibility](beads-compatibility.md).

The Linear adapter uses the public GraphQL API with an environment-provided personal API key or
OAuth access token. It resolves one team, pages team issues and workspace relations without per-issue
detail calls, filters endpoints back to that team, and canonicalizes one typed structural edge per
unordered issue pair before ranking. Its transport rejects non-query operations. Generated branch
suggestions are metadata, never observed edit evidence; observed surfaces still require local Git.

## 8. Canonical semantic text

The initial canonical representation should include:

- title, with modest extra weight;
- current description;
- acceptance criteria;
- design/current notes when present.

Identifiers, timestamps, actors, comments, audit history, and labels should not be embedded by
default. They remain available as structural/report metadata. Very long fields are truncated with a
documented, deterministic policy so historical notes cannot overwhelm current intent.

The content hash includes:

```text
schema version + canonicalization version + model ID + model revision + canonical text
```

Any change to these inputs invalidates only affected vectors.

## 9. Embedding provider

The provider interface accepts a batch of strings and returns normalized, fixed-dimension vectors
plus model metadata.

The default release should use a compact local CPU model with a pinned revision and a permissive
license. Hosted providers may be added behind explicit configuration and an unavoidable warning that
issue content will leave the machine.

The package manager installs Python dependencies. The program must not create a nested virtual
environment or run a package installer at runtime.

## 10. Cache and state

Use platform-standard user directories:

- cache: model artifacts and content-addressed vectors;
- state: run status, logs, manifests, and reports;
- config: optional user-level provider and output preferences.

Workspace state is namespaced by a stable hash of the canonical workspace identity, not its display
name. No model or vector file is written inside the analyzed repository unless the user explicitly
chooses an output path.

Cache writes must be atomic. Cross-process locking must work on macOS, Linux, and Windows; a
POSIX-only lock is insufficient. Corrupt, non-finite, wrong-dimension, or wrong-model vectors are
discarded and recomputed.

## 11. Similarity analysis

### Neighbors

Use cosine similarity over normalized vectors. Stable tie-breaking uses issue ID. Thresholds are
configuration, not universal truth, and every report records the effective values.

### Completed-work echoes

An active record whose closest completed neighbor exceeds a configured threshold is a review
candidate. The report says “verify against current project state,” never “close.”
For Beads, an optional close reason is bounded lifecycle counterevidence rather than semantic text.
An otherwise qualified semantic echo is omitted only when the reason references the exact paired
active ID and says the work was rehomed/moved and not completed/not done in the same clause or an
immediately following bounded sentence; marks the record `duplicate of` that ID (or superseded by it
with explicit canonical-record retention); or says the work was merged, folded, or absorbed into
that ID. Reports expose only stable count-by-code omission diagnostics, never close-reason text.
Missing, generic, ambiguous, negated, tentative, and different-ID reasons do not affect ranking;
neither does ordinary follow-up/filed/tracked-by prose. Linear behavior is unchanged. Incremental
runs re-evaluate qualifying pairs when either endpoint's review-relevant metadata changes.

### Deferred-work proximity

Semantic proximity alone is insufficient to recommend reconsidering deferred work. A candidate must
also have structural support, such as a shared parent, explicit dependency/relation, or direct textual
cross-reference. Explicit later-phase or trigger language is surfaced to reviewers as counterevidence.

### Duplicate candidates

High similarity may indicate duplication, shared context, or a broad parent/child relationship. The
tool reports candidates and structural differences; it does not merge them.

### Candidate ranking and volume

Default thresholds provide a conservative high-signal baseline. Lower-scoring pairs may enter the
review queue only when corroborated by structural evidence such as shared parentage, an explicit
dependency/relation, or reciprocal-neighbor rank. Reports record why the exception applied.

Candidate volume must be bounded through a deterministic per-issue cap or equivalent global budget.
Lowering a global threshold without a volume control is not an acceptable substitute for ranking.
The synchronous CLI defaults to a `0.08` exception margin, reciprocal rank `5`, three candidates per
issue, and 250 candidates per run. Candidates are assigned to typed-dependency, completed-work echo,
and overlap lanes with independent budgets. Standard runs admit typed dependencies before semantic
lanes; within the overlap lane, reciprocal exceptions rank behind stronger semantic signals.
Direct parent/child structure is reported as counterevidence and does not enable a below-threshold
exception on its own.

Reciprocal rank is corroboration, not sufficient evidence by itself. A below-threshold reciprocal
pair must also have corpus-discriminative, field-aligned local evidence: a rare title token aligned
with the other record (including CamelCase entities), or a rare multi-token phrase in the same
substantive field. A single long-form token outside a title is intentionally
insufficient. When either record has no substantive body, a shared non-function title token provides
a narrow sparse-record fallback. Frequency is derived deterministically from the local snapshot.
Reports expose only bounded evidence categories and counts, never matched terms or source text.

For recurring maintenance, `--weekly-review-budget N` is an opinionated hard total budget layered on
the existing lane and per-issue allowances. Before the standard dependency-first pass, it reserves
capacity for each evidence lane so a relation-rich tracker cannot consume the entire review queue.
For budgets of three or more, the target split is approximately 60% dependency, 20% completed-work
echo, and 20% possible overlap. A one-candidate queue reserves overlap; a two-candidate queue reserves
one dependency and one overlap. A reservation is minimum access, not a quota: capacity unused because
a lane lacks admissible candidates returns to the standard dependency → echo → overlap pass. The
budget is applied after incremental eligibility filtering, so unchanged records remain context
without consuming the queue. Sweep reports record the effective limit plus reserved,
admitted-to-reservation, unused, and omitted counts by lane. Code-surface collisions remain separate
evidence and do not consume this candidate budget.

Every sweep also emits a privacy-safe typed-dependency funnel. It counts non-parent typed edges,
edges inactive for the selected review scope (including closed-only structure), edges below the
bounded structural qualification floor, eligible edges, admitted edges, and omissions attributed
exclusively to the dependency per-issue allowance, dependency lane cap, or run cap. The producer
enforces both conservation equations: total equals inactive plus below-floor plus eligible, and
eligible equals admitted plus the three cap-omission counts. Funnel diagnostics contain counts only;
endpoint identifiers remain limited to admitted candidates and the existing capped-edge summaries.

A structure-only sweep with no comparable active typed relationship skips provider encoding and
cache access, records that skip in the report, and still emits the conserved structural funnel.
Comparable typed relationships continue to use semantic scores for qualification.

When either threshold is lowered below its default, selection first reproduces the default-threshold
queue under the same lane, endpoint, and run caps. Only remaining capacity is offered to permissive
additions. Reports expose qualified, admitted, baseline-protected, and cap-drop counts for every lane,
making sensitivity runs monotonic with respect to the bounded default queue.
If a stricter threshold changes which candidate survives a one-per-record, per-issue, lane, or run
cap, the report emits a deterministic `cap_replacements` entry. It contains candidate IDs, the
governing cap, displaced candidate IDs, and a nonempty causal chain from removed qualification
through each consumed or freed endpoint, echo, lane, or run slot. This makes cross-lane and cascading
bounded-queue replacements distinguishable from new semantic qualifications without exposing text.

Completed-work echo diversification has its own conserved audit funnel for every target affected by
the completed-target cap. Each `echo_target_hubs` entry satisfies `qualified = admitted + omitted`
and attributes every omission to exactly one governing reason: completed-target cap, one echo per
active record, general per-issue cap, echo lane cap, or run cap. `echo_backfills` links a target-cap
omission to a later admitted fallback for the same active record using candidate IDs and scores only.
This receipt proves a coverage substitution, not a relevance or actionability improvement.

### Evidence-specific explanations

Every candidate explains the evidence that caused it to surface. Explanations should identify the
strongest contributing canonical fields, lifecycle contrast, structural relationships, and relevant
counterevidence. Generic class-level prompts may supplement this evidence but cannot be the only
explanation.

Anchor extraction confidence, verification specificity, and candidate relationship uncertainty are
separate signals. Reports retain finite-vocabulary anchors and label specificity as a concrete check,
category check, or generic. Concrete checks require an explicit local contract, artifact, invariant,
test, or corroborated ownership-boundary type; the mere presence of acceptance criteria does not
qualify. Safe action/entity pairs remain category checks, while only failed extraction becomes
generic. Candidate evidence identifies semantic-only pairs with no structural
corroboration and preserves direct-threshold, reciprocal, shared-parent, and typed-dependency
admission paths. Neither semantic similarity nor an exception path may assert a shared contract or
completed outcome.

### Similarity performance

Providers return validated normalized vectors. Analysis must avoid renormalizing the same vector for
each comparison and must reuse pairwise scores within a run. A vectorized similarity matrix or an
equivalent bounded score cache is preferred for populations that fit comfortably in memory.

### Opinionated triage packet

`triage` is the default operator-facing workflow. It applies a bounded weekly review budget, enables
available local code-surface evidence, and writes both the complete `sweep` audit artifact and a
smaller agent-ready `triage` packet. The packet contains admitted candidates, collision leads,
bounded batches, conservation counts, warnings, and no arbitrary tracker body fields. Its
`analysis_fingerprint` is derived only from stable analysis inputs and decisions, so cold and warm
runs over the same snapshot can be compared without run IDs, timings, cache telemetry, or output
paths changing the identity. The complete and compact artifacts must carry the same fingerprint.

### Code-surface collision evidence

Code surfaces are optional corroborating evidence, not canonical tracker state and not a replacement
code-search index. The MVP may extract repository-relative file, directory, and `path::symbol`
pointers from work-record text and observe changed paths from associated local Git worktrees. It must
not copy source snippets, mutate a worktree, or persist inferred pointers back into Beads.

Every pointer records its source (`explicit-reference` or `active-worktree-diff`), bounded confidence,
finite edit intent (`observed-edit`, `likely-edit`, `reference-only`, or `unknown`), bounded reference
context (`prose`, `code-fence`, or `observed-worktree`), repository path presence, and Git revision
when available. Intent is a local ranking signal, not an admission gate: uncertain language must not
hide an exact-file lead. Collision records expose the contributing intent fields and rank observed or
repository-grounded prose ahead of fenced or missing references when stronger evidence is otherwise
equal. HTML issue-template comments never contribute pointers or intent. These signals do not
suppress collision recall. Repository provenance comes from the invoking worktree when it shares
the tracker checkout's Git common directory. An invocation outside Git or in an unrelated repository
uses an explicit warned fallback. Collision leads distinguish exact-file from shared-module evidence
and report whether their revisions match. Automatic worktree association may use a full issue ID or
an unambiguous numeric Bead suffix in the branch name; ambiguous associations require an explicit
operator mapping and otherwise remain unavailable.

Exact-file evidence may qualify from explicit or observed pointers. Shared-module evidence qualifies
only when at least one contributing pointer is an observed active-worktree change; explicit-only
module pairs are omitted from the primary queue and counted separately. This prevents broad directory
ownership from overwhelming the narrower collision signal while preserving observed and corroborated
module coordination.

Common explicit-only paths and modules are hub surfaces, analogous to broad semantic vocabulary.
When their active-record frequency exceeds the configured bound, reports summarize the surface and
the number of pairs it would have created rather than emitting every pair. A non-hub shared surface
or shared `path::symbol` can still qualify a pair. An exact path observed in either active worktree is
never suppressed by the hub guard.

`--explain-hub-guard [PAIRS]` (default 5 when bare) reports a bounded, deterministic sample of the
suppressed pairs together with the hub surfaces that suppressed them, so the guard can be audited
rather than trusted. The sample is drawn in issue-ID order and costs nothing when the flag is
absent.

The focused `collisions` command does not load an embedding model. Sweeps may include the same
analysis additively. A shared path is a prompt to coordinate before implementation or merge, never a
claim that the tasks have identical intent. Semantic code retrieval, AST indexing, and source
snippets remain outside the MVP and may be added only behind an optional evidence-provider contract.

## 12. Candidate-focused disposable batching

Batching operates on issues participating in review signals after structural filtering, ranking,
thresholds, and candidate caps have been applied. It does not force every active issue into a semantic
neighborhood merely because every vector has a nearest neighbor. Records without a qualifying signal
are reported separately as `no signal`.

Schema validation is necessary but not sufficient. The producer and consumer share a semantic
artifact validator that rejects duplicate or omitted members, non-partitioning review units,
evidence with no packaged endpoint, batches above the configured hard maximum, and disconnected
connected-component units. Its diagnostics use bounded positions and reason codes, not tracker text
or identifiers.

Broad container records such as epics are excluded from the default review population unless selected
explicitly. Parent/child similarity is structural context and potential counterevidence, not sufficient
evidence of overlap by itself.

Given a candidate graph and configured hard maximum `t`:

1. Find deterministic connected components over active candidate endpoints.
2. Emit components of at most `t` as connected review units.
3. Split larger components by growing a connected unit from a deterministic boundary seed, preferring
   nodes that keep the most candidate edges inside the unit; recursively process connected remainder
   components. This avoids cutting transitive bridge chains near their middle.
4. Pack independent singleton components into bounded agent envelopes, retaining each singleton as
   an explicit one-issue review unit rather than presenting the envelope as a semantic cluster.
5. Report component counts, fragmented components, singleton envelopes, maximum observed size, and
   candidate edges crossing artifacts.

No artifact may exceed `t`. Every multi-issue review unit has a connected induced candidate graph.
The implementation remains deterministic, candidate-only, and advisory; closed echo targets remain
evidence rather than artifact members.

## 13. Batch manifest

Each batch manifest is versioned JSON containing:

```json
{
  "schema_version": 1,
  "run_id": "...",
  "batch": 1,
  "kind": "connected-component",
  "review_units": [{"issue_ids": ["bd-1", "bd-2"]}],
  "snapshot": {
    "workspace_id": "...",
    "beads_version": "...",
    "source_revision": null
  },
  "policy": {
    "read_only": true,
    "implementation_allowed": false,
    "tracker_mutation_allowed": false
  },
  "issues": [],
  "neighbor_evidence": [],
  "review_rubric": []
}
```

`source_revision` is optional because Beads can operate without Git. Project-specific dispatchers may
add repository context separately without changing the core manifest.

## 14. Post-MVP agent dispatch

The shipped core CLI stops after producing manifests and reports. The local plugin foundation wraps
that synchronous CLI and does not add a scheduler or tracker-write authority. A future first-party
dispatch adapter could consume batch manifests if host-neutral integration proves insufficient.

A reviewer adapter must:

- give reviewers read-only tracker instructions;
- prohibit implementation and tracker mutation;
- request evidence for every proposed correction;
- preserve each batch's raw report;
- make partial failures visible;
- avoid requiring durable issues for disposable audit work.

Any future public dispatch interface should remain a command template or subprocess protocol rather
than a runtime-specific SDK. First-party adapters may later support multiple coding-agent CLIs.

## 15. Safety and privacy invariants

Automated tests must prove:

1. No executable path contains or invokes Beads mutation commands.
2. Tracker content is identical before and after every core command.
3. The default embedding provider performs no content-bearing network request.
4. First-run model downloads are pinned, disclosed, and separate from issue text.
5. Logs do not contain full issue bodies unless verbose output is explicitly requested.
6. Cache and report directories cannot be mistaken for Beads source data.
7. Concurrent synchronous runs cannot corrupt shared cache or artifact state.
8. Command failures return bounded diagnostics without mutating tracker or repository state.
9. Reports identify their tracker snapshot and embedding model.
10. Fixtures and documentation contain only synthetic public examples.

Post-MVP background execution would add separate status-transition and interrupted-run invariants;
those are not claims about the current synchronous CLI.

## 16. Configuration

Configuration precedence:

1. command-line flags;
2. environment variables;
3. user configuration directory;
4. built-in defaults.

No project-local configuration is required for the common path. Optional project configuration may
select structural filters or context-document discovery, but it must never contain vectors or model
artifacts and must not be needed merely to use Beads.

## 17. Output and observability

Every run records:

- start/end timestamps and duration;
- record counts by lifecycle;
- cache hits and misses;
- embedding model and revision;
- effective filters, thresholds, and target batch size;
- warnings and failures;
- paths to JSON and Markdown artifacts.

Machine output is stable and versioned. Human output favors short outcome language and links to the
full evidence. The CLI never treats a similarity score as a lifecycle verdict.

## 18. Packaging and support

- Python 3.11 or later.
- Installable as a normal package and user-level CLI with `uv tool`, `pipx`, or `pip`.
- Supported on current macOS, Linux, and Windows.
- MIT licensed.
- Version-checked release artifacts with published SHA-256 checksums.
- A pinned default model revision with its license linked from the public documentation.

The v0.4 build reproduced byte-for-byte under the same documented build inputs, but the build
toolchain is not yet fully pinned or independently attested. Do not describe the release as a
reproducible or provenance-attested build until those controls ship.

## 19. Validation strategy

Use synthetic Beads workspaces to test:

- unchanged, changed, new, closed, deferred, and deleted records;
- parent/child and dependency relationships;
- semantic duplicates with different vocabulary;
- related records that must not be classified as duplicates;
- malformed CLI JSON and unsupported Beads versions;
- cold, warm, and one-record incremental runs;
- concurrent synchronous sweeps and interrupted cache/artifact writes;
- deterministic batch membership and size bounds;
- candidate caps and structurally corroborated threshold exceptions;
- candidate-focused batching with explicit no-signal records and sparse tails;
- evidence-specific explanations and parent/child counterevidence;
- Windows-compatible cache locking;
- tracker hashes before and after commands.

Performance targets should be established on public synthetic corpora. Private project measurements
must not be published as fixtures, snapshots, logs, or examples; anonymized aggregate findings may be
retained with the repository owner's approval. The warm path for a public synthetic corpus of 1,000
records should complete within five seconds on a documented reference CPU, with similarity scoring and
batching measured separately from Beads acquisition.

## 20. Delivery boundary and roadmap

### Shipped foundation

- Package, Beads and Linear read adapters, canonicalization, pinned local provider, cache, and
  synchronous `neighbors`, `sweep`, `batch`, `triage`, and `collisions` commands.
- Stable versioned artifacts, bounded candidate queues, deterministic batching, incremental scopes,
  checkpoints, privacy tests, and cross-platform cache locking.
- Runtime-neutral JSON consumption plus a thin read-only Codex/Claude Code plugin foundation.

### Current hardening and evaluation

- Calibrate semantic ranking and review-budget behavior on public large-corpus surrogates and native
  Beads repositories without turning evaluation controls into default workflow complexity.
- Repeat observed-to-observed code-surface evaluation across additional repository layouts. The
  v0.4 gate passed with four genuinely active worktrees in this repository; that single result is
  not a universal precision or recall claim.
- Preserve compatibility and non-mutation guarantees while simplifying low-value or duplicative
  surfaces.

### Post-MVP, evidence-gated

- Async execution and `status` only if measured run duration or unattended operation warrants their
  lifecycle and storage complexity.
- First-party reviewer dispatch only if host-neutral manifests and the local plugin prove
  insufficient.
- Hosted providers, semantic code retrieval, AST indexing, or external vector stores only behind
  optional evidence-provider contracts with independent privacy and quality gates.
- Marketplace plugin publication and community-catalog submission after their host-workflow and
  contributor-readiness gates pass. The CLI already ships as a versioned GitHub release artifact;
  a package-index release remains separate distribution work.

## 21. Open decisions

1. Default local embedding model and upgrade policy.
2. Whether reports default to the user state directory or a temporary directory.
3. Whether project context-document discovery belongs in core or only in reviewer adapters.
4. Whether historical Dolt signals should be accepted from companion analytics tools.
5. Minimum supported Beads version and the exact read-only CLI contract.
