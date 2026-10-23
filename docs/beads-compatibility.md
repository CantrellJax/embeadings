# Beads compatibility contract

emBEADings v0.4.2 supports Beads **1.0.5 and newer** through a narrow, read-only CLI
contract. Version 1.0.5 is the oldest release exercised by the committed relationship fixture and by
the native-Beads evaluations; older releases are not claimed compatible.

The adapter executes exactly these command shapes:

```text
bd --readonly version --json
bd --readonly context --json
bd --readonly list --all --limit 0 --json
```

With `--fetch-comments` (`neighbors`, `packets`) it also runs, once per reported record whose listing
shows comments:

```text
bd --readonly comments ISSUE_ID --json
```

The ID must be a plain issue ID; `add` and any argument starting with `-` are refused before `bd` runs.
`created_at`, `comment_count`, and an inline `comments` array are read from the listing when present.

The installed `bd` must:

- report a semantic version at least `1.0.5`;
- return a context object containing a project/workspace identity or workspace path;
- return either an issue array or one recognized issue envelope;
- provide non-empty issue IDs, titles, and statuses;
- represent labels, parentage, and typed dependencies using one of the validated aliases; and
- preserve dependency direction by identifying the target separately from the source; and
- provide `close_reason` as a string when that optional lifecycle field is present.

The minimum version is an admission check, not a promise that every future Beads payload is
compatible. emBEADings continues to validate every response and fails closed on unknown or malformed
shapes. Run `embead doctor --json` to see the configured and resolved `bd` executable, detected
version, and an actionable failure before loading issue content.

## Bootstrap and partial snapshots

A child can legitimately reference a parent that is absent from the returned issue population
during bootstrap, filtering, or partial synchronization. emBEADings preserves that parent ID and
typed edge as external structural evidence; it does not fabricate a parent record or discard the
relationship. Self-dependencies and malformed targets still fail closed.

## Close dispositions

The optional Beads `close_reason` is retained as lifecycle metadata and participates in metadata-only
incremental fingerprints. It is not embedding input. Its text appears in a report only where a
reviewer needs the tracker's own words about a closed record: `match` resolution evidence, a closed
neighbor's `close_reason`, and `packets`; sweeps, triage, mentions, and checkpoints never carry it.
Completed-work echo ranking recognizes only three bounded, pair-local contradictions: work
explicitly rehomed or moved to the exact paired active ID and marked not completed/not done in the
same clause or an immediately following short sentence; a record marked `duplicate of` the exact
paired active ID (or superseded by it with explicit canonical-record retention); or work explicitly
merged, folded, or absorbed into that ID. Those semantic echo pairs are omitted before queue
selection and reported only as count-by-code diagnostics. Missing, generic, ambiguous, negated,
tentative, or different-ID reasons preserve ordinary completed-work echo behavior. A follow-up merely
filed or tracked by the active ID is not a recognized disposition. A close-reason-only change on a
closed endpoint re-evaluates its qualifying active/closed pairs in an incremental run. Linear does
not synthesize this Beads-specific field.

## What read-only means

emBEADings never asks Beads to create, update, synchronize, import, export, or otherwise mutate
tracker records. Logical source comparisons use canonical content digests rather than file
modification times.

This does **not** promise byte-for-byte or mtime stability for every file in Beads storage. The
embedded database may update physical metadata while servicing a read-only command. Evaluations
should therefore compare logical tracker exports/content hashes and Git-visible repository state,
and report physical mtime-only changes separately rather than treating them as tracker mutation.
