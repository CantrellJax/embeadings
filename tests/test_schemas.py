from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from embead.reports import (
    build_batch_manifest,
    build_collisions_payload,
    build_mentions_payload,
    build_neighbors_batch_payload,
    build_neighbors_payload,
    build_sweep_payload,
    build_triage_payload,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas" / "v1"
EXAMPLES = ROOT / "examples"
SNAPSHOT = {
    "workspace_id": "synthetic-workspace",
    "beads_version": "1.0.5",
    "workspace_path": None,
    "tracker_name": "beads",
    "tracker_version": "1.0.5",
    "acquisition_source": "live-beads-cli",
}
MODEL = {"model_id": "synthetic-local-model", "model_revision": "example-revision"}
CACHE = {"hits": 2, "misses": 0}
ISSUE = {
    "id": "demo-1",
    "title": "Document token refresh",
    "status": "open",
    "priority": 2,
    "labels": ["documentation"],
    "parent_id": "demo-epic",
    "dependencies": [],
}
EVIDENCE = {
    "kind": "completed-work-echo",
    "issue_id": "demo-1",
    "related_issue_id": "demo-2",
    "similarity": 0.91,
    "structural_context": "same parent demo-epic",
    "verification_anchor": {
        "category": "completed outcome",
        "operation": "render",
        "entity_class": "report",
        "source_field": "acceptance criteria",
        "confidence": "high",
        "extraction_confidence": "high",
        "confidence_scope": "anchor-extraction",
        "specificity": "concrete-check",
        "check_category": "artifact",
        "check_source_field": "acceptance criteria",
        "generic_fallback": False,
    },
    "candidate_evidence": {
        "evidence_basis": "structurally-corroborated",
        "structural_corroboration": "shared-parent",
        "admission_path": "semantic-threshold",
        "uncertainty": "structural-corroboration-recorded",
    },
    "what_to_verify": "Confirm whether completed work changed the active scope.",
    "counterevidence": ["no direct dependency is recorded"],
}
CODE_SURFACE_ANALYSIS = {
    "repository_available": True,
    "repository_context": "invocation-worktree",
    "repository_revision": "synthetic-revision",
    "base_reference": "origin/main",
    "base_revision": "synthetic-base-revision",
    "issue_count": 2,
    "pointer_count": 2,
    "issues_with_explicit_surfaces": 0,
    "issues_with_observed_surfaces": 2,
    "issues_without_surfaces": 0,
    "worktrees_discovered": 3,
    "worktrees_associated": 2,
    "source_counts": {"active-worktree-diff": 2},
    "hub_surface_limit": 5,
    "hub_surfaces": [],
    "pairs_omitted_by_hub_guard": 0,
    "pairs_omitted_by_module_guard": 0,
    "surfaces": [],
    "collisions": [
        {
            "issue_id": "demo-1",
            "related_issue_id": "demo-2",
            "kind": "exact-file",
            "confidence": "observed",
            "shared_paths": ["src/cache/index.py"],
            "shared_symbols": [],
            "shared_modules": ["src/cache"],
            "evidence_sources": ["active-worktree-diff"],
            "edit_intent": "observed-edit",
            "intent_source_fields": [],
            "revision_relation": "same",
            "what_to_verify": "Coordinate changes to the shared file.",
        }
    ],
    "warnings": [],
}


def _load(directory: Path, name: str) -> dict[str, Any]:
    return json.loads((directory / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    [
        "neighbors",
        "neighbors-batch",
        "batch",
        "sweep",
        "triage",
        "collisions",
        "mentions",
        "orphans",
        "match",
        "capabilities",
        "checkpoint",
    ],
)
def test_schema_is_valid_draft_2020_12_and_accepts_example(name: str) -> None:
    schema = _load(SCHEMAS, f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_load(EXAMPLES, f"{name}.json"))


def test_report_builders_produce_schema_valid_payloads() -> None:
    neighbor = {**ISSUE, "id": "demo-2", "similarity": 0.91}
    payloads = {
        "neighbors": build_neighbors_payload(
            ISSUE, [neighbor], snapshot=SNAPSHOT, model=MODEL, cache=CACHE
        ),
        "batch": build_batch_manifest(
            "synthetic-run-1",
            1,
            [ISSUE],
            [EVIDENCE],
            ["Verify against current source."],
            snapshot=SNAPSHOT,
            model=MODEL,
            cache=CACHE,
        ),
        "sweep": build_sweep_payload(
            "synthetic-run-1",
            [EVIDENCE],
            [
                {
                    "batch": 1,
                    "kind": "singleton-envelope",
                    "issue_ids": ["demo-1"],
                    "review_units": [{"issue_ids": ["demo-1"]}],
                }
            ],
            snapshot=SNAPSHOT,
            model=MODEL,
            cache=CACHE,
            filters={"status": ["open"]},
            thresholds={"completed_work_echo": 0.72},
            candidate_policy={
                "review_budget": {
                    "mode": "weekly",
                    "candidate_limit": 20,
                    "admitted_candidates": 1,
                    "omitted_candidates": 0,
                    "selection_policy": "reserved-lane-access-then-priority-reflow",
                    "reservation_order": ["dependency", "echo", "overlap"],
                    "priority_order": [
                        "typed-dependency",
                        "high-confidence-completed-work-echo",
                        "possible-overlap",
                    ],
                    "omitted_by_lane": {"dependency": 0, "echo": 0, "overlap": 0},
                    "lane_capacity": {
                        "dependency": {
                            "reserved": 12,
                            "admitted_to_reservation": 1,
                            "unused": 11,
                        },
                        "echo": {
                            "reserved": 4,
                            "admitted_to_reservation": 0,
                            "unused": 4,
                        },
                        "overlap": {
                            "reserved": 4,
                            "admitted_to_reservation": 0,
                            "unused": 4,
                        },
                    },
                    "code_surface_scope": "outside-semantic-candidate-limit",
                },
                "lanes": {
                    lane: {
                        "qualified": 0,
                        "admitted": 0,
                        "baseline_protected": 0,
                        "dropped_by_lane_cap": 0,
                        "dropped_by_issue_cap": 0,
                        "dropped_by_target_cap": 0,
                        "dropped_by_dependency_issue_cap": 0,
                        "dropped_by_run_cap": 0,
                        "reserved": 0,
                        "admitted_to_reservation": 0,
                        "unused_reserved": 0,
                    }
                    for lane in ("dependency", "echo", "overlap")
                },
            },
            capped_typed_dependencies=[
                {
                    "source_id": "demo-3",
                    "target_id": "demo-4",
                    "type": "blocks",
                    "drop_reason": "dependency-per-issue-cap",
                }
            ],
            no_signal={"count": 1, "issue_ids": ["demo-3"]},
            excluded={"count": 1, "by_reason": {"epic": 1}, "issue_ids": ["demo-epic"]},
            target_batch_size=5,
            duration_ms=12,
            code_surface_analysis=CODE_SURFACE_ANALYSIS,
        ),
        "collisions": build_collisions_payload(
            CODE_SURFACE_ANALYSIS,
            snapshot=SNAPSHOT,
            filters={"status": ["open"]},
        ),
    }
    payloads["triage"] = build_triage_payload(payloads["sweep"])

    for report_type, payload in payloads.items():
        Draft202012Validator(_load(SCHEMAS, f"{report_type}.schema.json")).validate(payload)

    assert payloads["sweep"]["no_signal"]["count"] == 1
    assert payloads["sweep"]["excluded"]["by_reason"] == {"epic": 1}
    assert payloads["sweep"]["anchor_metrics"] == {
        "total": 1,
        "confidence": {"high": 1, "medium": 0, "low": 0},
        "specificity": {"concrete-check": 1, "category-check": 0, "generic": 0},
        "actionable_proxy_count": 1,
        "actionable_proxy_rate": 1.0,
        "generic_fallback_count": 0,
        "generic_fallback_rate": 0.0,
    }
    assert payloads["batch"]["anchor_metrics"] == payloads["sweep"]["anchor_metrics"]
    assert payloads["triage"]["analysis_fingerprint"] == payloads["sweep"]["analysis_fingerprint"]


@pytest.mark.parametrize("report_type", ["sweep", "triage"])
def test_degradation_receipt_carries_named_conservation_balance(report_type: str) -> None:
    payload = _load(EXAMPLES, f"{report_type}.json")
    payload["analysis_status"] = "degraded"
    payload["degradations"] = [
        {
            "code": "typed-dependency-conservation-failed",
            "lane": "dependency",
            "stage": "admission",
            "balance": {
                "input": {"name": "eligible_candidates", "count": 2},
                "parts": [{"name": "admitted", "count": 1}, {"name": "omitted", "count": 0}],
                "unaccounted": 1,
            },
            "counts": {
                "total_non_parent_typed": 2,
                "inactive_or_closed_only": 0,
                "below_qualification": 0,
                "eligible": 2,
                "eligible_candidates": 2,
                "coalesced_eligible_edges": 0,
                "admitted": 1,
                "omitted_by_per_issue_cap": 0,
                "omitted_by_lane_cap": 0,
                "omitted_by_run_cap": 0,
            },
            "degraded_lanes": ["dependency"],
            "retained_lanes": ["echo", "overlap"],
        }
    ]

    Draft202012Validator(_load(SCHEMAS, f"{report_type}.schema.json")).validate(payload)


def test_version_one_allows_additive_fields() -> None:
    payload = _load(EXAMPLES, "sweep.json")
    payload["future_diagnostic"] = {"reason": "synthetic"}
    payload["candidates"][0]["future_evidence"] = ["synthetic"]

    Draft202012Validator(_load(SCHEMAS, "sweep.schema.json")).validate(payload)


def test_version_one_accepts_legacy_review_budget_shape() -> None:
    payload = _load(EXAMPLES, "sweep.json")
    review_budget = payload["parameters"]["candidate_policy"]["review_budget"]
    for field in (
        "selection_policy",
        "reservation_order",
        "lane_capacity",
        "code_surface_scope",
    ):
        review_budget.pop(field)

    Draft202012Validator(_load(SCHEMAS, "sweep.schema.json")).validate(payload)


@pytest.mark.parametrize(
    "name",
    [
        "neighbors",
        "neighbors-batch",
        "batch",
        "sweep",
        "collisions",
        "mentions",
        "orphans",
        "match",
    ],
)
def test_version_one_accepts_generic_linear_snapshot(name: str) -> None:
    payload = _load(EXAMPLES, f"{name}.json")
    payload["snapshot"] = {
        "workspace_id": "synthetic-linear-team",
        "beads_version": None,
        "workspace_path": None,
        "tracker_name": "linear",
        "tracker_version": "graphql-current",
        "relation_diagnostics": {
            "raw_relation_count": 7,
            "retained_relation_count": 2,
            "omitted_relation_count": 3,
            "boundary_relation_count": 2,
        },
    }

    Draft202012Validator(_load(SCHEMAS, f"{name}.schema.json")).validate(payload)


def test_unsupported_version_and_weakened_policy_are_rejected() -> None:
    schema = _load(SCHEMAS, "neighbors.schema.json")
    example = _load(EXAMPLES, "neighbors.json")
    unsupported = copy.deepcopy(example)
    unsupported["schema_version"] = 2
    unsafe = copy.deepcopy(example)
    unsafe["policy"]["tracker_mutation_allowed"] = True

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(unsupported)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(unsafe)


def test_new_report_builders_produce_schema_valid_payloads() -> None:
    neighbor = {
        **ISSUE,
        "id": "demo-2",
        "similarity": 0.68,
        "rank": 1,
        "reverse_rank": 1,
        "text_claims": [
            {"issue_id": "demo-2", "related_issue_id": "demo-1", "kind": "absorbed-by"}
        ],
    }
    batch = build_neighbors_batch_payload(
        [(ISSUE, [neighbor]), ({**ISSUE, "id": "demo-2"}, [])],
        snapshot=SNAPSHOT,
        model=MODEL,
        cache=CACHE,
    )
    Draft202012Validator(_load(SCHEMAS, "neighbors-batch.schema.json")).validate(batch)
    single = build_neighbors_payload(ISSUE, [neighbor], snapshot=SNAPSHOT, model=MODEL)
    Draft202012Validator(_load(SCHEMAS, "neighbors.schema.json")).validate(single)

    mentions = build_mentions_payload(
        [
            {
                "issue_id": "demo-2",
                "related_issue_id": "demo-1",
                "kind": "absorbed-by",
                "source_fields": ("notes", "close_reason"),
                "typed_link": False,
            }
        ],
        snapshot=SNAPSHOT,
        summary={
            "claimants_scanned": 2,
            "claims_found": 1,
            "claims_with_typed_link": 0,
            "kind_counts": {"absorbed-by": 1},
            "omitted_by_limit": 0,
        },
    )
    Draft202012Validator(_load(SCHEMAS, "mentions.schema.json")).validate(mentions)

    unsafe = copy.deepcopy(mentions)
    unsafe["claims"][0]["context"] = "raw tracker text"
    with pytest.raises(ValidationError):
        Draft202012Validator(_load(SCHEMAS, "mentions.schema.json")).validate(unsafe)
