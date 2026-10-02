"""Command-line interface for emBEADings."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platformdirs import user_cache_path, user_state_path

from ._version import __version__
from .analysis import (
    MultiViewSimilarityIndex,
    SimilarityIndex,
    nearest_neighbors,
    package_candidate_batches,
)
from .artifacts import atomic_text, private_directory
from .beads import BeadsAdapter
from .cache import VectorCache
from .doctor import diagnose
from .explain import explain_candidate
from .identifiers import IssueIdResolver
from .incremental import (
    IncrementalScope,
    build_checkpoint,
    ensure_external_path,
    load_checkpoint,
    scope_since_timestamp,
)
from .linear import LinearAdapter
from .mentions import extract_claims
from .mentions import kind_counts as claim_kind_counts
from .models import IssueRecord, WorkspaceSnapshot, canonical_text, semantic_field_texts
from .orphans import (
    find_dangling_parents,
    find_parentless,
    group_parentless,
    is_live,
)
from .orphans import parent_status_counts as orphan_parent_status_counts
from .provider import HashingProvider, Model2VecProvider, provider_readiness
from .ranking import (
    NO_STRUCTURAL_LINK,
    CandidatePolicy,
    CandidateRanking,
    has_reviewable_typed_relationship,
    rank_candidates,
    structural_context,
)
from .reports import (
    build_batch_manifest,
    build_collisions_payload,
    build_mentions_payload,
    build_neighbors_batch_payload,
    build_neighbors_payload,
    build_orphans_payload,
    build_sweep_payload,
    build_triage_payload,
    describe_conservation_balance,
    render_batch_markdown,
    render_collisions_markdown,
    render_mentions_markdown,
    render_neighbors_batch_markdown,
    render_neighbors_markdown,
    render_orphans_markdown,
    render_sweep_markdown,
    render_triage_markdown,
)
from .surfaces import (
    CONFIDENCE_LEVELS,
    analyze_code_surfaces,
    compile_branch_patterns,
    parse_worktree_mappings,
)
from .trackers import TrackerAdapter, TrackerError

ACTIVE_STATUSES = {"open", "in_progress", "blocked", "deferred"}
CLOSED_STATUSES = {"closed", "done", "completed", "resolved"}
COLLISION_STATUSES = {"open", "in_progress", "blocked"}
REPORT_FILE_FORMATS = {
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
}
REVIEW_RUBRIC = (
    "Verify each candidate against current source, documentation, and shipped behavior.",
    "Record counterevidence when similar wording reflects different scope.",
    "Do not implement changes or mutate the tracker during this review.",
)
PRODUCER_CAPABILITIES = (
    "additive-fields",
    "advisory-evidence",
    "read-only-review",
    "code-surface-pointers",
)


DIVERGENCE_EXIT_CODE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embead",
        description="Find related tracker work without changing your tracker.",
        epilog="Read-only • Issue text is embedded locally by default.",
    )
    parser.add_argument("--version", action="version", version=f"embead {__version__}")
    parser.add_argument(
        "--source",
        choices=("beads", "linear"),
        default=os.environ.get("EMBEAD_SOURCE", "beads"),
        help="Read work from Beads (default) or a Linear team",
    )
    parser.add_argument(
        "--linear-team",
        default=os.environ.get("LINEAR_TEAM"),
        metavar="ID|KEY|NAME",
        help="Linear team ID, key, or exact name (or set LINEAR_TEAM)",
    )
    parser.add_argument(
        "--provider",
        choices=("model2vec", "hashing"),
        default=os.environ.get("EMBEAD_PROVIDER", "model2vec"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fail-on-divergence",
        action="store_true",
        help=(
            "Exit "
            f"{DIVERGENCE_EXIT_CODE} when live tracker data and the discoverable export disagree"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    neighbors = subparsers.add_parser("neighbors", help="Find nearest semantic neighbors")
    neighbors.add_argument(
        "issue_ids",
        nargs="*",
        metavar="ISSUE_ID",
        help="One or more seeds; several seeds share one tracker, model, and vector load",
    )
    neighbors.add_argument(
        "--ids-file",
        type=Path,
        metavar="FILE",
        help="Read additional seed IDs from FILE, one per line ('#' starts a comment)",
    )
    neighbors.add_argument("--limit", type=int, default=5)
    neighbors.add_argument("--include-closed", action="store_true")
    neighbors.add_argument(
        "--orphans-only",
        action="store_true",
        help=(
            "Keep only neighbors with no recorded structural link; --limit then counts "
            "surviving neighbors rather than ranked ones"
        ),
    )
    neighbors.add_argument(
        "--include-ephemeral",
        action="store_true",
        help="Include temporary Beads runtime records (excluded by default)",
    )
    neighbors.add_argument("--json", action="store_true", dest="as_json")
    neighbors.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="Atomically write this single report (JSON with --json; Markdown otherwise)",
    )

    mentions = subparsers.add_parser(
        "mentions",
        help=(
            "Find records whose text calls another a duplicate, successor, absorber, or fix "
            "without a typed link"
        ),
    )
    mentions.add_argument(
        "--include-linked",
        action="store_true",
        help="Also report claims already backed by a parent or dependency link",
    )
    mentions.add_argument(
        "--include-closed",
        action="store_true",
        help="Also report claims made by closed records (default: active claimants only)",
    )
    mentions.add_argument(
        "--include-mentions",
        action="store_true",
        help="Also report bare ID mentions that carry no lineage verb",
    )
    mentions.add_argument("--limit", type=int, default=50)
    _ephemeral_argument(mentions)
    mentions.add_argument("--json", action="store_true", dest="as_json")
    mentions.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="Atomically write this single report (JSON with --json; Markdown otherwise)",
    )

    orphans = subparsers.add_parser(
        "orphans",
        help="List live issues whose parent is closed or missing (no embedding model)",
    )
    orphans.add_argument(
        "--include-parentless",
        action="store_true",
        help="Also list live issues with no parent, grouped by issue type, in a separate section",
    )
    _ephemeral_argument(orphans)
    orphans.add_argument("--json", action="store_true", dest="as_json")
    orphans.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="Atomically write this single report (JSON with --json; Markdown otherwise)",
    )

    triage = subparsers.add_parser(
        "triage",
        help="Run an opinionated bounded coordination review",
    )
    triage.add_argument(
        "--review-budget",
        "--weekly-review-budget",
        dest="weekly_review_budget",
        type=int,
        default=20,
        metavar="CANDIDATES",
        help="Maximum semantic and structural review candidates (default: 20)",
    )
    triage.add_argument(
        "--size", type=int, default=9, help="Hard maximum issues per review artifact"
    )
    triage.add_argument("--status", action="append")
    triage.add_argument("--include-epics", action="store_true")
    _ephemeral_argument(triage)
    _incremental_arguments(triage)
    _code_surface_arguments(triage, opt_in=False)
    _multi_artifact_output_arguments(triage)
    triage.add_argument("--json", action="store_true", dest="as_json")
    triage.set_defaults(
        echo_threshold=0.72,
        overlap_threshold=0.82,
        exception_margin=0.08,
        reciprocal_rank=5,
        max_candidates_per_issue=3,
        max_echoes_per_target=2,
        max_echo_alternatives_per_active=3,
        max_dependency_candidates_per_issue=3,
        max_candidates=None,
        max_dependency_candidates=75,
        max_echo_candidates=125,
        max_overlap_candidates=125,
        objective=None,
        semantic_view="whole",
        code_surfaces=True,
    )

    sweep = subparsers.add_parser("sweep", help="Create disposable semantic review batches")
    sweep.add_argument(
        "--size", type=int, default=9, help="Hard maximum issues per agent review artifact"
    )
    _ephemeral_argument(sweep)
    sweep.add_argument(
        "--status",
        action="append",
        help="Include a stored status (repeatable; defaults to all active work)",
    )
    sweep.add_argument("--echo-threshold", type=float, default=0.72)
    sweep.add_argument("--overlap-threshold", type=float, default=0.82)
    sweep.add_argument(
        "--include-epics",
        action="store_true",
        help="Include epic/container records in candidate analysis (excluded by default)",
    )
    _candidate_policy_arguments(sweep)
    _semantic_retrieval_arguments(sweep)
    _incremental_arguments(sweep)
    _code_surface_arguments(sweep, opt_in=True)
    _multi_artifact_output_arguments(sweep)
    sweep.add_argument("--json", action="store_true", dest="as_json")

    batch = subparsers.add_parser("batch", help="Alias for a synchronous sweep")
    batch.add_argument(
        "--size", type=int, default=9, help="Hard maximum issues per agent review artifact"
    )
    batch.add_argument("--status", action="append")
    batch.add_argument("--echo-threshold", type=float, default=0.72)
    batch.add_argument("--overlap-threshold", type=float, default=0.82)
    batch.add_argument("--include-epics", action="store_true")
    _ephemeral_argument(batch)
    _candidate_policy_arguments(batch)
    _semantic_retrieval_arguments(batch)
    _incremental_arguments(batch)
    _code_surface_arguments(batch, opt_in=True)
    _multi_artifact_output_arguments(batch)
    batch.add_argument("--json", action="store_true", dest="as_json")

    readiness = subparsers.add_parser(
        "readiness",
        help="Prepare the local embedding model without reading tracker issues",
    )
    readiness.add_argument(
        "--offline",
        action="store_true",
        help="Require model artifacts to already exist in the configured local cache",
    )
    readiness.add_argument("--json", action="store_true", dest="as_json")

    capabilities = subparsers.add_parser(
        "capabilities",
        help=(
            "Describe the report contract and producer capabilities without reading tracker issues"
        ),
    )
    capabilities.add_argument("--json", action="store_true", dest="as_json")

    doctor = subparsers.add_parser(
        "doctor",
        help="Inspect source, Git, model, and cache readiness without changing state",
    )
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="Require pinned model artifacts to already exist in the local cache",
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")

    collisions = subparsers.add_parser(
        "collisions",
        help="Find active work that points at the same local code surfaces",
    )
    collisions.add_argument(
        "--status",
        action="append",
        help=(
            "Include a stored status (repeatable; defaults to concurrent open, in_progress, "
            "and blocked work; deferred is opt-in)"
        ),
    )
    collisions.add_argument("--include-epics", action="store_true")
    _ephemeral_argument(collisions)
    _code_surface_arguments(collisions, opt_in=False)
    collisions.add_argument(
        "--output",
        type=Path,
        metavar="FILE",
        help="Atomically write this single report (JSON with --json; Markdown otherwise)",
    )
    collisions.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _multi_artifact_output_arguments(parser: argparse.ArgumentParser) -> None:
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help=(
            "Write one .json/.md report file; any other path remains a "
            "backward-compatible artifact directory"
        ),
    )
    output.add_argument(
        "--output-dir",
        type=Path,
        metavar="DIRECTORY",
        help="Write the complete report and per-batch artifact set to DIRECTORY",
    )


def _candidate_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--exception-margin",
        type=float,
        default=0.08,
        help="Maximum below-threshold margin requiring corroborating evidence",
    )
    parser.add_argument(
        "--reciprocal-rank",
        type=int,
        default=5,
        help="Admit near-threshold pairs that mutually rank within this depth (0 disables)",
    )
    parser.add_argument("--max-candidates-per-issue", type=int, default=3)
    parser.add_argument(
        "--max-echoes-per-target",
        type=int,
        default=2,
        help=(
            "Maximum completed-work echoes that may point to the same closed record "
            "before deterministic backfill"
        ),
    )
    parser.add_argument(
        "--max-echo-alternatives-per-active",
        type=int,
        default=3,
        help="Bound completed-record fallbacks retained for each active issue",
    )
    parser.add_argument(
        "--max-dependency-candidates-per-issue",
        type=int,
        default=3,
        help="Independent per-issue allowance for typed dependency candidates",
    )
    total_budget = parser.add_mutually_exclusive_group()
    total_budget.add_argument("--max-candidates", type=int, default=None)
    total_budget.add_argument(
        "--weekly-review-budget",
        "--review-budget",
        type=int,
        metavar="CANDIDATES",
        help=(
            "Bound a weekly semantic queue with reserved access for dependency, echo, and "
            "overlap lanes; unused capacity reflows in dependency-to-echo-to-overlap order"
        ),
    )
    parser.add_argument(
        "--max-dependency-candidates",
        type=int,
        default=75,
        help="Independent budget for candidates backed by a typed dependency",
    )
    parser.add_argument("--max-echo-candidates", type=int, default=125)
    parser.add_argument("--max-overlap-candidates", type=int, default=125)


def _semantic_retrieval_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--objective",
        action="append",
        choices=("collision", "overlap", "echo", "structure"),
        help=(
            "Run an explicit review objective (repeatable). Selecting objectives separates "
            "typed structure from semantic novelty; omitted for legacy behavior."
        ),
    )
    parser.add_argument(
        "--semantic-view",
        choices=("whole", "fields"),
        default="whole",
        help=("Use the stable whole-record vector or experimental whole-plus-field retrieval"),
    )


def _incremental_arguments(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--changed-since",
        metavar="RFC3339",
        help="Review candidates touching active records updated after this timestamp",
    )
    scope.add_argument(
        "--since-checkpoint",
        type=Path,
        metavar="PATH",
        help="Review candidates touching active records changed since a prior checkpoint",
    )
    parser.add_argument(
        "--write-checkpoint",
        type=Path,
        metavar="PATH",
        help="Atomically write a portable metadata-only checkpoint outside the repository",
    )


def _ephemeral_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-ephemeral",
        action="store_true",
        help="Include temporary Beads runtime records (excluded by default)",
    )


def _code_surface_arguments(parser: argparse.ArgumentParser, *, opt_in: bool) -> None:
    if opt_in:
        parser.add_argument(
            "--code-surfaces",
            action="store_true",
            help="Add local explicit-reference and active-worktree collision evidence",
        )
    parser.add_argument(
        "--worktree-map",
        action="append",
        default=[],
        metavar="ISSUE_ID=PATH",
        help=(
            "Associate an active issue with a registered Git worktree; repeatable. "
            "Supplying a mapping enables code-surface analysis for sweeps."
        ),
    )
    parser.add_argument(
        "--branch-pattern",
        action="append",
        default=[],
        metavar="REGEX",
        help=(
            "Extra rule for reading an issue ID out of a worktree branch name; the ID is the "
            "named group 'id' (or group 1) and may be a short form such as abc12.4 or abc12-4. "
            "Repeatable."
        ),
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Local Git reference used to identify committed worktree changes",
    )
    parser.add_argument(
        "--min-confidence",
        choices=CONFIDENCE_LEVELS,
        default="explicit",
        help=(
            "Drop collision leads below this confidence: explicit keeps all, corroborated needs "
            "one side observed in a worktree, observed needs both (default: explicit)"
        ),
    )
    parser.add_argument(
        "--explain-hub-guard",
        type=int,
        nargs="?",
        const=5,
        default=0,
        metavar="PAIRS",
        help=(
            "Report up to this many suppressed pairs, with the hub surfaces that "
            "suppressed them, so the guard can be audited (default when bare: 5)"
        ),
    )
    parser.add_argument(
        "--max-hub-surface-issues",
        type=int,
        default=5,
        help=(
            "Summarize explicit-only paths/modules referenced by more than this many "
            "active records instead of emitting every pair"
        ),
    )


def _provider(name: str) -> Model2VecProvider | HashingProvider:
    if name == "hashing":
        return HashingProvider()
    return Model2VecProvider()


def _load_source(args: argparse.Namespace) -> tuple[WorkspaceSnapshot, tuple[IssueRecord, ...]]:
    adapter: TrackerAdapter
    if args.source == "linear":
        if not args.linear_team:
            raise ValueError("--linear-team or LINEAR_TEAM is required for the Linear source")
        adapter = LinearAdapter(team=args.linear_team)
    else:
        adapter = BeadsAdapter()
    return adapter.load()


def _divergence_exit(args: argparse.Namespace, snapshot: Any) -> int:
    """Report requested source divergence as a distinct, CI-usable exit code."""

    reasons = tuple(getattr(snapshot, "source_divergence_reasons", ()) or ())
    if not getattr(args, "fail_on_divergence", False) or not reasons:
        return 0
    for warning in getattr(snapshot, "source_warnings", ()) or ():
        print(f"embead: {warning}", file=sys.stderr)
    print(
        "embead: live tracker data diverges from the discoverable export "
        f"({', '.join(reasons)}); the report above used live data.",
        file=sys.stderr,
    )
    return DIVERGENCE_EXIT_CODE


def _workspace_paths(workspace_id: str) -> tuple[Path, Path]:
    namespace = workspace_id[:16]
    cache_root = user_cache_path("embeadings") / "vectors"
    state_root = user_state_path("embeadings") / "runs"
    private_directory(cache_root)
    private_directory(state_root)
    return (
        cache_root / namespace,
        state_root / namespace,
    )


def _load_vectors(
    issues: tuple[IssueRecord, ...],
    provider: Model2VecProvider | HashingProvider,
    cache: VectorCache,
) -> tuple[dict[str, list[float]], dict[str, int]]:
    texts = [canonical_text(issue) for issue in issues]
    keys = [
        cache.key_for(
            text,
            model_id=provider.model_id,
            model_revision=provider.model_revision,
        )
        for text in texts
    ]
    hits = sum(
        cache.get(
            key,
            model_id=provider.model_id,
            model_revision=provider.model_revision,
        )
        is not None
        for key in keys
    )
    if isinstance(provider, Model2VecProvider) and hits < len(issues):
        print(
            f"Preparing local model {provider.model_id}@{provider.model_revision[:12]}. "
            "Issue text is not uploaded.",
            file=sys.stderr,
        )
    vectors = cache.encode(texts, provider)
    return dict(zip((issue.id for issue in issues), vectors, strict=True)), {
        "hits": hits,
        "misses": len(issues) - hits,
    }


def _load_field_similarity_index(
    issues: tuple[IssueRecord, ...],
    provider: Model2VecProvider | HashingProvider,
    cache: VectorCache,
    whole_record: SimilarityIndex,
) -> tuple[MultiViewSimilarityIndex, dict[str, Any]]:
    """Load addressable field views without changing the stable whole-record cache."""

    field_vectors: dict[str, dict[str, list[float]]] = {}
    field_hits = field_misses = 0
    view_counts: dict[str, int] = {}
    for field in ("title", "description", "acceptance_criteria", "design"):
        entries = [
            (issue.id, text) for issue in issues if (text := semantic_field_texts(issue).get(field))
        ]
        if not entries:
            continue
        canonicalization_version = f"field-aware-v1:{field}"
        keys = [
            cache.key_for(
                text,
                model_id=provider.model_id,
                model_revision=provider.model_revision,
                canonicalization_version=canonicalization_version,
            )
            for _identifier, text in entries
        ]
        hits = sum(
            cache.get(
                key,
                model_id=provider.model_id,
                model_revision=provider.model_revision,
            )
            is not None
            for key in keys
        )
        field_hits += hits
        field_misses += len(entries) - hits
        vectors = cache.encode(
            [text for _identifier, text in entries],
            provider,
            canonicalization_version=canonicalization_version,
        )
        field_vectors[field] = dict(
            zip((identifier for identifier, _text in entries), vectors, strict=True)
        )
        view_counts[field] = len(entries)
    return MultiViewSimilarityIndex(whole_record, field_vectors), {
        "field_hits": field_hits,
        "field_misses": field_misses,
        "field_view_counts": view_counts,
        "semantic_view": "fields",
    }


def _objective_for_candidate(candidate: dict[str, Any]) -> str:
    if candidate["lane"] == "dependency":
        return "structure"
    return "echo" if candidate["kind"] == "completed-work-echo" else "overlap"


def _retrieval_provenance(
    candidate: dict[str, Any],
    index: SimilarityIndex | MultiViewSimilarityIndex,
    *,
    active_ids: list[str],
    closed_ids: list[str],
) -> dict[str, Any]:
    left_id = candidate["issue_id"]
    right_id = candidate["related_issue_id"]
    if isinstance(index, MultiViewSimilarityIndex):
        channel_scores = index.channel_scores(left_id, right_id)
        selection_rule = "max-semantic-view"
    else:
        channel_scores = (("whole_record", index.score(left_id, right_id)),)
        selection_rule = "whole-record-cosine"
    forward_pool = closed_ids if candidate["kind"] == "completed-work-echo" else active_ids
    reverse_pool = active_ids
    best_score = max(score for _channel, score in channel_scores)
    receipts = []
    for channel, score in channel_scores:
        if isinstance(index, MultiViewSimilarityIndex):
            forward_rank = index.channel_rank(
                channel, left_id, right_id, [item for item in forward_pool if item != left_id]
            )
            reverse_rank = index.channel_rank(
                channel, right_id, left_id, [item for item in reverse_pool if item != right_id]
            )
        else:
            forward_rank = _rank_in_channel(
                index, left_id, right_id, [item for item in forward_pool if item != left_id]
            )
            reverse_rank = _rank_in_channel(
                index, right_id, left_id, [item for item in reverse_pool if item != right_id]
            )
        receipts.append(
            {
                "channel": (
                    "whole-record-semantic"
                    if channel == "whole_record"
                    else f"field-semantic:{channel}"
                ),
                "evidence_family": "tracker-text",
                "pair_score": round(score, 6),
                "selected": math.isclose(score, best_score, abs_tol=1e-12),
                "ranks": {
                    "issue_to_related": forward_rank,
                    "related_to_issue": reverse_rank,
                },
            }
        )
    relationship = candidate.get("dependency_evidence")
    if isinstance(relationship, dict):
        receipts.append(
            {
                "channel": "typed-tracker-relationship",
                "evidence_family": "tracker-structure",
                "relationship_type": relationship.get("type"),
                "selected": candidate["lane"] == "dependency",
            }
        )
    return {
        "selection_rule": selection_rule,
        "channels": receipts,
    }


def _rank_in_channel(
    index: SimilarityIndex,
    query_id: str,
    target_id: str,
    candidate_ids: list[str],
) -> int | None:
    return next(
        (
            position
            for position, (identifier, _score) in enumerate(
                index.ranked(query_id, candidate_ids), start=1
            )
            if identifier == target_id
        ),
        None,
    )


def _structural_context(left: IssueRecord, right: IssueRecord) -> str:
    return structural_context(left, right)


def _issue_summary(issue: IssueRecord) -> dict[str, Any]:
    return {
        "id": issue.id,
        "title": issue.title,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "priority": issue.priority,
        "labels": list(issue.labels),
        "parent_id": issue.parent_id,
        "dependencies": list(issue.dependencies),
        "dependency_links": [asdict(link) for link in issue.dependency_links],
    }


def _model_metadata(provider: Model2VecProvider | HashingProvider) -> dict[str, str]:
    return {"model_id": provider.model_id, "model_revision": provider.model_revision}


def _readiness(args: argparse.Namespace) -> int:
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    result = provider_readiness(_provider(args.provider))
    payload = {
        "readiness_version": 1,
        **result,
        "corpus_loaded": False,
        "network_policy": "offline" if args.offline else "prefetch-allowed",
    }
    if args.as_json:
        sys.stdout.write(_json_text(payload))
    else:
        sys.stdout.write(
            "Embedding model ready\n"
            f"Model: {payload['model_id']}@{payload['model_revision']}\n"
            f"Vector dimension: {payload['vector_dimension']}\n"
            "Corpus loaded: no\n"
        )
    return 0


def _capabilities(args: argparse.Namespace) -> int:
    payload = {
        "document_type": "embeadings-capabilities",
        "protocol_version": 1,
        "role": "producer",
        "schema_versions": [1],
        "report_types": [
            "neighbors",
            "neighbors-batch",
            "batch",
            "sweep",
            "triage",
            "collisions",
            "mentions",
            "orphans",
        ],
        "capabilities": list(PRODUCER_CAPABILITIES),
        "required_capabilities": ["read-only-review"],
    }
    if args.as_json:
        sys.stdout.write(_json_text(payload))
    else:
        sys.stdout.write(
            "emBEADings producer capabilities\n"
            "Protocol: 1\n"
            "Schemas: 1\n"
            f"Reports: {', '.join(payload['report_types'])}\n"
            f"Capabilities: {', '.join(payload['capabilities'])}\n"
            "Required: read-only-review\n"
        )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    payload = diagnose(
        source=args.source,
        linear_team=args.linear_team,
        provider=_provider(args.provider),
        offline=args.offline,
    )
    if args.as_json:
        sys.stdout.write(_json_text(payload))
    else:
        sys.stdout.write(
            f"emBEADings doctor: {payload['status']}\n"
            f"Source: {payload['source']['status']} — {payload['source']['detail']}\n"
            f"Repository: {payload['repository']['status']} — "
            f"{payload['repository']['detail']}\n"
            f"Embedding: {payload['embedding']['status']} — "
            f"{payload['embedding']['detail']}\n"
            f"Cache: {payload['cache']['status']} — {payload['cache']['detail']}\n"
        )
    return 2 if payload["status"] == "blocked" else 0


def _surface_analysis(
    args: argparse.Namespace,
    snapshot: Any,
    issues: list[IssueRecord],
    eligible_issue_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    mappings = parse_worktree_mappings(args.worktree_map)
    analysis = analyze_code_surfaces(
        issues,
        workspace_path=snapshot.workspace_path,
        invocation_path=Path.cwd(),
        worktree_mappings=mappings,
        base_reference=args.base_ref,
        hub_surface_limit=args.max_hub_surface_issues,
        hub_guard_sample_limit=args.explain_hub_guard,
        eligible_issue_ids=eligible_issue_ids,
        branch_patterns=compile_branch_patterns(getattr(args, "branch_pattern", ())),
        min_confidence=getattr(args, "min_confidence", "explicit"),
    )
    return analysis.to_dict()


def _collisions(args: argparse.Namespace) -> int:
    snapshot, issues = _load_source(args)
    statuses = {status.casefold() for status in (args.status or COLLISION_STATUSES)}
    population = [
        issue
        for issue in issues
        if issue.status.casefold() in statuses
        and (args.include_epics or issue.issue_type.casefold() != "epic")
        and (args.include_ephemeral or not issue.ephemeral)
    ]
    analysis = _surface_analysis(args, snapshot, population)
    payload = build_collisions_payload(
        analysis,
        snapshot=asdict(snapshot),
        filters={
            "status": sorted(statuses),
            "include_epics": args.include_epics,
            "include_ephemeral": args.include_ephemeral,
            "min_confidence": args.min_confidence,
        },
    )
    rendered = _json_text(payload) if args.as_json else render_collisions_markdown(payload)
    if args.output:
        _atomic_text(args.output, rendered)
    sys.stdout.write(rendered)
    return _divergence_exit(args, snapshot)


def _atomic_text(path: Path, text: str) -> None:
    atomic_text(path, text)


def _json_text(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _neighbor_seed_ids(args: argparse.Namespace) -> list[str]:
    seeds = list(args.issue_ids)
    if args.ids_file:
        for line in args.ids_file.read_text(encoding="utf-8").splitlines():
            if identifier := line.split("#", 1)[0].strip():
                seeds.append(identifier)
    seeds = list(dict.fromkeys(seeds))
    if not seeds:
        raise ValueError("neighbors needs at least one ISSUE_ID or --ids-file")
    return seeds


def _neighbor_evidence(
    args: argparse.Namespace,
    seed: IssueRecord,
    issues: tuple[IssueRecord, ...],
    by_id: dict[str, IssueRecord],
    vectors: dict[str, list[float]],
    similarity_index: SimilarityIndex,
    pool: Any,
    resolver: IssueIdResolver,
) -> list[dict[str, Any]]:
    ranked = nearest_neighbors(
        seed,
        issues,
        vectors,
        # Orphan filtering is a property of the pair, not of the ranking, so the
        # limit has to apply to the surviving neighbors. Rank everything first.
        limit=(len(issues) if args.limit else 0) if args.orphans_only else args.limit,
        include_closed=args.include_closed,
        similarity_index=similarity_index,
    )
    evidence = []
    for rank, neighbor in enumerate(ranked, start=1):
        related = by_id[neighbor.issue_id]
        context = _structural_context(seed, related)
        if args.orphans_only and context != NO_STRUCTURAL_LINK:
            continue
        claims = extract_claims((seed, related), (seed, related), resolver=resolver)
        evidence.append(
            {
                **_issue_summary(related),
                "similarity": round(neighbor.similarity, 6),
                "rank": rank,
                # Where the seed ranks among this neighbor's own neighbors.
                "reverse_rank": similarity_index.rank_of(related.id, seed.id, pool),
                "structural_context": context,
                "text_claims": [
                    {
                        "issue_id": claim.issue_id,
                        "related_issue_id": claim.related_issue_id,
                        "kind": claim.kind,
                    }
                    for claim in claims
                ],
            }
        )
        if args.orphans_only and len(evidence) >= args.limit:
            break
    return evidence


def _neighbors(args: argparse.Namespace) -> int:
    seeds = _neighbor_seed_ids(args)
    if args.limit < 0:
        raise ValueError("--limit cannot be negative")
    snapshot, issues = _load_source(args)
    if not args.include_ephemeral:
        issues = tuple(issue for issue in issues if not issue.ephemeral)
    by_id = {issue.id: issue for issue in issues}
    missing = [seed for seed in seeds if seed not in by_id]
    if missing:
        raise ValueError(f"issue not found: {', '.join(missing)}")
    provider = _provider(args.provider)
    cache_path, _ = _workspace_paths(snapshot.workspace_id)
    vectors, cache_stats = _load_vectors(issues, provider, VectorCache(cache_path))
    similarity_index = SimilarityIndex(vectors)
    # The reverse rank uses the same candidate pool the forward ranking did.
    pool = similarity_index.positions(
        [
            issue.id
            for issue in issues
            if args.include_closed or issue.status.casefold() not in CLOSED_STATUSES
        ]
    )
    resolver = IssueIdResolver(by_id)
    results = [
        (
            _issue_summary(by_id[seed]),
            _neighbor_evidence(
                args, by_id[seed], issues, by_id, vectors, similarity_index, pool, resolver
            ),
        )
        for seed in seeds
    ]
    filters = {
        "include_closed": bool(args.include_closed),
        "structural_link": "none-recorded" if args.orphans_only else "any",
        "limit": args.limit,
    }
    if len(results) == 1:
        payload = build_neighbors_payload(
            results[0][0],
            results[0][1],
            snapshot=asdict(snapshot),
            model=_model_metadata(provider),
            cache=cache_stats,
            filters=filters,
        )
        rendered = _json_text(payload) if args.as_json else render_neighbors_markdown(payload)
    else:
        payload = build_neighbors_batch_payload(
            results,
            snapshot=asdict(snapshot),
            model=_model_metadata(provider),
            cache=cache_stats,
            filters=filters,
        )
        rendered = _json_text(payload) if args.as_json else render_neighbors_batch_markdown(payload)
    if args.output:
        _atomic_text(args.output, rendered)
    sys.stdout.write(rendered)
    return _divergence_exit(args, snapshot)


def _mentions(args: argparse.Namespace) -> int:
    if args.limit < 0:
        raise ValueError("--limit cannot be negative")
    snapshot, issues = _load_source(args)
    if not args.include_ephemeral:
        issues = tuple(issue for issue in issues if not issue.ephemeral)
    by_id = {issue.id: issue for issue in issues}
    claimants = [
        issue
        for issue in issues
        if args.include_closed or issue.status.casefold() in ACTIVE_STATUSES
    ]
    found = extract_claims(claimants, issues, include_mentions=args.include_mentions)
    selected = [claim for claim in found if args.include_linked or not claim.typed_link]
    shown = selected[: args.limit]
    payload = build_mentions_payload(
        [
            {
                **asdict(claim),
                "issue_status": by_id[claim.issue_id].status,
                "issue_title": by_id[claim.issue_id].title,
                "related_status": by_id[claim.related_issue_id].status,
                "related_title": by_id[claim.related_issue_id].title,
            }
            for claim in shown
        ],
        snapshot=asdict(snapshot),
        filters={
            "include_linked": bool(args.include_linked),
            "include_closed": bool(args.include_closed),
            "include_mentions": bool(args.include_mentions),
            "include_ephemeral": bool(args.include_ephemeral),
            "limit": args.limit,
        },
        summary={
            "claimants_scanned": len(claimants),
            "claims_found": len(selected),
            "claims_with_typed_link": sum(claim.typed_link for claim in found),
            "kind_counts": claim_kind_counts(selected),
            "omitted_by_limit": len(selected) - len(shown),
        },
    )
    rendered = _json_text(payload) if args.as_json else render_mentions_markdown(payload)
    if args.output:
        _atomic_text(args.output, rendered)
    sys.stdout.write(rendered)
    return _divergence_exit(args, snapshot)


def _orphans(args: argparse.Namespace) -> int:
    snapshot, issues = _load_source(args)
    # Parents are looked up in the full listing; ephemeral filtering only narrows what is reported.
    reportable = [issue for issue in issues if args.include_ephemeral or not issue.ephemeral]
    dangling = find_dangling_parents(issues, reportable=reportable)
    parentless = find_parentless(reportable) if args.include_parentless else ()
    payload = build_orphans_payload(
        dangling,
        snapshot=asdict(snapshot),
        filters={
            "include_parentless": bool(args.include_parentless),
            "include_ephemeral": bool(args.include_ephemeral),
        },
        summary={
            "live_issues_scanned": sum(is_live(issue) for issue in reportable),
            "dangling_parent_count": len(dangling),
            "parent_status_counts": orphan_parent_status_counts(dangling),
            "parentless_count": len(parentless),
        },
        parentless=group_parentless(parentless),
    )
    rendered = _json_text(payload) if args.as_json else render_orphans_markdown(payload)
    if args.output:
        _atomic_text(args.output, rendered)
    sys.stdout.write(rendered)
    return _divergence_exit(args, snapshot)


def _candidate_evidence(
    population: list[IssueRecord],
    all_issues: tuple[IssueRecord, ...],
    vectors: dict[str, list[float]],
    *,
    echo_threshold: float,
    overlap_threshold: float,
    similarity_index: SimilarityIndex | MultiViewSimilarityIndex | None = None,
    exception_margin: float = 0.08,
    reciprocal_rank: int = 5,
    max_candidates_per_issue: int = 3,
    max_echoes_per_target: int = 2,
    max_echo_alternatives_per_active: int = 3,
    max_dependency_candidates_per_issue: int = 3,
    max_candidates: int = 250,
    max_dependency_candidates: int = 75,
    max_echo_candidates: int = 125,
    max_overlap_candidates: int = 125,
    lane_reservations: dict[str, int] | None = None,
    eligible_issue_ids: frozenset[str] | None = None,
    objectives: frozenset[str] | None = None,
) -> CandidateRanking:
    ranking = rank_candidates(
        population,
        all_issues,
        similarity_index or SimilarityIndex(vectors),
        CandidatePolicy(
            echo_threshold=echo_threshold,
            overlap_threshold=overlap_threshold,
            exception_margin=exception_margin,
            reciprocal_rank=reciprocal_rank,
            max_per_issue=max_candidates_per_issue,
            max_echoes_per_target=max_echoes_per_target,
            max_echo_alternatives_per_active=max_echo_alternatives_per_active,
            max_dependencies_per_issue=max_dependency_candidates_per_issue,
            max_total=max_candidates,
            max_dependencies=max_dependency_candidates,
            max_echoes=max_echo_candidates,
            max_overlaps=max_overlap_candidates,
            lane_reservations=lane_reservations,
            objectives=objectives,
        ),
        eligible_issue_ids=eligible_issue_ids,
    )
    by_id = {issue.id: issue for issue in all_issues}
    active_ids = sorted(issue.id for issue in population)
    closed_ids = sorted(
        issue.id
        for issue in all_issues
        if issue.status.casefold() in {"closed", "done", "completed", "resolved"}
    )
    explained = []
    for candidate in ranking.candidates:
        left = by_id[candidate["issue_id"]]
        right = by_id[candidate["related_issue_id"]]
        enriched = {
            **candidate,
            **explain_candidate(
                left,
                right,
                kind=candidate["kind"],
                similarity=candidate["similarity"],
                structural_context=candidate["structural_context"],
            ),
        }
        if objectives is not None or isinstance(similarity_index, MultiViewSimilarityIndex):
            enriched.update(
                {
                    "objective": _objective_for_candidate(candidate),
                    "retrieval_provenance": _retrieval_provenance(
                        candidate,
                        similarity_index or SimilarityIndex(vectors),
                        active_ids=active_ids,
                        closed_ids=closed_ids,
                    ),
                }
            )
        explained.append(enriched)
    return CandidateRanking(
        candidates=tuple(explained),
        qualified=ranking.qualified,
        dropped_by_issue_cap=ranking.dropped_by_issue_cap,
        dropped_by_run_cap=ranking.dropped_by_run_cap,
        lanes=ranking.lanes,
        baseline_protected=ranking.baseline_protected,
        dropped_by_lane_cap=ranking.dropped_by_lane_cap,
        dropped_by_dependency_issue_cap=ranking.dropped_by_dependency_issue_cap,
        dropped_by_echo_target_cap=ranking.dropped_by_echo_target_cap,
        capped_typed_dependencies=ranking.capped_typed_dependencies,
        echo_target_hubs=ranking.echo_target_hubs,
        echo_backfills=ranking.echo_backfills,
        reciprocal_diagnostics=ranking.reciprocal_diagnostics,
        cap_replacements=ranking.cap_replacements,
        dependency_funnel=ranking.dependency_funnel,
        degradation_receipts=ranking.degradation_receipts,
        echo_disposition_omissions=ranking.echo_disposition_omissions,
    )


def _review_lane_reservations(limit: int, lane_caps: dict[str, int]) -> dict[str, int]:
    """Reserve a structural majority while guaranteeing semantic lane access.

    The reservations are minimum access, not quotas. Any unused capacity is
    returned to the normal dependency -> echo -> overlap priority pass.
    """

    if limit == 1:
        requested = {"dependency": 0, "echo": 0, "overlap": 1}
    elif limit == 2:
        requested = {"dependency": 1, "echo": 0, "overlap": 1}
    else:
        echo = max(1, limit // 5)
        overlap = max(1, limit // 5)
        requested = {
            "dependency": limit - echo - overlap,
            "echo": echo,
            "overlap": overlap,
        }
    return {lane: min(requested[lane], lane_caps[lane]) for lane in requested}


def _sweep_output_paths(
    args: argparse.Namespace,
    state_path: Path,
    run_id: str,
) -> tuple[Path | None, Path | None, str | None]:
    """Resolve a complete artifact directory or one explicit primary report file."""

    output = args.output
    output_dir = args.output_dir
    if output_dir is not None:
        return output_dir, None, None
    if output is not None:
        report_format = REPORT_FILE_FORMATS.get(output.suffix.casefold())
        if report_format is not None:
            return None, output, report_format
        return output, None, None
    return state_path / run_id, None, None


def _sweep(args: argparse.Namespace) -> int:
    if args.size < 1:
        raise ValueError("--size must be positive")
    max_candidates = (
        args.weekly_review_budget
        if args.weekly_review_budget is not None
        else args.max_candidates
        if args.max_candidates is not None
        else 250
    )
    objectives = frozenset(args.objective) if args.objective else None
    lane_caps = {
        "dependency": args.max_dependency_candidates,
        "echo": args.max_echo_candidates,
        "overlap": args.max_overlap_candidates,
    }
    if objectives is not None:
        lane_caps = {
            "dependency": lane_caps["dependency"] if "structure" in objectives else 0,
            "echo": lane_caps["echo"] if "echo" in objectives else 0,
            "overlap": lane_caps["overlap"] if "overlap" in objectives else 0,
        }
    lane_reservations = (
        _review_lane_reservations(max_candidates, lane_caps)
        if args.weekly_review_budget is not None
        else None
    )
    policy = CandidatePolicy(
        echo_threshold=args.echo_threshold,
        overlap_threshold=args.overlap_threshold,
        exception_margin=args.exception_margin,
        reciprocal_rank=args.reciprocal_rank,
        max_per_issue=args.max_candidates_per_issue,
        max_echoes_per_target=args.max_echoes_per_target,
        max_echo_alternatives_per_active=args.max_echo_alternatives_per_active,
        max_dependencies_per_issue=args.max_dependency_candidates_per_issue,
        max_total=max_candidates,
        max_dependencies=lane_caps["dependency"],
        max_echoes=lane_caps["echo"],
        max_overlaps=lane_caps["overlap"],
        lane_reservations=lane_reservations,
        objectives=objectives,
    )
    policy.validate()
    started = time.monotonic()
    phase_started = started
    snapshot, issues = _load_source(args)
    review_issues = (
        issues
        if args.include_ephemeral
        else tuple(issue for issue in issues if not issue.ephemeral)
    )
    excluded_ephemeral = [
        issue for issue in issues if issue.ephemeral and not args.include_ephemeral
    ]
    acquisition_ms = round((time.monotonic() - phase_started) * 1000)
    if args.write_checkpoint:
        ensure_external_path(
            args.write_checkpoint,
            snapshot.workspace_path,
            purpose="checkpoint output",
        )
    if args.since_checkpoint:
        ensure_external_path(
            args.since_checkpoint,
            snapshot.workspace_path,
            purpose="checkpoint input",
        )
    statuses = {status.casefold() for status in (args.status or ACTIVE_STATUSES)}
    selected_population = [issue for issue in review_issues if issue.status.casefold() in statuses]
    excluded_epics = [
        issue
        for issue in selected_population
        if not args.include_epics and issue.issue_type.casefold() == "epic"
    ]
    excluded_ids = {issue.id for issue in excluded_epics}
    population = [issue for issue in selected_population if issue.id not in excluded_ids]
    scope: IncrementalScope | None = None
    if args.changed_since:
        scope = scope_since_timestamp(issues, args.changed_since)
    elif args.since_checkpoint:
        scope = load_checkpoint(
            args.since_checkpoint,
            issues,
            workspace_id=snapshot.workspace_id,
        )
    active_scope_ids = (
        frozenset(issue.id for issue in population if issue.id in scope.changed_ids)
        if scope is not None
        else None
    )
    ranking_scope_ids = scope.changed_ids if scope is not None else None
    code_surface_payload: dict[str, Any] | None = None
    code_surface_ms = 0
    if args.code_surfaces or args.worktree_map or (objectives and "collision" in objectives):
        phase_started = time.monotonic()
        code_surface_payload = _surface_analysis(
            args,
            snapshot,
            population,
            active_scope_ids,
        )
        code_surface_ms = round((time.monotonic() - phase_started) * 1000)
    skip_semantic_vectors = objectives == frozenset({"structure"}) and not (
        has_reviewable_typed_relationship(
            population,
            review_issues,
            eligible_issue_ids=active_scope_ids,
        )
    )
    provider = _provider(args.provider)
    cache_path, state_path = _workspace_paths(snapshot.workspace_id)
    phase_started = time.monotonic()
    if skip_semantic_vectors:
        # A non-zero sentinel keeps the ranking and packaging interfaces stable.
        # No candidate can consume it because the structural comparability gate
        # proved that this review has no active typed relationship to qualify.
        vectors = {issue.id: [1.0] for issue in review_issues}
        cache_stats = {
            "hits": 0,
            "misses": 0,
            "embedding_skipped": True,
            "skip_reason": "no-reviewable-typed-relationships",
            "skipped_records": len(review_issues),
        }
    else:
        vectors, cache_stats = _load_vectors(review_issues, provider, VectorCache(cache_path))
    embedding_ms = round((time.monotonic() - phase_started) * 1000)
    phase_started = time.monotonic()
    whole_record_index = SimilarityIndex(vectors)
    similarity_scoring_ms = round((time.monotonic() - phase_started) * 1000)
    similarity_index: SimilarityIndex | MultiViewSimilarityIndex = whole_record_index
    if args.semantic_view == "fields" and not skip_semantic_vectors:
        phase_started = time.monotonic()
        similarity_index, field_cache_stats = _load_field_similarity_index(
            review_issues,
            provider,
            VectorCache(cache_path),
            whole_record_index,
        )
        cache_stats.update(field_cache_stats)
        embedding_ms += round((time.monotonic() - phase_started) * 1000)
    phase_started = time.monotonic()
    ranking = _candidate_evidence(
        population,
        review_issues,
        vectors,
        echo_threshold=args.echo_threshold,
        overlap_threshold=args.overlap_threshold,
        similarity_index=similarity_index,
        exception_margin=args.exception_margin,
        reciprocal_rank=args.reciprocal_rank,
        max_candidates_per_issue=args.max_candidates_per_issue,
        max_echoes_per_target=args.max_echoes_per_target,
        max_echo_alternatives_per_active=args.max_echo_alternatives_per_active,
        max_dependency_candidates_per_issue=args.max_dependency_candidates_per_issue,
        max_candidates=max_candidates,
        max_dependency_candidates=lane_caps["dependency"],
        max_echo_candidates=lane_caps["echo"],
        max_overlap_candidates=lane_caps["overlap"],
        lane_reservations=lane_reservations,
        eligible_issue_ids=ranking_scope_ids,
        objectives=objectives,
    )
    candidates = list(ranking.candidates)
    candidate_analysis_ms = round((time.monotonic() - phase_started) * 1000)
    phase_started = time.monotonic()
    packaging = package_candidate_batches(
        population,
        candidates,
        vectors,
        max_batch_size=args.size,
        similarity_index=whole_record_index,
    )
    batches = packaging.batches
    batching_ms = round((time.monotonic() - phase_started) * 1000)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    run_dir, report_file, report_file_format = _sweep_output_paths(
        args,
        state_path,
        run_id,
    )
    if args.output is None and args.output_dir is None:
        private_directory(state_path)
    if run_dir is not None:
        private_directory(run_dir)
    manifests: list[dict[str, Any]] = []
    model = _model_metadata(provider)
    snapshot_payload = asdict(snapshot)
    for index, review_batch in enumerate(batches, start=1):
        batch_issues = review_batch.issues
        member_ids = {issue.id for issue in batch_issues}
        batch_evidence = [
            item
            for item in candidates
            if item["issue_id"] in member_ids or item["related_issue_id"] in member_ids
        ]
        manifest = build_batch_manifest(
            run_id,
            index,
            [_issue_summary(issue) for issue in batch_issues],
            batch_evidence,
            REVIEW_RUBRIC,
            snapshot=snapshot_payload,
            model=model,
            cache=cache_stats,
            batch_kind=review_batch.kind,
            review_units=[{"issue_ids": list(unit)} for unit in review_batch.review_units],
        )
        manifests.append(manifest)
        if run_dir is not None:
            _atomic_text(run_dir / f"batch-{index}.json", _json_text(manifest))
            _atomic_text(run_dir / f"batch-{index}.md", render_batch_markdown(manifest))
    summary_batches = [
        {
            "batch": item["batch"],
            "kind": item["kind"],
            "issue_ids": [issue["id"] for issue in item["issues"]],
            "review_units": item["review_units"],
        }
        for item in manifests
    ]
    ranking_warnings = list(snapshot.source_warnings)
    if excluded_ephemeral:
        ranking_warnings.append(
            f"Excluded {len(excluded_ephemeral)} ephemeral runtime records from durable review."
        )
    if skip_semantic_vectors:
        ranking_warnings.append(
            "Semantic embedding skipped: the structure-only review had no reviewable "
            "active typed relationships."
        )
    semantic_issue_cap_drops = (
        ranking.dropped_by_issue_cap - ranking.dropped_by_dependency_issue_cap
    )
    if semantic_issue_cap_drops:
        ranking_warnings.append(
            f"Per-issue semantic candidate cap omitted {semantic_issue_cap_drops} qualified pairs."
        )
    if ranking.dropped_by_dependency_issue_cap:
        ranking_warnings.append(
            "Per-issue dependency allowance omitted "
            f"{ranking.dropped_by_dependency_issue_cap} qualified typed edges; "
            "compact structural summaries were retained."
        )
    if ranking.dropped_by_echo_target_cap:
        ranking_warnings.append(
            "Completed-target diversity cap omitted "
            f"{ranking.dropped_by_echo_target_cap} repeated echo pairs; "
            "the next qualified candidates were considered."
        )
    if ranking.echo_disposition_omissions:
        ranking_warnings.append(
            "Explicit pair-local close dispositions omitted "
            f"{sum(ranking.echo_disposition_omissions.values())} contradictory completed-work "
            "echoes."
        )
    if ranking.dropped_by_run_cap:
        ranking_warnings.append(
            f"Run candidate cap omitted {ranking.dropped_by_run_cap} qualified pairs."
        )
    if ranking.dropped_by_lane_cap:
        ranking_warnings.append(
            f"Lane candidate budgets omitted {ranking.dropped_by_lane_cap} qualified pairs."
        )
    reciprocal_omissions = (ranking.reciprocal_diagnostics or {}).get("omitted", 0)
    if reciprocal_omissions:
        ranking_warnings.append(
            "Reciprocal evidence guard omitted "
            f"{reciprocal_omissions} pairs without discriminative local evidence."
        )
    if ranking.cap_replacements:
        ranking_warnings.append(
            f"Threshold comparison exposed {len(ranking.cap_replacements)} "
            "deterministic cap-driven replacements."
        )
    for receipt in ranking.degradation_receipts:
        balance = describe_conservation_balance(receipt.get("balance"))
        ranking_warnings.append(
            "Dependency lane unavailable: its conservation audit failed"
            + (f" ({balance})" if balance else "")
            + "; healthy requested semantic lanes were retained."
        )
    if packaging.diagnostics.cross_batch_candidate_edges:
        ranking_warnings.append(
            f"Hard batch limit split {packaging.diagnostics.cross_batch_candidate_edges} "
            "candidate edges across review artifacts."
        )
    queue_population = (
        [issue for issue in population if issue.id in active_scope_ids]
        if active_scope_ids is not None
        else population
    )
    population_ids = {issue.id for issue in queue_population}
    candidate_issue_ids = {
        identifier
        for item in candidates
        for identifier in (item["issue_id"], item["related_issue_id"])
        if identifier in population_ids
    }
    no_signal_ids = sorted(
        issue.id for issue in queue_population if issue.id not in candidate_issue_ids
    )
    unchanged_ids = (
        sorted(issue.id for issue in population if issue.id not in active_scope_ids)
        if active_scope_ids is not None
        else []
    )
    scope_payload = {
        "mode": scope.mode if scope else "full",
        "changed_active_count": (
            len(active_scope_ids) if active_scope_ids is not None else len(population_ids)
        ),
        "changed_closed_count": (
            sum(
                issue.id in scope.changed_ids
                and issue.status.casefold() in {"closed", "done", "completed", "resolved"}
                for issue in review_issues
            )
            if scope
            else 0
        ),
        "unchanged_active_count": len(unchanged_ids),
        "unknown_timestamp_count": len(scope.unknown_timestamp_ids) if scope else 0,
        "deleted_since_checkpoint_count": len(scope.deleted_ids) if scope else 0,
        "checkpoint_created_at": scope.checkpoint_created_at if scope else None,
    }
    review_budget = {
        "mode": "weekly" if args.weekly_review_budget is not None else "standard",
        "candidate_limit": max_candidates,
        "admitted_candidates": len(candidates),
        "omitted_candidates": ranking.dropped_by_run_cap,
        "selection_policy": "reserved-lane-access-then-priority-reflow",
        "reservation_order": ["dependency", "echo", "overlap"],
        "priority_order": [
            "typed-dependency",
            "high-confidence-completed-work-echo",
            "possible-overlap",
        ],
        "omitted_by_lane": {
            lane: metrics.dropped_by_run_cap for lane, metrics in (ranking.lanes or {}).items()
        },
        "lane_capacity": {
            lane: {
                "reserved": metrics.reserved,
                "admitted_to_reservation": metrics.admitted_to_reservation,
                "unused": metrics.unused_reserved,
            }
            for lane, metrics in (ranking.lanes or {}).items()
            if lane_reservations is not None
        },
        "code_surface_scope": "outside-semantic-candidate-limit",
    }
    payload = build_sweep_payload(
        run_id,
        candidates,
        summary_batches,
        snapshot=snapshot_payload,
        model=model,
        cache=cache_stats,
        filters={
            "status": sorted(statuses),
            "include_epics": args.include_epics,
            "include_ephemeral": args.include_ephemeral,
            **({"review_objectives": sorted(objectives)} if objectives is not None else {}),
            **({"semantic_view": "fields"} if args.semantic_view == "fields" else {}),
            "incremental_scope": scope_payload,
        },
        thresholds={
            "completed_work_echo": args.echo_threshold,
            "possible_overlap": args.overlap_threshold,
        },
        candidate_policy={
            "exception_margin": args.exception_margin,
            "reciprocal_rank": args.reciprocal_rank,
            "max_per_issue": args.max_candidates_per_issue,
            "max_echoes_per_target": args.max_echoes_per_target,
            "max_echo_alternatives_per_active": args.max_echo_alternatives_per_active,
            "max_dependencies_per_issue": args.max_dependency_candidates_per_issue,
            "max_total": max_candidates,
            **({"review_objectives": sorted(objectives)} if objectives is not None else {}),
            **({"semantic_view": "fields"} if args.semantic_view == "fields" else {}),
            "review_budget": review_budget,
            "lane_caps": lane_caps,
            "qualified": ranking.qualified,
            "baseline_protected": ranking.baseline_protected,
            "reciprocal_diagnostics": ranking.reciprocal_diagnostics,
            "cap_replacements": list(ranking.cap_replacements),
            "dependency_funnel": asdict(ranking.dependency_funnel)
            if ranking.dependency_funnel
            else {},
            "lanes": {lane: asdict(metrics) for lane, metrics in (ranking.lanes or {}).items()},
            "dropped_by_lane_cap": ranking.dropped_by_lane_cap,
            "dropped_by_issue_cap": ranking.dropped_by_issue_cap,
            "dropped_by_dependency_issue_cap": ranking.dropped_by_dependency_issue_cap,
            "dropped_by_echo_target_cap": ranking.dropped_by_echo_target_cap,
            "echo_target_hubs": list(ranking.echo_target_hubs),
            "echo_backfills": list(ranking.echo_backfills),
            **(
                {"echo_disposition_omissions": ranking.echo_disposition_omissions}
                if ranking.echo_disposition_omissions
                else {}
            ),
            "dropped_by_run_cap": ranking.dropped_by_run_cap,
        },
        capped_typed_dependencies=list(ranking.capped_typed_dependencies),
        degradations=ranking.degradation_receipts,
        warnings=ranking_warnings,
        no_signal={"count": len(no_signal_ids), "issue_ids": no_signal_ids},
        excluded={
            "count": len(excluded_epics) + len(excluded_ephemeral) + len(unchanged_ids),
            "by_reason": {
                **({"epic": len(excluded_epics)} if excluded_epics else {}),
                **({"ephemeral": len(excluded_ephemeral)} if excluded_ephemeral else {}),
                **({"unchanged": len(unchanged_ids)} if unchanged_ids else {}),
            },
            "issue_ids": sorted(
                [issue.id for issue in excluded_epics]
                + [issue.id for issue in excluded_ephemeral]
                + unchanged_ids
            ),
        },
        target_batch_size=args.size,
        batch_diagnostics=asdict(packaging.diagnostics),
        duration_ms=round((time.monotonic() - started) * 1000),
        code_surface_analysis=code_surface_payload,
    )
    payload["timings_ms"] = {
        "acquisition": acquisition_ms,
        "embedding_and_cache": embedding_ms,
        "similarity_scoring": similarity_scoring_ms,
        "candidate_analysis": candidate_analysis_ms,
        "batching": batching_ms,
        "code_surface_analysis": code_surface_ms,
    }
    payload["artifact_output"] = (
        {
            "mode": "report-file",
            "format": report_file_format,
        }
        if report_file is not None
        else {
            "mode": "artifact-directory",
            "format": "json-and-markdown",
        }
    )
    if run_dir is not None:
        payload["output_directory"] = str(run_dir)
        _atomic_text(run_dir / "report.json", _json_text(payload))
        _atomic_text(run_dir / "report.md", render_sweep_markdown(payload))
    if args.write_checkpoint:
        _atomic_text(
            args.write_checkpoint,
            _json_text(build_checkpoint(issues, workspace_id=snapshot.workspace_id)),
        )
    if args.command == "triage":
        packet = build_triage_payload(payload)
        if run_dir is not None:
            _atomic_text(run_dir / "triage.json", _json_text(packet))
            _atomic_text(run_dir / "triage.md", render_triage_markdown(packet))
        rendered = _json_text(packet) if args.as_json else render_triage_markdown(packet)
        report_json = _json_text(packet)
        report_markdown = render_triage_markdown(packet)
    else:
        rendered = _json_text(payload) if args.as_json else render_sweep_markdown(payload)
        report_json = _json_text(payload)
        report_markdown = render_sweep_markdown(payload)
    if report_file is not None:
        _atomic_text(
            report_file,
            report_json if report_file_format == "json" else report_markdown,
        )
    sys.stdout.write(rendered)
    if not args.as_json:
        if report_file is not None:
            sys.stdout.write(f"\nReport: {report_file}\n")
        else:
            sys.stdout.write(f"\nArtifacts: {run_dir}\n")
    return _divergence_exit(args, snapshot)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "neighbors":
            return _neighbors(args)
        if args.command == "readiness":
            return _readiness(args)
        if args.command == "capabilities":
            return _capabilities(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "collisions":
            return _collisions(args)
        if args.command == "mentions":
            return _mentions(args)
        if args.command == "orphans":
            return _orphans(args)
        return _sweep(args)
    except (TrackerError, OSError, RuntimeError, ValueError) as exc:
        print(f"embead: {exc}", file=sys.stderr)
        print("emBEADings made no tracker changes.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
