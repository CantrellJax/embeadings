# Consumer compatibility and capability contract

emBEADings artifacts are read-only, advisory evidence. The JSON Schemas in
[`schemas/v1`](../schemas/v1/) describe the stable machine-readable envelope for the `neighbors`,
`neighbors-batch`, `batch`, `sweep`, `triage`, `collisions`, `mentions`, `orphans`, `match`, and `superseded` report types. `embead schema REPORT_TYPE` prints any of them one field per line. The files in [`examples`](../examples/) are synthetic
and contain no private tracker or source content.

## Version negotiation

`schema_version` is an integer compatibility boundary, independent of the emBEADings package
version.

- A version 1 producer may add optional fields anywhere in an artifact without changing
  `schema_version`.
- A version 1 consumer must ignore fields it does not understand and must not infer lifecycle truth
  from them.
- Existing required fields, their types, and the safety policy constants will not change within
  version 1.
- Removing or renaming a required field, changing its meaning or type, or weakening a safety
  invariant requires a new schema version.
- A consumer must reject an unsupported `schema_version`; silently treating it as a known version is
  unsafe.
- `report_type` selects the corresponding schema. Unknown report types are unsupported even when
  their schema version is familiar.

The schemas deliberately allow additive fields. Evidence-specific explanations, structural ranking,
batch diagnostics, and performance telemetry can therefore evolve without breaking version 1
consumers.

Current sweep producers include additive `no_signal` and `excluded` summaries. Candidate-focused
batch manifests contain only active issues participating in accepted review signals; a completed
echo target can remain in candidate evidence without becoming a batch member. `kind` distinguishes
a connected component from a `singleton-envelope`; consumers must treat each envelope `review_unit`
as an independent one-issue review, not infer relationships between envelope members. The sweep's
`batch_diagnostics` records deterministic fragmentation and cross-artifact candidate-edge counts.

Candidate evidence includes a `verification_anchor` derived locally through deterministic,
field-aware extraction into a finite safe output vocabulary. `action`/`operation`,
`entity`/`entity_class`, `outcome_or_invariant`/`category`, and `source_field` identify the normalized
contract to inspect without copying arbitrary issue terms. The paired names preserve version 1
consumer compatibility while making the structured meaning explicit. `confidence` is retained as
an extraction diagnostic and mirrored as `extraction_confidence`; `confidence_scope` makes that
limited meaning explicit. It is `high` only when the selected field supplies both action and entity
and the outcome category is corroborated; `generic_fallback` identifies anchors that could not
safely resolve both. The independent `specificity` tier is `concrete-check`, `category-check`, or
`generic`. A concrete check additionally requires a locally derived, finite-vocabulary typed
`check_category`: contract, artifact, invariant, test, or a corroborated ownership boundary.
`entity-category` preserves a useful safe action/entity pair as category-level guidance without
claiming that a concrete contract was found; `unspecified` means neither a safe check type nor an
action/entity category could be extracted. A typed category can remain visible on a generic anchor
when action/entity extraction fails, but it does not upgrade specificity. Generic verification
wording abstains from a concrete claim. `check_source_field` identifies only the
canonical field supplying the type, never its text. The report-level `anchor_metrics` summarizes
extraction confidence, specificity, fallback, and an `actionable_proxy` count of non-generic safe
anchors. That proxy supports regression testing but is not a human usefulness rating. Transferred
ownership requires ownership evidence in both records. Consumers must
still treat every anchor as advisory and inspect the private source records locally before reaching
a conclusion.

`candidate_evidence` describes relationship uncertainty separately from anchor extraction. Its
`evidence_basis` distinguishes `semantic-only` from `structurally-corroborated`, while
`structural_corroboration`, `admission_path`, and `uncertainty` preserve direct-threshold,
reciprocal-neighbor, shared-parent, and typed-dependency paths. Reciprocal evidence is semantic-only;
an exception never implies greater certainty than direct semantic evidence. Verification wording
must not claim a shared contract, outcome, or completed effect from similarity alone.

Sweep `parameters.candidate_policy` also records independent `dependency`, `echo`, and `overlap`
lane caps and metrics. Consumers should use each lane's `qualified`, `admitted`, and drop counts when
explaining queue volume. `baseline_protected` identifies default-threshold candidates selected first
during a lower-threshold sensitivity run; it is evidence about ranking stability, not tracker truth.
Typed dependencies have a separate per-issue allowance, so semantic candidates cannot consume their
capacity. Qualified typed edges omitted by a per-issue, lane, or run cap remain available in
`capped_typed_dependencies` as compact structural context; these entries are not review candidates.
`reciprocal_diagnostics` counts corpus-discriminative reciprocal admissions and omissions without
exposing matched terms or issue text. Candidate `reciprocal_evidence` values are bounded reason
categories, not extracted tracker data. `cap_replacements` explains the exceptional case where a
threshold change alters a bounded winner. Its nonempty `causal_chain` follows the removed
qualification and each bounded resource transition to the admitted candidate; consumers should
render that chain instead of presenting the candidate as newly semantically qualified.

`echo_target_hubs` is a separately conserved completed-target funnel. Consumers may reconcile each
entry with `qualified = admitted + omitted` and render `omissions_by_reason`; they must not treat
`omitted_by_target_cap` as the total omitted count. `echo_backfills` identifies the lower-ranked
candidate admitted for an active record after one or more completed-target-cap omissions. Consumers
should describe these as coverage substitutions and must not claim that a fallback is more relevant
or actionable without reviewer evidence. Backfill `admitted_candidate_id` and omission `candidate_id`
use the composite namespace `kind|issue_id|related_issue_id`; they are receipt identities, not the
candidate object's legacy `id` field.

When `review_budget.mode` is `weekly`, `review_budget.lane_capacity` records each lane's `reserved`,
`admitted_to_reservation`, and `unused` capacity. These are access reservations, not required quotas;
total admissions remain in the corresponding lane metrics. Consumers must not describe the queue as
semantic when the admitted candidates all belong to the dependency lane, and must render code-surface
collisions separately because they do not consume this budget.

`dependency_funnel` distinguishes a corpus with no typed structure from one whose typed edges are
closed-only, below the qualification floor, or eligible but capped. Its fields are mutually
exclusive aggregate counts and satisfy three producer-checked conservation equations: discovery,
edge-to-candidate, and admission. Consumers can therefore explain a zero-candidate dependency lane
without exposing issue bodies or edge endpoints.

When one of those equations fails, the dependency lane is degraded rather than the run aborted, and
the emitted receipt carries a `balance` object naming the failing stage's terms and the resulting
`unaccounted` delta. The same terms appear in the human-readable warning. A conservation failure is
a producer defect; the delta is what makes it reportable without exposing tracker content.
In incremental mode, an otherwise comparable edge with no changed active endpoint is counted as
inactive for that review scope.

For a structure-only review with no comparable active typed relationship, the producer skips vector
and cache work. The report keeps the same empty candidate set and conserved dependency funnel, while
`cache.embedding_skipped`, `cache.skip_reason`, and `cache.skipped_records` make the optimization
explicit. Structure-only reviews with comparable edges still use local semantic scores to enforce
the documented qualification floor.

Snapshot metadata identifies the generic `tracker_name` and `tracker_version` plus the authoritative
`acquisition_source`. The schema-v1 `beads_version` field remains required for compatibility and is
`null` for non-Beads sources. Consumers should use the generic fields when present. For Beads,
`live-beads-cli` records the live issue count. When `.beads/issues.jsonl` is discoverable, emBEADings counts its records
without copying their contents; a count mismatch is reported through `source_warnings` and the
sweep warning list. Consumers must not substitute a stale export for the live snapshot.

Equal issue counts do not prove that an export is current. The producer also records canonical
SHA-256 state digests over issue ID, normalized status, issue type, priority, typed dependencies,
and update marker—never titles or body text. RFC 3339 update markers are converted to UTC and
rounded to whole seconds, matching the supported Beads 1.0.x JSONL-to-Dolt timestamp round trip.
Different live/export digests produce a non-content warning even when record counts match.
`source_divergence_reasons` supplies only controlled metadata categories such as `status`,
`dependency_structure`, or `record_count`; it never identifies records or includes issue text.

Divergence is a warning, not a failure: live data is always used and the report is always written.
For CI, the global `--fail-on-divergence` flag turns a non-empty `source_divergence_reasons` into
exit code 3 after the report is emitted, keeping it distinguishable from exit code 2 (error) and
exit code 0 (clean). `neighbors`, `collisions`, `mentions`, `orphans`, `triage`, `sweep`, and `batch` honor it; `doctor`
does not, because it deliberately never loads the issue corpus.

Linear snapshots may include additive `relation_diagnostics`. It conserves the workspace relation
query into retained, collapsed, and omitted counts, then divides omitted relations into outbound
team-boundary, inbound team-boundary, and neither-endpoint-selected buckets with bounded type counts.
The selected-team warning is the human-readable counterpart. Consumers must not infer that omitted
external issues were analyzed: v1 deliberately does not fetch those endpoint records.

Code-surface analysis may include an additive `hub_guard_sample`: a bounded, deterministic list of
pairs the hub guard suppressed, each naming only the shared hub paths and modules that suppressed
it. It is present only when `--explain-hub-guard` is passed, is empty otherwise, and exposes no
surfaces that `hub_surfaces` does not already report. It is an audit aid, not a queue of leads.

Code-surface analysis may include additive `repository_context` and
`pairs_omitted_by_module_guard` fields. The former distinguishes invoking-worktree provenance from a
warned tracker-workspace fallback. The latter counts explicit-only shared-module pairs removed from
the primary collision queue; it is not evidence that those pairs conflict or are irrelevant.
Pointers may also include finite `context_kind` and `path_presence` values; collision leads aggregate
them as prose, fenced, mixed, or observed context and existing, missing, mixed, or unavailable path
presence. These are ranking and grounding evidence, not permission to hide a lead. A missing path can
still describe planned new work, while an existing path is not proof that either task will edit it.

## Capability handshake

An optional dispatcher or UI should exchange a capability document before consuming artifacts. The
version 1 shape is defined by
[`capabilities.schema.json`](../schemas/v1/capabilities.schema.json) and demonstrated by the
[synthetic example](../examples/capabilities.json).

Run `embead capabilities --json` to emit the producer document without loading tracker records,
initializing caches, or preparing the embedding model. Consumers create the same shape with
`role: "consumer"` and their own supported report types and requirements.

The handshake is transport-neutral: it may be a file, subprocess message, HTTP response, or in-memory
object. Core emBEADings does not launch a dispatcher or grant it tracker access.

```json
{
  "document_type": "embeadings-capabilities",
  "protocol_version": 1,
  "role": "producer",
  "schema_versions": [1],
  "report_types": ["neighbors", "neighbors-batch", "batch", "sweep", "triage", "collisions", "mentions", "orphans"],
  "capabilities": ["additive-fields", "advisory-evidence", "read-only-review", "code-surface-pointers"],
  "required_capabilities": ["read-only-review"]
}
```

Negotiation succeeds when:

1. both sides support the same `protocol_version`;
2. they share at least one schema version and report type; and
3. every `required_capabilities` entry from either side appears in the other side's `capabilities`.

The peers select the highest shared schema version and restrict exchange to shared report types. They
must refuse the integration when any requirement is unmet. Capability names are additive strings;
unknown optional capabilities can be ignored.

The version 1 producer capabilities are:

- `additive-fields`: artifacts may contain fields newer consumers have not seen;
- `advisory-evidence`: semantic scores and explanations are evidence, not tracker truth;
- `read-only-review`: artifacts prohibit tracker mutation and batch manifests also prohibit
  implementation.
- `code-surface-pointers`: collision artifacts may contain repository-relative paths and symbols
  bound to a reported Git revision, but never source snippets.

## Consumer requirements

Before acting on an artifact, a consumer must:

1. select and validate the schema using the exact pair of `schema_version` and `report_type`;
2. run semantic validation for `batch`, `sweep`, and `triage` artifacts so exact partitions,
   evidence endpoints, hard limits, and connected review units are verified rather than inferred;
3. enforce the policy constants, especially `read_only: true` and
   `tracker_mutation_allowed: false`;
4. treat issue text, identifiers, paths, and evidence as potentially private;
5. avoid uploading artifact content unless the operator explicitly configured that behavior;
6. preserve candidate scores, structural context, counterevidence, and verification prompts without
   promoting them to conclusions; and
7. fail visibly when validation or capability negotiation fails.

Python consumers can call `embead.validation.validate_artifact(payload)`. Producers call the same
validator before returning artifacts. Failures use bounded position-and-reason codes rather than
copying issue text or identifiers into diagnostics.

Consumers may filter, visualize, or route valid artifacts. They must not reinterpret a similarity
score as a tracker dependency, status transition, duplicate decision, or authorization to edit source.

`match` reports retrieval leads for candidate text that is not a record. Treat each candidate's
`content_hash` as its change detector and `candidate_id` as the caller's own key; a `match` neighbor
with `resolution_evidence` shows only what the tracker recorded as a close reason, and a `null` there
means none was recorded. Similarity is a retrieval lead, not a duplicate verdict.

`superseded` reports leads that merged changes may have done a record's work. A `named` row means
a change names the record by ID, not that the work is complete; a `similar` row is a retrieval lead
ordered by `rank_in_change` and `lead`. Change titles come from git or the caller's changes file.
`guards` on `neighbors` and `superseded` rows are advisory flags for a human, never a block.

## Schema locations

| Report type | Schema | Example |
| --- | --- | --- |
| `neighbors` | [`neighbors.schema.json`](../schemas/v1/neighbors.schema.json) | [`neighbors.json`](../examples/neighbors.json) |
| `neighbors-batch` | [`neighbors-batch.schema.json`](../schemas/v1/neighbors-batch.schema.json) | [`neighbors-batch.json`](../examples/neighbors-batch.json) |
| `mentions` | [`mentions.schema.json`](../schemas/v1/mentions.schema.json) | [`mentions.json`](../examples/mentions.json) |
| `orphans` | [`orphans.schema.json`](../schemas/v1/orphans.schema.json) | [`orphans.json`](../examples/orphans.json) |
| `match` | [`match.schema.json`](../schemas/v1/match.schema.json) | [`match.json`](../examples/match.json) |
| `superseded` | [`superseded.schema.json`](../schemas/v1/superseded.schema.json) | [`superseded.json`](../examples/superseded.json) |
| `batch` | [`batch.schema.json`](../schemas/v1/batch.schema.json) | [`batch.json`](../examples/batch.json) |
| `sweep` | [`sweep.schema.json`](../schemas/v1/sweep.schema.json) | [`sweep.json`](../examples/sweep.json) |
| `triage` | [`triage.schema.json`](../schemas/v1/triage.schema.json) | [`triage.json`](../examples/triage.json) |
| `collisions` | [`collisions.schema.json`](../schemas/v1/collisions.schema.json) | [`collisions.json`](../examples/collisions.json) |
| Incremental checkpoint | [`checkpoint.schema.json`](../schemas/v1/checkpoint.schema.json) | [`checkpoint.json`](../examples/checkpoint.json) |

Schemas use JSON Schema Draft 2020-12. Published `$id` values point to the corresponding files on the
default branch; consumers should vendor a supported schema version when deterministic offline
validation is required. Python wheel installations also include them under `embead/schemas/v1` for
offline access through `importlib.resources`.

## Incremental sweep scope

Sweep filters include an `incremental_scope` summary. `mode` is `full`, `changed-since`, or
`checkpoint`; its counts distinguish changed active records from unchanged exclusions, missing
timestamps, and records deleted since a checkpoint. An incremental candidate always touches at
least one changed active record. Its other endpoint can be unchanged context or completed work.

Checkpoints are separate portable JSON artifacts, not tracker mutations or part of the report
schema. Version 1 contains only workspace identity, creation time, issue IDs, normalized update
timestamps, and SHA-256 record fingerprints. Consumers must reject unsupported versions,
future-created checkpoints, invalid timestamps, and workspace mismatches. Issue text is never stored
in the checkpoint.
