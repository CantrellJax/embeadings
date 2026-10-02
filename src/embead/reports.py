"""Stable, read-only report payloads and human-friendly Markdown renderers.

This module deliberately knows nothing about the embedding or Beads adapters.  Inputs
may be mappings, dataclasses, named tuples, or ordinary objects with public
attributes.  Every builder returns values accepted by :mod:`json`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .validation import validate_artifact

SCHEMA_VERSION = 1
ADVISORY_NOTICE = (
    "Semantic similarity is advisory evidence, not tracker truth. "
    "Verify against current project state before taking action."
)
READ_ONLY_NOTICE = (
    "Read-only report: emBEADings did not change issues and this report does not "
    "recommend lifecycle changes."
)


def _field(value: Any, *names: str, default: Any = None) -> Any:
    """Read the first available field without requiring a concrete input type."""

    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _jsonable(value: Any) -> Any:
    """Convert common Python values to deterministic JSON-compatible values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("report values must not contain non-finite numbers")
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump())
    if hasattr(value, "_asdict"):
        return _jsonable(value._asdict())
    if isinstance(value, (set, frozenset)):
        converted = [_jsonable(item) for item in value]
        return sorted(converted, key=_sort_repr)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return _jsonable(
            {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_") and not callable(item)
            }
        )
    slots = getattr(type(value), "__slots__", ())
    if isinstance(slots, str):
        slots = (slots,)
    if slots:
        return _jsonable(
            {
                name: getattr(value, name)
                for name in slots
                if not name.startswith("_") and hasattr(value, name)
            }
        )
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def _sort_repr(value: Any) -> str:
    if isinstance(value, Mapping):
        return "|".join(f"{key}={_sort_repr(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "[" + ",".join(_sort_repr(item) for item in value) + "]"
    return str(value)


def _record(value: Any) -> dict[str, Any]:
    record = _jsonable(value)
    if not isinstance(record, dict):
        raise TypeError("issues and evidence must be mapping-like objects")
    # Lifecycle source text can inform bounded ranking decisions but is never
    # report content, even when a caller passes a full IssueRecord directly.
    record.pop("close_reason", None)
    if "id" not in record:
        issue_id = _field(value, "issue_id")
        if issue_id is not None:
            record["id"] = _jsonable(issue_id)
    return dict(sorted(record.items()))


def _identity(value: Any) -> str:
    return str(_field(value, "id", "issue_id", default=""))


def _score(value: Any) -> float:
    raw = _field(value, "similarity", "score", default=-1.0)
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return -1.0
    return score if math.isfinite(score) else -1.0


def _evidence_key(value: Any) -> tuple[float, str, str]:
    return (
        -_score(value),
        str(_field(value, "id", "issue_id", "source_id", default="")),
        str(_field(value, "neighbor_id", "related_issue_id", "target_id", default="")),
    )


def _candidate_key(value: Any) -> tuple[int, float, str, str, str]:
    admission = str(_field(value, "admission_reason", default="semantic-threshold"))
    return (
        int(admission != "semantic-threshold"),
        -_score(value),
        str(_field(value, "kind", "type", default="")),
        str(_field(value, "issue_id", "id", default="")),
        str(_field(value, "related_issue_id", "neighbor_id", default="")),
    )


def _anchor_metrics(evidence: Iterable[Any]) -> dict[str, Any]:
    """Summarize safe-anchor specificity without inspecting or repeating issue text."""

    items = list(evidence)
    confidence = {"high": 0, "medium": 0, "low": 0}
    specificity = {"concrete-check": 0, "category-check": 0, "generic": 0}
    generic_fallback_count = 0
    actionable_proxy_count = 0
    for item in items:
        anchor = _field(item, "verification_anchor", default={}) or {}
        level = str(_field(anchor, "confidence", default="low"))
        confidence[level if level in confidence else "low"] += 1
        tier = str(_field(anchor, "specificity", default="generic"))
        specificity[tier if tier in specificity else "generic"] += 1
        generic_fallback_count += bool(_field(anchor, "generic_fallback", default=True))
        actionable_proxy_count += tier in {"concrete-check", "category-check"}
    total = len(items)
    return {
        "total": total,
        "confidence": confidence,
        "specificity": specificity,
        # This is a deterministic packaging proxy, not a substitute for blinded human ratings.
        "actionable_proxy_count": actionable_proxy_count,
        "actionable_proxy_rate": round(actionable_proxy_count / total, 4) if total else 0.0,
        "generic_fallback_count": generic_fallback_count,
        "generic_fallback_rate": round(generic_fallback_count / total, 4) if total else 0.0,
    }


def build_neighbors_payload(
    issue: Any,
    neighbors: Iterable[Any],
    *,
    snapshot: Any,
    model: Any,
    cache: Any | None = None,
    filters: Any | None = None,
) -> dict[str, Any]:
    """Build the versioned machine payload for a nearest-neighbor query."""

    ordered = sorted(neighbors, key=_evidence_key)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "neighbors",
        "filters": _jsonable(filters or {}),
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "model": _jsonable(model),
        "cache": _jsonable(cache or {}),
        "issue": _record(issue),
        "neighbors": [_record(neighbor) for neighbor in ordered],
    }
    return payload


def build_neighbors_batch_payload(
    results: Iterable[tuple[Any, Iterable[Any]]],
    *,
    snapshot: Any,
    model: Any,
    cache: Any | None = None,
    filters: Any | None = None,
) -> dict[str, Any]:
    """Build one payload for several seeds that shared a single corpus and model load."""

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "neighbors-batch",
        "filters": _jsonable(filters or {}),
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "model": _jsonable(model),
        "cache": _jsonable(cache or {}),
        "results": [
            {
                "issue": _record(issue),
                "neighbors": [_record(item) for item in sorted(neighbors, key=_evidence_key)],
            }
            for issue, neighbors in results
        ],
    }


def build_mentions_payload(
    claims: Iterable[Any],
    *,
    snapshot: Any,
    filters: Any | None = None,
    summary: Any | None = None,
) -> dict[str, Any]:
    """Build a report of lineage claims written in tracker text, without the text itself."""

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "mentions",
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "snippets_included": False,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "filters": _jsonable(filters or {}),
        "summary": _jsonable(summary or {}),
        "claims": [_jsonable(claim) for claim in claims],
    }


def build_orphans_payload(
    dangling: Iterable[Any],
    *,
    snapshot: Any,
    filters: Any | None = None,
    summary: Any | None = None,
    parentless: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Build a structural report of live issues whose parent is closed or missing."""

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "orphans",
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "snippets_included": False,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "filters": _jsonable(filters or {}),
        "summary": _jsonable(summary or {}),
        "dangling_parent": [_jsonable(row) for row in dangling],
        "parentless": [_jsonable(group) for group in parentless or ()],
    }


def build_match_payload(
    candidates: Iterable[Any],
    *,
    snapshot: Any,
    model: Any,
    cache: Any | None = None,
    filters: Any | None = None,
    summary: Any | None = None,
) -> dict[str, Any]:
    """Build the report for candidate text matched against existing records (no new record)."""

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "match",
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "snippets_included": False,
            "creates_records": False,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "model": _jsonable(model),
        "cache": _jsonable(cache or {}),
        "filters": _jsonable(filters or {}),
        "summary": _jsonable(summary or {}),
        "candidates": [_jsonable(candidate) for candidate in candidates],
    }


def build_batch_manifest(
    run_id: str,
    batch: int,
    issues: Iterable[Any],
    neighbor_evidence: Iterable[Any],
    review_rubric: Iterable[Any],
    *,
    snapshot: Any,
    model: Any,
    cache: Any | None = None,
    batch_kind: str = "connected-component",
    review_units: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic, disposable review-batch manifest."""

    ordered_issues = sorted(issues, key=_identity)
    ordered_evidence = sorted(neighbor_evidence, key=_evidence_key)
    rubric = [_jsonable(item) for item in review_rubric]
    normalized_units = (
        [_jsonable(item) for item in review_units]
        if review_units is not None
        else [
            {
                "issue_ids": [
                    str(_field(issue, "id", "issue_id", default="")) for issue in ordered_issues
                ]
            }
        ]
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "batch",
        "run_id": str(run_id),
        "batch": int(batch),
        "kind": str(batch_kind),
        "review_units": normalized_units,
        "snapshot": _jsonable(snapshot),
        "model": _jsonable(model),
        "cache": _jsonable(cache or {}),
        "policy": {
            "read_only": True,
            "implementation_allowed": False,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "issues": [_record(issue) for issue in ordered_issues],
        "neighbor_evidence": [_record(item) for item in ordered_evidence],
        "anchor_metrics": _anchor_metrics(ordered_evidence),
        "review_rubric": rubric,
    }
    validate_artifact(payload)
    return payload


def build_collisions_payload(
    analysis: Any,
    *,
    snapshot: Any,
    filters: Any | None = None,
) -> dict[str, Any]:
    """Build a versioned code-surface collision report without source contents."""

    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "collisions",
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "source_mutation_allowed": False,
            "advisory": True,
            "snippets_included": False,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "filters": _jsonable(filters or {}),
        "code_surface_analysis": _jsonable(analysis),
    }


def build_sweep_payload(
    run_id: str,
    candidates: Iterable[Any],
    batches: Iterable[Any],
    *,
    snapshot: Any,
    model: Any,
    cache: Any | None = None,
    filters: Any | None = None,
    thresholds: Any | None = None,
    candidate_policy: Any | None = None,
    capped_typed_dependencies: Iterable[Any] = (),
    degradations: Iterable[Any] = (),
    no_signal: Any | None = None,
    excluded: Any | None = None,
    target_batch_size: int | None = None,
    batch_diagnostics: Any | None = None,
    warnings: Iterable[str] = (),
    duration_ms: int | float | None = None,
    code_surface_analysis: Any | None = None,
) -> dict[str, Any]:
    """Build a versioned summary of one synchronous or asynchronous sweep."""

    ordered_candidates = sorted(candidates, key=_candidate_key)
    normalized_batches = [_jsonable(item) for item in batches]
    normalized_batches.sort(key=lambda item: int(_field(item, "batch", "batch_number", default=0)))
    normalized_degradations = sorted(
        (_jsonable(item) for item in degradations),
        key=lambda item: (
            str(_field(item, "lane", default="")),
            str(_field(item, "code", default="")),
            str(_field(item, "stage", default="")),
        ),
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "sweep",
        "run_id": str(run_id),
        "analysis_status": "degraded" if normalized_degradations else "complete",
        "degradations": normalized_degradations,
        "policy": {
            "read_only": True,
            "tracker_mutation_allowed": False,
            "advisory": True,
            "notice": f"{READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        },
        "snapshot": _jsonable(snapshot),
        "model": _jsonable(model),
        "cache": _jsonable(cache or {}),
        "parameters": {
            "filters": _jsonable(filters or {}),
            "thresholds": _jsonable(thresholds or {}),
            "candidate_policy": _jsonable(candidate_policy or {}),
            "target_batch_size": target_batch_size,
            "max_batch_size": target_batch_size,
        },
        "candidates": [_record(candidate) for candidate in ordered_candidates],
        "capped_typed_dependencies": [
            _record(item)
            for item in sorted(
                capped_typed_dependencies,
                key=lambda item: (
                    str(_field(item, "source_id", default="")),
                    str(_field(item, "target_id", default="")),
                    str(_field(item, "type", default="")),
                    str(_field(item, "drop_reason", default="")),
                ),
            )
        ],
        "anchor_metrics": _anchor_metrics(ordered_candidates),
        "batches": normalized_batches,
        "no_signal": _jsonable(no_signal or {"count": 0, "issue_ids": []}),
        "excluded": _jsonable(excluded or {"count": 0, "by_reason": {}, "issue_ids": []}),
        "warnings": sorted(str(warning) for warning in warnings),
        "batch_diagnostics": _jsonable(batch_diagnostics or {}),
    }
    if duration_ms is not None:
        payload["duration_ms"] = _jsonable(duration_ms)
    if code_surface_analysis is not None:
        payload["code_surface_analysis"] = _jsonable(code_surface_analysis)
    payload["analysis_fingerprint"] = _analysis_fingerprint(payload)
    validate_artifact(payload)
    return payload


def _analysis_fingerprint(payload: Mapping[str, Any]) -> str:
    """Fingerprint stable analysis content while excluding runtime and cache receipts."""

    stable = {
        key: payload[key]
        for key in (
            "snapshot",
            "model",
            "parameters",
            "candidates",
            "capped_typed_dependencies",
            "batches",
            "no_signal",
            "excluded",
            "warnings",
            "analysis_status",
            "degradations",
            "code_surface_analysis",
        )
        if key in payload
    }
    encoded = json.dumps(
        _jsonable(stable),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    issue_id = str(_field(candidate, "issue_id", default=""))
    related_id = str(_field(candidate, "related_issue_id", default=""))
    kind = str(_field(candidate, "kind", default="review-candidate"))
    evidence = _field(candidate, "candidate_evidence", default={}) or {}
    compact = {
        "candidate_id": f"{kind}|{issue_id}|{related_id}",
        "kind": kind,
        "lane": str(_field(candidate, "lane", default="overlap")),
        "issue_id": issue_id,
        "related_issue_id": related_id,
        "similarity": _score(candidate),
        "evidence_basis": str(_field(evidence, "evidence_basis", default="semantic-only")),
        "what_to_verify": str(_field(candidate, "what_to_verify", default=ADVISORY_NOTICE)),
        "counterevidence": _jsonable(_field(candidate, "counterevidence", default=[])),
    }
    for field in (
        "objective",
        "admission_reason",
        "structural_context",
        "dependency_evidence",
        "verification_anchor",
    ):
        value = _field(candidate, field, default=None)
        if value is not None:
            compact[field] = _jsonable(value)
    return compact


def build_triage_payload(sweep: Mapping[str, Any]) -> dict[str, Any]:
    """Build a compact, redacted packet for agent or human triage judgment."""

    if _field(sweep, "report_type") != "sweep":
        raise ValueError("triage packets require a sweep report")
    snapshot = _field(sweep, "snapshot", default={}) or {}
    parameters = _field(sweep, "parameters", default={}) or {}
    policy = _field(parameters, "candidate_policy", default={}) or {}
    analysis = _field(sweep, "code_surface_analysis", default={}) or {}
    lanes = _field(policy, "lanes", default={}) or {}
    no_signal = _field(sweep, "no_signal", default={}) or {}
    excluded = _field(sweep, "excluded", default={}) or {}
    warnings = {str(warning) for warning in (_field(sweep, "warnings", default=[]) or [])}
    warnings.update(str(warning) for warning in (_field(analysis, "warnings", default=[]) or []))
    fingerprint = str(_field(sweep, "analysis_fingerprint", default=_analysis_fingerprint(sweep)))
    source_report = {
        "report_type": "sweep",
        "analysis_fingerprint": fingerprint,
        "model": _jsonable(_field(sweep, "model", default={})),
    }
    if _field(sweep, "degradations", default=[]) or []:
        source_report["parameters"] = _jsonable(parameters)
    packet = {
        "schema_version": SCHEMA_VERSION,
        "report_type": "triage",
        "analysis_status": str(_field(sweep, "analysis_status", default="complete")),
        "degradations": _jsonable(_field(sweep, "degradations", default=[]) or []),
        "policy": _jsonable(_field(sweep, "policy", default={})),
        "analysis_fingerprint": fingerprint,
        "source_report": source_report,
        "snapshot": {
            key: _jsonable(snapshot[key])
            for key in (
                "workspace_id",
                "tracker_name",
                "tracker_version",
                "beads_version",
                "acquisition_source",
                "live_issue_count",
                "dependency_count",
                "relation_diagnostics",
            )
            if key in snapshot
        },
        "review_budget": _jsonable(_field(policy, "review_budget", default={})),
        "batch_policy": {
            "max_batch_size": int(_field(parameters, "max_batch_size", default=0)),
        },
        "lane_summary": {
            lane: {
                "qualified": int(_field(metrics, "qualified", default=0)),
                "admitted": int(_field(metrics, "admitted", default=0)),
                "omitted": int(_field(metrics, "dropped_by_lane_cap", default=0))
                + int(_field(metrics, "dropped_by_issue_cap", default=0))
                + int(_field(metrics, "dropped_by_target_cap", default=0))
                + int(_field(metrics, "dropped_by_run_cap", default=0)),
            }
            for lane, metrics in sorted(lanes.items())
        },
        "candidates": [
            _compact_candidate(candidate)
            for candidate in (_field(sweep, "candidates", default=[]) or [])
        ],
        "collisions": _jsonable(_field(analysis, "collisions", default=[]) or []),
        "batches": _jsonable(_field(sweep, "batches", default=[]) or []),
        "population": {
            "no_signal": int(_field(no_signal, "count", default=0)),
            "excluded": int(_field(excluded, "count", default=0)),
        },
        "warnings": sorted(warnings),
    }
    artifact_output = _field(sweep, "artifact_output", default=None)
    if artifact_output is not None:
        packet["artifact_output"] = _jsonable(artifact_output)
    validate_artifact(packet)
    return packet


def _escape(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def describe_conservation_balance(balance: Any) -> str:
    """Render the named terms of a conservation identity, including the delta."""

    if not balance:
        return ""
    source = _field(balance, "input", default={}) or {}
    name = _field(source, "name")
    if name is None:
        return ""
    terms = [f"{name}={_field(source, 'count', default=0)}"]
    terms.extend(
        f"{_field(part, 'name')}={_field(part, 'count', default=0)}"
        for part in _field(balance, "parts", default=[]) or []
    )
    terms.append(f"unaccounted={_field(balance, 'unaccounted', default=0)}")
    return ", ".join(terms)


def _counterevidence_text(value: Any) -> str:
    counterevidence = _field(value, "counterevidence", default=[])
    if isinstance(counterevidence, str):
        return counterevidence or "none recorded"
    return "; ".join(str(item) for item in counterevidence or []) or "none recorded"


def _dependency_text(value: Any) -> str | None:
    evidence = _field(value, "dependency_evidence")
    if not evidence:
        return None
    source = _field(evidence, "source_id", default="unknown")
    target = _field(evidence, "target_id", default="unknown")
    relationship_type = _field(evidence, "type", "relationship_type", default="depends-on")
    return f"{source} → {target} ({relationship_type})"


def _retrieval_provenance_text(value: Any) -> str | None:
    provenance = _field(value, "retrieval_provenance", default={}) or {}
    channels = _field(provenance, "channels", default=[]) or []
    if not channels:
        return None
    rendered = []
    for receipt in channels:
        name = str(_field(receipt, "channel", default="unknown"))
        score = _field(receipt, "pair_score", default=None)
        ranks = _field(receipt, "ranks", default={}) or {}
        rank_text = ""
        if ranks:
            rank_text = (
                f" ranks {_field(ranks, 'issue_to_related', default='-')}"
                f"/{_field(ranks, 'related_to_issue', default='-')}"
            )
        score_text = f" score {float(score):.2f}" if score is not None else ""
        selected = " selected" if _field(receipt, "selected", default=False) else ""
        rendered.append(f"{name}{score_text}{rank_text}{selected}")
    rule = _field(provenance, "selection_rule", default="recorded-channel-union")
    return f"{rule}: " + "; ".join(rendered)


def _metadata_lines(payload: Mapping[str, Any]) -> list[str]:
    snapshot = payload.get("snapshot") or {}
    model = payload.get("model") or {}
    cache = payload.get("cache") or {}
    workspace = _field(snapshot, "workspace_id", "workspace", default="unknown")
    tracker_name = _field(snapshot, "tracker_name", default="beads") or "beads"
    tracker_version = _field(snapshot, "tracker_version", default=None) or _field(
        snapshot, "beads_version", default="unknown"
    )
    model_id = _field(model, "id", "model_id", "name", default="unknown")
    revision = _field(model, "revision", "model_revision", default="unknown")
    hits = _field(cache, "hits", "cache_hits", default=0)
    misses = _field(cache, "misses", "cache_misses", default=0)
    source = _field(snapshot, "acquisition_source", default="unknown")
    live_count = _field(snapshot, "live_issue_count", default="unknown")
    export_count = _field(snapshot, "export_issue_count", default=None)
    divergence_reasons = _field(snapshot, "source_divergence_reasons", default=[]) or []
    warnings = _field(snapshot, "source_warnings", default=[]) or []
    relation_diagnostics = _field(snapshot, "relation_diagnostics", default={}) or {}
    lines = [
        f"- Workspace snapshot: `{_escape(workspace)}` "
        f"({_escape(tracker_name)} `{_escape(tracker_version)}`)",
        f"- Acquisition source: `{_escape(source)}` ({_escape(live_count)} live issues)",
        f"- Embedding model: `{_escape(model_id)}` revision `{_escape(revision)}`",
        f"- Cache: {_escape(hits)} hits, {_escape(misses)} misses",
    ]
    if export_count is not None:
        lines.append(f"- Discoverable JSONL export: {_escape(export_count)} issues")
    if divergence_reasons:
        lines.append(
            "- Source divergence categories: "
            + ", ".join(f"`{_escape(reason)}`" for reason in divergence_reasons)
        )
    if relation_diagnostics:
        lines.append(
            "- Source relation accounting: "
            f"raw={_field(relation_diagnostics, 'raw_relation_count', default=0)}, "
            f"retained={_field(relation_diagnostics, 'retained_relation_count', default=0)}, "
            f"collapsed={_field(relation_diagnostics, 'collapsed_relation_count', default=0)}, "
            f"omitted={_field(relation_diagnostics, 'omitted_relation_count', default=0)}, "
            f"team-boundary={_field(relation_diagnostics, 'boundary_relation_count', default=0)}"
        )
    lines.extend(f"- Source warning: {_escape(warning)}" for warning in warnings)
    return lines


def render_neighbors_markdown(payload: Mapping[str, Any]) -> str:
    """Render a compact neighbors report; intentionally not a similarity matrix."""

    issue = payload.get("issue") or {}
    neighbors = payload.get("neighbors") or []
    issue_id = _field(issue, "id", "issue_id", default="unknown")
    title = _field(issue, "title", default="Untitled issue")
    lines = [
        f"# Semantic neighbors for {_escape(issue_id)}",
        "",
        f"**{_escape(title)}**",
        "",
        f"> {READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        "",
        "## Snapshot",
        "",
        *_metadata_lines(payload),
        "",
        "## Nearest semantic neighbors",
        "",
    ]
    orphans_only = _field(payload.get("filters"), "structural_link") == "none-recorded"
    if orphans_only:
        lines.extend(
            [
                "Filtered to neighbors with no recorded structural link.",
                "",
            ]
        )
    if not neighbors:
        lines.extend(
            [
                (
                    "No semantic neighbors without a recorded structural link were found "
                    "in the selected population."
                    if orphans_only
                    else "No semantic neighbors were found in the selected population."
                ),
                "",
            ]
        )
    else:
        lines.extend(
            [
                "| Rank | Issue | Status | Title | Similarity | Reverse rank | "
                "Structural context | Text claims |",
                "|---:|---|---|---|---:|---:|---|---|",
            ]
        )
        for neighbor in neighbors:
            score = _score(neighbor)
            score_text = f"{score:.2f}" if score >= 0 else "—"
            context = _field(
                neighbor, "structural_context", "relationship", default="none recorded"
            )
            claims = _field(neighbor, "text_claims", default=[]) or []
            claim_text = ", ".join(
                f"{_field(claim, 'issue_id')} {_field(claim, 'kind')} "
                f"{_field(claim, 'related_issue_id')}"
                for claim in claims
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(_field(neighbor, "rank", default="—")),
                        _escape(_field(neighbor, "id", "issue_id", default="unknown")),
                        _escape(_field(neighbor, "status", default="unknown")),
                        _escape(_field(neighbor, "title", default="Untitled issue")),
                        score_text,
                        _escape(_field(neighbor, "reverse_rank", default=None) or "—"),
                        _escape(context),
                        _escape(claim_text or "—"),
                    ]
                )
                + " |"
            )
        lines.extend(
            [
                "",
                "Scores are advisory; proximity can reflect shared context rather than overlap. "
                "Rank is the stronger signal: a neighbor at rank 1 whose reverse rank is also "
                "small is worth reading even when its score looks low for this model.",
                "",
            ]
        )
    lines.extend(
        [
            "## What to do next",
            "",
            "Verify relevant candidates against current project state and record evidence "
            "separately before considering any tracker action.",
            "",
        ]
    )
    return "\n".join(lines)


def _kind_label(value: Any) -> str:
    raw = str(_field(value, "kind", "type", default="review candidate"))
    normalized = raw.lower().replace("_", "-").replace(" ", "-")
    if normalized in {"completed-work-echo", "echo", "completed-echo"}:
        return "Completed-work echo"
    if normalized in {"possible-overlap", "overlap", "duplicate-candidate"}:
        return "Possible overlap"
    return raw.replace("_", " ").replace("-", " ").capitalize()


def render_batch_markdown(payload: Mapping[str, Any]) -> str:
    """Render one bounded reviewer handoff."""

    batch = payload.get("batch", "?")
    issues = payload.get("issues") or []
    evidence = payload.get("neighbor_evidence") or []
    rubric = payload.get("review_rubric") or []
    batch_kind = payload.get("kind", "connected-component")
    lines = [
        f"# Review batch {_escape(batch)}",
        "",
        f"> {READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        "",
        "## Snapshot",
        "",
        *_metadata_lines(payload),
        "",
        f"## Issues ({len(issues)})",
        "",
    ]
    if batch_kind == "singleton-envelope":
        lines.extend(
            [
                "This is an agent envelope of independent one-issue review units; "
                "its items are not presented as a semantic cluster.",
                "",
            ]
        )
    for issue in issues:
        lines.append(
            f"- `{_escape(_field(issue, 'id', 'issue_id', default='unknown'))}` "
            f"— {_escape(_field(issue, 'title', default='Untitled issue'))} "
            f"({_escape(_field(issue, 'status', default='unknown'))})"
        )
    if not issues:
        lines.append("No issues are assigned to this batch.")
    lines.extend(["", "## Why these items were surfaced", ""])
    if not evidence:
        lines.append("No neighbor evidence was recorded.")
    for item in evidence:
        source = _field(item, "issue_id", "source_id", "id", default="unknown")
        related = _field(item, "related_issue_id", "neighbor_id", "target_id", default="unknown")
        score = _score(item)
        score_text = f"{score:.2f}" if score >= 0 else "not recorded"
        context = _field(item, "structural_context", "relationship", default="none recorded")
        detail = [
            f"### {_kind_label(item)}: `{_escape(source)}` ↔ `{_escape(related)}`",
            "",
            f"- Similarity: {score_text} (advisory)",
            "- Admission reason: "
            + _escape(_field(item, "admission_reason", default="semantic-threshold")),
            f"- Pattern: {_escape(_field(item, 'pattern', default='not classified'))}",
            "- Why surfaced: " + _escape(_field(item, "why_surfaced", default="not recorded")),
            f"- Structural context: {_escape(context)}",
        ]
        dependency = _dependency_text(item)
        if dependency:
            detail.append(f"- Typed dependency: {_escape(dependency)}")
        objective = _field(item, "objective", default=None)
        if objective:
            detail.append(f"- Review objective: {_escape(objective)}")
        provenance = _retrieval_provenance_text(item)
        if provenance:
            detail.append(f"- Retrieval provenance: {_escape(provenance)}")
        candidate_evidence = _field(item, "candidate_evidence", default={}) or {}
        anchor = _field(item, "verification_anchor", default={}) or {}
        detail.extend(
            [
                "- Evidence basis: "
                + _escape(_field(candidate_evidence, "evidence_basis", default="not recorded")),
                "- Structural corroboration: "
                + _escape(
                    _field(candidate_evidence, "structural_corroboration", default="not recorded")
                ),
                "- Verification specificity: "
                + _escape(_field(anchor, "specificity", default="not recorded")),
                "- Verification check category: "
                + _escape(_field(anchor, "check_category", default="not recorded")),
            ]
        )
        detail.extend(
            [
                "- Counterevidence: " + _escape(_counterevidence_text(item)),
                "- What to verify: "
                + _escape(
                    _field(
                        item,
                        "what_to_verify",
                        "verify",
                        default="Compare both records with current project state.",
                    )
                ),
                "",
            ]
        )
        lines.extend(detail)
    lines.extend(["## Review rubric", ""])
    if rubric:
        for item in rubric:
            if isinstance(item, Mapping):
                text = _field(item, "text", "instruction", "question", default=_sort_repr(item))
            else:
                text = item
            lines.append(f"- {_escape(text)}")
    else:
        lines.append("- Verify every observation against current project evidence.")
    lines.extend(["", "Do not implement changes or mutate the tracker as part of this review.", ""])
    return "\n".join(lines)


def _hub_guard_sample_markdown(analysis: Mapping[str, Any]) -> list[str]:
    """Show which hub surfaces suppressed a bounded sample of pairs."""

    sample = _field(analysis, "hub_guard_sample", default=[]) or []
    if not sample:
        return []
    omitted = _field(analysis, "pairs_omitted_by_hub_guard", default=0)
    lines = [
        "### Hub guard sample",
        "",
        f"{len(sample)} of {omitted} suppressed pairs, with the shared hub surfaces that "
        "suppressed them. Suppressed pairs share only hub surfaces, so they are weak "
        "evidence by construction.",
        "",
    ]
    for item in sample:
        surfaces = [
            *(_field(item, "suppressing_paths", default=[]) or []),
            *(_field(item, "suppressing_modules", default=[]) or []),
        ]
        lines.append(
            "- `"
            + _escape(_field(item, "issue_id", default="unknown"))
            + "` ↔ `"
            + _escape(_field(item, "related_issue_id", default="unknown"))
            + "` — shared only: "
            + _escape(", ".join(f"`{item}`" for item in surfaces) or "none")
        )
    lines.append("")
    return lines


def _code_surface_markdown(analysis: Mapping[str, Any]) -> list[str]:
    collisions = _field(analysis, "collisions", default=[]) or []
    hub_surfaces = _field(analysis, "hub_surfaces", default=[]) or []
    lines = [
        "## Code-surface collision evidence",
        "",
        "> Paths and symbols are revision-bound pointers. No source snippets are included.",
        "",
        "- Repository available: "
        + ("yes" if _field(analysis, "repository_available", default=False) else "no"),
        "- Repository context: "
        + str(_field(analysis, "repository_context", default="unavailable")),
        "- Active records analyzed: " + str(_field(analysis, "issue_count", default=0)),
        "- Records with explicit surfaces: "
        + str(_field(analysis, "issues_with_explicit_surfaces", default=0)),
        "- Records with observed worktree surfaces: "
        + str(_field(analysis, "issues_with_observed_surfaces", default=0)),
        "- Records without surfaces: "
        + str(_field(analysis, "issues_without_surfaces", default=0)),
        "- Surface pointers: " + str(_field(analysis, "pointer_count", default=0)),
        "- Git worktrees associated: "
        + str(_field(analysis, "worktrees_associated", default=0))
        + " / "
        + str(_field(analysis, "worktrees_discovered", default=0)),
        "- Hub surfaces summarized: " + str(len(hub_surfaces)),
        "- Pairs omitted by hub guard: "
        + str(_field(analysis, "pairs_omitted_by_hub_guard", default=0)),
        "- Explicit-only module pairs omitted: "
        + str(_field(analysis, "pairs_omitted_by_module_guard", default=0)),
        "- Leads below --min-confidence "
        + str(_field(analysis, "min_confidence", default="explicit"))
        + " omitted: "
        + str(_field(analysis, "pairs_omitted_by_confidence_filter", default=0)),
        "- Collision leads: " + str(len(collisions)),
        "",
    ]
    if hub_surfaces:
        lines.extend(["### Hub surfaces", ""])
        for item in hub_surfaces:
            lines.append(
                "- `"
                + _escape(_field(item, "surface", default="unknown"))
                + "` ("
                + _escape(_field(item, "kind", default="surface"))
                + ", referenced by "
                + str(_field(item, "issue_count", default=0))
                + " active records)"
            )
        lines.append("")
    lines.extend(_hub_guard_sample_markdown(analysis))
    if not collisions:
        lines.extend(["No code-surface collisions were found.", ""])
        return lines
    for collision in collisions:
        issue_id = _field(collision, "issue_id", default="unknown")
        related = _field(collision, "related_issue_id", default="unknown")
        shared_paths = _field(collision, "shared_paths", default=[]) or []
        shared_symbols = _field(collision, "shared_symbols", default=[]) or []
        shared_modules = _field(collision, "shared_modules", default=[]) or []
        lines.extend(
            [
                f"### `{_escape(issue_id)}` ↔ `{_escape(related)}`",
                "",
                "- Collision kind: " + _escape(_field(collision, "kind", default="unknown")),
                "- Confidence: " + _escape(_field(collision, "confidence", default="unknown")),
                "- Edit intent: " + _escape(_field(collision, "edit_intent", default="unknown")),
                "- Reference context: "
                + _escape(_field(collision, "context_kind", default="unknown")),
                "- Repository path presence: "
                + _escape(_field(collision, "path_presence", default="unavailable")),
                "- Intent source fields: "
                + _escape(
                    ", ".join(
                        str(item)
                        for item in _field(collision, "intent_source_fields", default=[]) or []
                    )
                    or "none"
                ),
                "- Revision relation: "
                + _escape(_field(collision, "revision_relation", default="unavailable")),
                "- Shared paths: "
                + _escape(", ".join(str(item) for item in shared_paths) or "none"),
                "- Shared symbols: "
                + _escape(", ".join(str(item) for item in shared_symbols) or "none"),
                "- Shared modules: "
                + _escape(", ".join(str(item) for item in shared_modules) or "none"),
                "- Evidence sources: "
                + _escape(
                    ", ".join(
                        str(item)
                        for item in _field(collision, "evidence_sources", default=[]) or []
                    )
                    or "none"
                ),
                "- What to verify: "
                + _escape(_field(collision, "what_to_verify", default="Coordinate the work.")),
                "",
            ]
        )
    return lines


def render_neighbors_batch_markdown(payload: Mapping[str, Any]) -> str:
    """Render each seed's neighbors as the single-seed report would, one after another."""

    sections = []
    for result in payload.get("results") or []:
        sections.append(
            render_neighbors_markdown(
                {
                    **payload,
                    "report_type": "neighbors",
                    "issue": _field(result, "issue", default={}),
                    "neighbors": _field(result, "neighbors", default=[]),
                }
            )
        )
    return "\n".join(sections)


def render_match_markdown(payload: Mapping[str, Any]) -> str:
    """Render each candidate's nearest existing records as one table per candidate."""

    summary = payload.get("summary") or {}
    lines = [
        "# emBEADings candidate match",
        "",
        f"> {READ_ONLY_NOTICE}",
        "",
        "Similarity is a retrieval lead, not a duplicate verdict. Candidate text was not added "
        "to the tracker, the index, or the vector cache.",
        "",
        *_metadata_lines(payload),
        f"- Candidates: {_field(summary, 'candidate_count', default=0)} "
        f"({_field(summary, 'candidates_with_matches', default=0)} with matches)",
        "",
    ]
    for candidate in payload.get("candidates") or []:
        rows = _field(candidate, "neighbors", default=[]) or []
        lines.extend(
            [
                f"## {_escape(_field(candidate, 'candidate_id', default='unknown'))}",
                "",
                f"- Content hash: `{_escape(_field(candidate, 'content_hash', default=''))}`",
                f"- Status: {_escape(_field(candidate, 'status', default='unknown'))}",
                f"- Records compared: {_field(candidate, 'records_compared', default=0)}",
            ]
        )
        reason = _field(candidate, "no_match_reason", default=None)
        if reason:
            lines.append(f"- No match because: {_escape(reason)}")
        lines.append("")
        if not rows:
            lines.extend(["No existing record met the criteria.", ""])
            continue
        lines.extend(
            [
                "| Rank | Similarity | Issue | Status | Type | Priority | Parent | Title "
                "| Close reason |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for row in rows:
            priority = _field(row, "priority", default=None)
            parent = _field(row, "parent_id", default=None)
            parent_status = _field(row, "parent_status", default=None)
            evidence = _field(row, "resolution_evidence", default=None)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(_field(row, "rank", default="")),
                        f"{float(_field(row, 'similarity', default=0.0)):.3f}",
                        _escape(_field(row, "issue_id", default="unknown")),
                        _escape(_field(row, "status", default="unknown")),
                        _escape(_field(row, "issue_type", default="")),
                        _escape("" if priority is None else f"P{priority}"),
                        _escape("" if not parent else f"{parent} ({parent_status})"),
                        _escape(_field(row, "title", default="")),
                        _escape(_field(evidence, "text", default="") if evidence else ""),
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_orphans_markdown(payload: Mapping[str, Any]) -> str:
    """Render dangling-parent issues as one counts line and one table."""

    summary = payload.get("summary") or {}
    rows = payload.get("dangling_parent") or []
    groups = payload.get("parentless") or []
    by_parent = _field(summary, "parent_status_counts", default={}) or {}
    lines = [
        "# emBEADings orphaned issues",
        "",
        f"> {READ_ONLY_NOTICE}",
        "",
        "Live issues (any status except closed) whose parent is closed or missing from the "
        "tracker. A deferred, in-progress, or blocked parent is live and is not reported.",
        "",
        "- Live issues scanned: " + str(_field(summary, "live_issues_scanned", default=0)),
        "- Dangling parent: "
        + str(_field(summary, "dangling_parent_count", default=len(rows)))
        + " ("
        + (", ".join(f"{status} {count}" for status, count in by_parent.items()) or "none")
        + ")",
        "",
    ]
    if not rows:
        lines.extend(["No live issue has a closed or missing parent.", ""])
    else:
        lines.extend(
            [
                "| Parent | Parent status | Issue | Type | Status | Priority | Title |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in rows:
            priority = _field(row, "priority", default=None)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(_field(row, "parent_id", default="unknown")),
                        _escape(_field(row, "parent_status", default="unknown")),
                        _escape(_field(row, "issue_id", default="unknown")),
                        _escape(_field(row, "issue_type", default="")),
                        _escape(_field(row, "status", default="unknown")),
                        _escape("" if priority is None else f"P{priority}"),
                        _escape(_field(row, "title", default="")),
                    ]
                )
                + " |"
            )
        lines.append("")
    if _field(payload.get("filters") or {}, "include_parentless", default=False):
        lines.extend(
            [
                "## Live issues with no parent",
                "",
                "Top-level work is normal; this section is informational and separate from the "
                "dangling-parent list above.",
                "",
            ]
        )
        if not groups:
            lines.extend(["None.", ""])
        for group in groups:
            lines.extend(
                [
                    f"### {_escape(_field(group, 'issue_type', default='unknown'))} "
                    f"({_field(group, 'count', default=0)})",
                    "",
                    "| Issue | Status | Priority | Title |",
                    "|---|---|---|---|",
                ]
            )
            for item in _field(group, "issues", default=[]) or []:
                priority = _field(item, "priority", default=None)
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            _escape(_field(item, "issue_id", default="unknown")),
                            _escape(_field(item, "status", default="unknown")),
                            _escape("" if priority is None else f"P{priority}"),
                            _escape(_field(item, "title", default="")),
                        ]
                    )
                    + " |"
                )
            lines.append("")
    lines.extend(
        [
            "## What to do next",
            "",
            "Open each issue and re-parent it, reopen the parent, or close the issue; "
            "emBEADings does none of these.",
            "",
        ]
    )
    return "\n".join(lines)


def render_mentions_markdown(payload: Mapping[str, Any]) -> str:
    """Render text lineage claims as one counts line and one table."""

    summary = payload.get("summary") or {}
    claims = payload.get("claims") or []
    kinds = _field(summary, "kind_counts", default={}) or {}
    lines = [
        "# emBEADings text lineage claims",
        "",
        f"> {READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        "",
        "Records whose text names another record as a duplicate, successor, absorber, or fix. "
        "Only IDs and claim kinds are reported; the surrounding text is not.",
        "",
        "- Claims found: "
        + str(_field(summary, "claims_found", default=len(claims)))
        + " ("
        + (", ".join(f"{kind} {count}" for kind, count in kinds.items()) or "none")
        + ")",
        "- Already backed by a typed link: "
        + str(_field(summary, "claims_with_typed_link", default=0)),
        "- Shown: "
        + str(len(claims))
        + "; omitted by --limit: "
        + str(_field(summary, "omitted_by_limit", default=0)),
        "",
    ]
    if not claims:
        lines.extend(["No text lineage claims matched the selected filters.", ""])
    else:
        lines.extend(
            [
                "| Record | Status | Claim | Names | Status | Fields | Typed link |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for claim in claims:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(_field(claim, "issue_id", default="unknown")),
                        _escape(_field(claim, "issue_status", default="unknown")),
                        _escape(_field(claim, "kind", default="unknown")),
                        _escape(_field(claim, "related_issue_id", default="unknown")),
                        _escape(_field(claim, "related_status", default="unknown")),
                        _escape(", ".join(_field(claim, "source_fields", default=[]) or [])),
                        "yes" if _field(claim, "typed_link", default=False) else "no",
                    ]
                )
                + " |"
            )
        lines.append("")
    lines.extend(
        [
            "## What to do next",
            "",
            "Open both records. Where the claim still holds, record it as a typed link or close "
            "the absorbed side; emBEADings does neither.",
            "",
        ]
    )
    return "\n".join(lines)


def render_collisions_markdown(payload: Mapping[str, Any]) -> str:
    """Render a focused code-surface collision report."""

    analysis = payload.get("code_surface_analysis") or {}
    snapshot = payload.get("snapshot") or {}
    tracker_name = _field(snapshot, "tracker_name", default="beads") or "beads"
    tracker_version = _field(snapshot, "tracker_version", default=None) or _field(
        snapshot, "beads_version", default="unknown"
    )
    warnings = _field(analysis, "warnings", default=[]) or []
    lines = [
        "# emBEADings code-surface collision review",
        "",
        f"> {READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        "",
        *_code_surface_markdown(analysis),
        "## Snapshot",
        "",
        "- Workspace snapshot: `"
        + _escape(_field(snapshot, "workspace_id", default="unknown"))
        + "` ("
        + _escape(tracker_name)
        + " `"
        + _escape(tracker_version)
        + "`)",
        "- Acquisition source: `"
        + _escape(_field(snapshot, "acquisition_source", default="unknown"))
        + "`",
        "- Repository revision: `"
        + _escape(_field(analysis, "repository_revision", default="unavailable"))
        + "`",
        "- Repository context: `"
        + _escape(_field(analysis, "repository_context", default="unavailable"))
        + "`",
        "- Base reference: `"
        + _escape(_field(analysis, "base_reference", default="unavailable"))
        + "`",
        "- Base revision: `"
        + _escape(_field(analysis, "base_revision", default="unavailable"))
        + "`",
    ]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {_escape(warning)}" for warning in warnings)
    lines.extend(
        [
            "",
            "Coordinate against the referenced revision before implementation or merge; "
            "shared paths do not prove conflicting intent.",
            "",
        ]
    )
    return "\n".join(lines)


def render_triage_markdown(payload: Mapping[str, Any]) -> str:
    """Render the compact triage packet without expanding the full audit report."""

    candidates = _field(payload, "candidates", default=[]) or []
    collisions = _field(payload, "collisions", default=[]) or []
    lanes = _field(payload, "lane_summary", default={}) or {}
    population = _field(payload, "population", default={}) or {}
    warnings = _field(payload, "warnings", default=[]) or []
    degradations = _field(payload, "degradations", default=[]) or []
    lines = [
        "# emBEADings triage packet",
        "",
        f"> {READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        "",
        f"Analysis fingerprint: `{_escape(_field(payload, 'analysis_fingerprint'))}`",
        "",
        f"Analysis status: **{_escape(_field(payload, 'analysis_status', default='complete'))}**",
        "",
        "## Queue",
        "",
        f"- Review candidates: {len(candidates)}",
        f"- Code-surface leads: {len(collisions)}",
        f"- No-signal records: {_field(population, 'no_signal', default=0)}",
        f"- Excluded records: {_field(population, 'excluded', default=0)}",
    ]
    for lane in ("dependency", "echo", "overlap"):
        metrics = _field(lanes, lane, default={}) or {}
        lines.append(
            f"- {lane.capitalize()}: {_field(metrics, 'admitted', default=0)} admitted / "
            f"{_field(metrics, 'qualified', default=0)} qualified; "
            f"{_field(metrics, 'omitted', default=0)} omitted"
        )
    if degradations:
        lines.extend(["", "## Degraded analysis", ""])
        for receipt in degradations:
            retained = ", ".join(_field(receipt, "retained_lanes", default=[]) or []) or "none"
            balance = describe_conservation_balance(_field(receipt, "balance"))
            lines.append(
                "- "
                + _escape(_field(receipt, "lane", default="unknown"))
                + " lane unavailable at "
                + _escape(_field(receipt, "stage", default="unknown"))
                + " conservation"
                + (f" ({_escape(balance)})" if balance else "")
                + "; retained lanes: "
                + _escape(retained)
            )
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {_escape(warning)}" for warning in warnings)
    lines.extend(["", "## Candidates", ""])
    if not candidates:
        lines.append("No semantic or structural review candidates were found.")
    for candidate in candidates:
        lines.extend(
            [
                "- `"
                + _escape(_field(candidate, "issue_id"))
                + "` ↔ `"
                + _escape(_field(candidate, "related_issue_id"))
                + "` — "
                + _escape(_field(candidate, "lane", default="unknown"))
                + ", similarity "
                + f"{float(_field(candidate, 'similarity', default=0.0)):.3f}",
                "  - Verify: "
                + _escape(_field(candidate, "what_to_verify", default=ADVISORY_NOTICE)),
            ]
        )
    lines.extend(["", "## Code-surface leads", ""])
    if not collisions:
        lines.append("No code-surface leads were found.")
    for collision in collisions:
        paths = ", ".join(_field(collision, "shared_paths", default=[]) or []) or "module only"
        lines.extend(
            [
                "- `"
                + _escape(_field(collision, "issue_id"))
                + "` ↔ `"
                + _escape(_field(collision, "related_issue_id"))
                + "` — "
                + _escape(_field(collision, "confidence", default="unknown"))
                + ", intent "
                + _escape(_field(collision, "edit_intent", default="unknown"))
                + ", context "
                + _escape(_field(collision, "context_kind", default="unknown"))
                + ", paths "
                + _escape(_field(collision, "path_presence", default="unavailable")),
                f"  - Surfaces: {_escape(paths)}",
                "  - Verify: "
                + _escape(_field(collision, "what_to_verify", default="Coordinate the work.")),
            ]
        )
    lines.append("")
    return "\n".join(lines)


def render_sweep_markdown(payload: Mapping[str, Any]) -> str:
    """Render the sweep outcome and bounded candidate evidence."""

    candidates = payload.get("candidates") or []
    batches = payload.get("batches") or []
    warnings = payload.get("warnings") or []
    no_signal = payload.get("no_signal") or {}
    excluded = payload.get("excluded") or {}
    excluded_by_reason = _field(excluded, "by_reason", default={}) or {}
    candidate_policy = _field(payload.get("parameters") or {}, "candidate_policy", default={}) or {}
    filters = _field(payload.get("parameters") or {}, "filters", default={}) or {}
    incremental_scope = _field(filters, "incremental_scope", default={}) or {}
    lane_metrics = _field(candidate_policy, "lanes", default={}) or {}
    dependency_funnel = _field(candidate_policy, "dependency_funnel", default={}) or {}
    echo_target_hubs = _field(candidate_policy, "echo_target_hubs", default=[]) or []
    echo_backfills = _field(candidate_policy, "echo_backfills", default=[]) or []
    echo_disposition_omissions = (
        _field(candidate_policy, "echo_disposition_omissions", default={}) or {}
    )
    review_budget = _field(candidate_policy, "review_budget", default={}) or {}
    capped_dependencies = payload.get("capped_typed_dependencies") or []
    diagnostics = payload.get("batch_diagnostics") or {}
    anchor_metrics = payload.get("anchor_metrics") or {}
    code_surface_analysis = payload.get("code_surface_analysis") or {}
    degradations = payload.get("degradations") or []
    echo_count = sum(_kind_label(item) == "Completed-work echo" for item in candidates)
    overlap_count = sum(_kind_label(item) == "Possible overlap" for item in candidates)
    lines = [
        f"# emBEADings sweep {_escape(payload.get('run_id', 'unknown'))}",
        "",
        f"> {READ_ONLY_NOTICE} {ADVISORY_NOTICE}",
        "",
        f"Analysis status: **{_escape(payload.get('analysis_status', 'complete'))}**",
        "",
        "## Outcome",
        "",
        f"Found {len(candidates)} review candidates across {len(batches)} batches.",
        "",
        f"- Completed-work echoes: {echo_count}",
        f"- Possible overlaps: {overlap_count}",
        f"- Other review candidates: {len(candidates) - echo_count - overlap_count}",
        f"- No-signal records: {_field(no_signal, 'count', default=0)}",
        f"- Excluded records: {_field(excluded, 'count', default=0)}",
        "- Ephemeral runtime records excluded: "
        + str(_field(excluded_by_reason, "ephemeral", default=0)),
        f"- Qualified typed dependencies omitted by caps: {len(capped_dependencies)}",
        f"- Review scope: {_field(incremental_scope, 'mode', default='full')}",
        "- Changed active records in scope: "
        + str(_field(incremental_scope, "changed_active_count", default="not recorded")),
        "- Unchanged active records excluded: "
        + str(_field(incremental_scope, "unchanged_active_count", default=0)),
        f"- Singleton components: {_field(diagnostics, 'singleton_component_count', default=0)}",
        f"- Singleton agent envelopes: {_field(diagnostics, 'agent_envelope_count', default=0)}",
        "- Cross-batch candidate edges: "
        + str(_field(diagnostics, "cross_batch_candidate_edges", default=0)),
        "- Generic verification-anchor fallbacks: "
        + str(_field(anchor_metrics, "generic_fallback_count", default=0))
        + " / "
        + str(_field(anchor_metrics, "total", default=len(candidates))),
        "- Verification specificity: "
        + ", ".join(
            f"{tier}={count}"
            for tier, count in (_field(anchor_metrics, "specificity", default={}) or {}).items()
        ),
        "- Actionable-anchor proxy: "
        + str(_field(anchor_metrics, "actionable_proxy_count", default=0))
        + " / "
        + str(_field(anchor_metrics, "total", default=len(candidates)))
        + " (non-generic finite-vocabulary anchors; not a human rating)",
        "",
    ]
    if degradations:
        lines.extend(["### Degraded analysis", ""])
        for receipt in degradations:
            retained = ", ".join(_field(receipt, "retained_lanes", default=[]) or []) or "none"
            balance = describe_conservation_balance(_field(receipt, "balance"))
            lines.append(
                "- "
                + _escape(_field(receipt, "lane", default="unknown"))
                + " lane unavailable at "
                + _escape(_field(receipt, "stage", default="unknown"))
                + " conservation"
                + (f" ({_escape(balance)})" if balance else "")
                + "; retained lanes: "
                + _escape(retained)
            )
        lines.append("")
    if code_surface_analysis:
        lines.extend(_code_surface_markdown(code_surface_analysis))
    if review_budget:
        priority = " → ".join(_field(review_budget, "priority_order", default=[]) or [])
        omitted_by_lane = _field(review_budget, "omitted_by_lane", default={}) or {}
        lane_capacity = _field(review_budget, "lane_capacity", default={}) or {}
        lines.extend(
            [
                "## Review budget",
                "",
                f"- Mode: {_field(review_budget, 'mode', default='standard')}",
                "- Candidate limit: "
                + str(_field(review_budget, "candidate_limit", default="not recorded")),
                "- Admitted candidates: "
                + str(_field(review_budget, "admitted_candidates", default=len(candidates))),
                "- Omitted by total budget: "
                + str(_field(review_budget, "omitted_candidates", default=0)),
                "- Selection: reserved lane access, then unused-capacity reflow",
                f"- Reflow priority: {_escape(priority or 'not recorded')}",
                "- Code-surface leads: separately prioritized outside the semantic candidate limit",
                "- Omitted by lane: "
                + ", ".join(
                    f"{lane}={_field(omitted_by_lane, lane, default=0)}"
                    for lane in ("dependency", "echo", "overlap")
                ),
                "",
            ]
        )
        if lane_capacity:
            lines.extend(["### Reserved lane capacity", ""])
            for lane in ("dependency", "echo", "overlap"):
                capacity = _field(lane_capacity, lane, default={}) or {}
                lines.append(
                    f"- {lane.capitalize()}: "
                    f"{_field(capacity, 'reserved', default=0)} reserved; "
                    f"{_field(capacity, 'admitted_to_reservation', default=0)} admitted to "
                    "reservation; "
                    f"{_field(capacity, 'unused', default=0)} unused"
                )
            lines.append("")
    lines.extend(["## Candidate lanes", ""])
    if lane_metrics:
        for lane in ("dependency", "echo", "overlap"):
            metrics = _field(lane_metrics, lane, default={}) or {}
            lines.append(
                f"- {lane.capitalize()}: {_field(metrics, 'admitted', default=0)} admitted / "
                f"{_field(metrics, 'qualified', default=0)} qualified; "
                f"{_field(metrics, 'dropped_by_lane_cap', default=0)} dropped by lane budget; "
                f"{_field(metrics, 'dropped_by_issue_cap', default=0)} dropped by per-issue "
                "budget; "
                f"{_field(metrics, 'dropped_by_target_cap', default=0)} dropped by echo-target "
                "diversity"
            )
        dependency_metrics = _field(lane_metrics, "dependency", default={}) or {}
        lines.append(
            "- Typed dependencies dropped by their independent per-issue allowance: "
            f"{_field(dependency_metrics, 'dropped_by_dependency_issue_cap', default=0)}"
        )
        lines.append(
            "- Baseline candidates protected in sensitivity mode: "
            f"{_field(candidate_policy, 'baseline_protected', default=0)}"
        )
    else:
        lines.append("Lane metrics were not recorded by this producer.")
    if echo_target_hubs:
        lines.extend(["", "### Repeated completed targets", ""])
        for hub in echo_target_hubs:
            omissions = _field(hub, "omissions_by_reason", default={}) or {}
            lines.append(
                f"- `{_escape(_field(hub, 'related_issue_id', default='unknown'))}`: "
                f"{_field(hub, 'qualified', default=0)} qualified = "
                f"{_field(hub, 'admitted', default=0)} admitted + "
                f"{_field(hub, 'omitted', default=0)} omitted "
                "(target diversity="
                f"{_field(omissions, 'completed-target-cap', default=0)}, "
                f"one echo per active={_field(omissions, 'one-echo-per-active', default=0)}, "
                f"per-issue={_field(omissions, 'per-issue-cap', default=0)}, "
                f"lane={_field(omissions, 'lane-cap', default=0)}, "
                f"run={_field(omissions, 'run-cap', default=0)})"
            )
    if echo_disposition_omissions:
        lines.extend(["", "### Explicit close dispositions", ""])
        lines.append(
            "Pair-local lifecycle metadata omitted contradictory completed-work echoes without "
            "including close-reason text."
        )
        lines.append("")
        for reason, count in sorted(echo_disposition_omissions.items()):
            lines.append(f"- `{_escape(reason)}`: {count}")
    if echo_backfills:
        lines.extend(["", "### Completed-target backfills", ""])
        lines.append(
            "Backfills increase target coverage; they are not evidence that the admitted pair is "
            "more relevant than the omitted pair."
        )
        lines.append("")
        for backfill in echo_backfills:
            omitted_ids = ", ".join(
                f"`{_escape(_field(item, 'candidate_id', default='unknown'))}`"
                for item in (_field(backfill, "omitted_candidates", default=[]) or [])
            )
            admitted_id = _escape(_field(backfill, "admitted_candidate_id", default="unknown"))
            lines.append(
                f"- Active `{_escape(_field(backfill, 'issue_id', default='unknown'))}`: "
                f"admitted `{admitted_id}` "
                f"after target-cap omission(s): {omitted_ids or 'none recorded'}"
            )
    lines.extend(["", "## Typed dependency funnel", ""])
    if dependency_funnel:
        total = _field(dependency_funnel, "total_non_parent_typed", default=0)
        inactive = _field(dependency_funnel, "inactive_or_closed_only", default=0)
        below = _field(dependency_funnel, "below_qualification", default=0)
        eligible = _field(dependency_funnel, "eligible", default=0)
        eligible_candidates = _field(dependency_funnel, "eligible_candidates", default=eligible)
        coalesced = _field(dependency_funnel, "coalesced_eligible_edges", default=0)
        admitted = _field(dependency_funnel, "admitted", default=0)
        per_issue = _field(dependency_funnel, "omitted_by_per_issue_cap", default=0)
        lane_cap = _field(dependency_funnel, "omitted_by_lane_cap", default=0)
        run_cap = _field(dependency_funnel, "omitted_by_run_cap", default=0)
        lines.extend(
            [
                f"- Total non-parent typed edges: {total}",
                f"- Inactive, closed-only, or outside review scope: {inactive}",
                f"- Below structural qualification floor: {below}",
                f"- Eligible typed edges: {eligible}",
                f"- Eligible candidate pairs: {eligible_candidates}",
                f"- Additional eligible edges coalesced by pair: {coalesced}",
                f"- Admitted: {admitted}",
                f"- Omitted by dependency per-issue allowance: {per_issue}",
                f"- Omitted by dependency lane cap: {lane_cap}",
                f"- Omitted by run cap: {run_cap}",
                f"- Discovery conservation: {total} = {inactive} + {below} + {eligible}",
                "- Edge-to-candidate conservation: "
                f"{eligible} = {eligible_candidates} + {coalesced}",
                "- Admission conservation: "
                f"{eligible_candidates} = {admitted} + {per_issue} + {lane_cap} + {run_cap}",
            ]
        )
    else:
        lines.append("Dependency funnel metrics were not recorded by this producer.")
    reciprocal = _field(candidate_policy, "reciprocal_diagnostics", default={}) or {}
    lines.extend(
        [
            "",
            "## Reciprocal-neighbor guard",
            "",
            "- Admitted by substantive local evidence: "
            + str(_field(reciprocal, "admitted", default=0)),
            "- Omitted without discriminative local evidence: "
            + str(_field(reciprocal, "omitted", default=0)),
        ]
    )
    replacements = _field(candidate_policy, "cap_replacements", default=[]) or []
    if replacements:
        lines.extend(["", "## Threshold cap replacements", ""])
        for item in replacements:
            displaced = ", ".join(_field(item, "displaced_candidate_ids", default=[]))
            lines.append(
                f"- `{_escape(_field(item, 'candidate_id'))}` entered because "
                f"`{_escape(_field(item, 'governing_cap'))}` bound; displaced: "
                f"{_escape(displaced or 'none recorded')}"
            )
            for step in _field(item, "causal_chain", default=[]) or []:
                lines.append(
                    "  - "
                    + _escape(_field(step, "event", default="selection-transition"))
                    + ": `"
                    + _escape(_field(step, "candidate_id", default="unknown"))
                    + "` via `"
                    + _escape(_field(step, "resource", default="unknown"))
                    + "`"
                )
    if capped_dependencies:
        lines.extend(["", "## Capped typed dependencies", ""])
        for item in capped_dependencies:
            lines.append(
                f"- `{_escape(_field(item, 'source_id'))}` → "
                f"`{_escape(_field(item, 'target_id'))}` "
                f"({_escape(_field(item, 'type'))}); "
                f"not promoted: {_escape(_field(item, 'drop_reason'))}"
            )
    lines.extend(
        [
            "",
            "## Snapshot",
            "",
            *_metadata_lines(payload),
            "",
            "## Candidates",
            "",
        ]
    )
    if not candidates:
        lines.extend(["No review candidates were found in the selected population.", ""])
    for item in candidates:
        issue_id = _field(item, "issue_id", "id", default="unknown")
        related = _field(item, "related_issue_id", "neighbor_id", default="unknown")
        score = _score(item)
        score_text = f"{score:.2f}" if score >= 0 else "not recorded"
        detail = [
            f"### {_kind_label(item)}: `{_escape(issue_id)}` ↔ `{_escape(related)}`",
            "",
            f"- Similarity: {score_text} (advisory)",
            "- Admission reason: "
            + _escape(_field(item, "admission_reason", default="semantic-threshold")),
            f"- Pattern: {_escape(_field(item, 'pattern', default='not classified'))}",
            "- Why surfaced: " + _escape(_field(item, "why_surfaced", default="not recorded")),
            "- Structural context: "
            + _escape(
                _field(
                    item,
                    "structural_context",
                    "relationship",
                    default="none recorded",
                )
            ),
        ]
        dependency = _dependency_text(item)
        if dependency:
            detail.append(f"- Typed dependency: {_escape(dependency)}")
        objective = _field(item, "objective", default=None)
        if objective:
            detail.append(f"- Review objective: {_escape(objective)}")
        provenance = _retrieval_provenance_text(item)
        if provenance:
            detail.append(f"- Retrieval provenance: {_escape(provenance)}")
        candidate_evidence = _field(item, "candidate_evidence", default={}) or {}
        anchor = _field(item, "verification_anchor", default={}) or {}
        detail.extend(
            [
                "- Evidence basis: "
                + _escape(_field(candidate_evidence, "evidence_basis", default="not recorded")),
                "- Structural corroboration: "
                + _escape(
                    _field(candidate_evidence, "structural_corroboration", default="not recorded")
                ),
                "- Verification specificity: "
                + _escape(_field(anchor, "specificity", default="not recorded")),
                "- Verification check category: "
                + _escape(_field(anchor, "check_category", default="not recorded")),
            ]
        )
        detail.extend(
            [
                "- Counterevidence: " + _escape(_counterevidence_text(item)),
                "- What to verify: "
                + _escape(
                    _field(
                        item,
                        "what_to_verify",
                        "verify",
                        default="Compare both records with current project state.",
                    )
                ),
                "",
            ]
        )
        lines.extend(detail)
    lines.extend(["## Batches", ""])
    if not batches:
        lines.append("No batches were generated.")
    for batch in batches:
        number = _field(batch, "batch", "batch_number", default="?")
        members = _field(batch, "issues", "issue_ids", default=[]) or []
        issue_count = len(members)
        kind = _field(batch, "kind", default="connected-component")
        lines.append(f"- Batch {_escape(number)}: {issue_count} issues ({_escape(kind)})")
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {_escape(warning)}" for warning in warnings)
    lines.extend(
        [
            "",
            "Verify candidates against current project state; this report does not decide "
            "whether any issue should change.",
            "",
        ]
    )
    return "\n".join(lines)
