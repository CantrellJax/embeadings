"""Core, dependency-free data models for emBEADings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Final

SCHEMA_VERSION: Final[int] = 1
CANONICALIZATION_VERSION: Final[int] = 1
DEFAULT_FIELD_LIMIT: Final[int] = 16_000


@dataclass(frozen=True, slots=True)
class DependencyLink:
    """A typed, directed relationship emitted by a tracker for one issue."""

    source_id: str
    target_id: str
    relationship_type: str


@dataclass(frozen=True, slots=True)
class RelationDiagnostics:
    """Conserved accounting for tracker relations retained or omitted at a scope boundary."""

    raw_relation_count: int
    retained_relation_count: int
    retained_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    collapsed_relation_count: int = 0
    omitted_relation_count: int = 0
    omitted_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    boundary_relation_count: int = 0
    outbound_boundary_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    inbound_boundary_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    unrelated_external_relation_count: int = 0
    unrelated_external_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class IssueRecord:
    """The tracker fields used by semantic and structural analysis."""

    id: str
    title: str
    description: str = ""
    status: str = ""
    issue_type: str = ""
    priority: int | None = None
    labels: tuple[str, ...] = field(default_factory=tuple)
    parent_id: str | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    dependency_links: tuple[DependencyLink, ...] = field(default_factory=tuple)
    acceptance_criteria: str = ""
    design: str = ""
    notes: str = ""
    updated_at: str = ""
    ephemeral: bool = False
    close_reason: str = ""
    assignee: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Identity and tool metadata for the tracker snapshot being analyzed."""

    workspace_id: str
    beads_version: str | None
    workspace_path: str | None = None
    dependency_count: int | None = None
    dependency_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    acquisition_source: str = "live-beads-cli"
    live_issue_count: int | None = None
    export_issue_count: int | None = None
    live_source_digest: str | None = None
    export_source_digest: str | None = None
    source_divergence_reasons: tuple[str, ...] = field(default_factory=tuple)
    source_warnings: tuple[str, ...] = field(default_factory=tuple)
    relation_diagnostics: RelationDiagnostics | None = None
    tracker_name: str = "beads"
    tracker_version: str = ""


def _normalize_text(value: str, *, limit: int) -> str:
    """Normalize newlines and trailing whitespace, then truncate deterministically."""

    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized = "\n".join(line.rstrip() for line in lines).strip()
    return normalized[:limit]


def canonical_text(issue: IssueRecord, *, field_limit: int = DEFAULT_FIELD_LIMIT) -> str:
    """Build stable semantic text, with a modest second copy of the title."""

    if field_limit < 1:
        raise ValueError("field_limit must be positive")

    title = _normalize_text(issue.title, limit=field_limit)
    sections = (
        ("Title", title),
        ("Title emphasis", title),
        ("Description", _normalize_text(issue.description, limit=field_limit)),
        (
            "Acceptance criteria",
            _normalize_text(issue.acceptance_criteria, limit=field_limit),
        ),
        ("Design", _normalize_text(issue.design, limit=field_limit)),
        ("Notes", _normalize_text(issue.notes, limit=field_limit)),
    )
    return "\n\n".join(f"{heading}:\n{text}" for heading, text in sections if text)


def semantic_field_texts(
    issue: IssueRecord, *, field_limit: int = DEFAULT_FIELD_LIMIT
) -> dict[str, str]:
    """Return stable, addressable semantic views for experimental retrieval.

    Notes are intentionally excluded from field-local retrieval. They remain in
    :func:`canonical_text`, but evaluator feedback showed that historical and
    administrative notes can overwhelm the current intent of a work item.
    """

    if field_limit < 1:
        raise ValueError("field_limit must be positive")
    fields = (
        ("title", issue.title),
        ("description", issue.description),
        ("acceptance_criteria", issue.acceptance_criteria),
        ("design", issue.design),
    )
    return {
        name: text for name, value in fields if (text := _normalize_text(value, limit=field_limit))
    }


def content_hash(
    issue: IssueRecord,
    *,
    model_id: str,
    model_revision: str,
    schema_version: int = SCHEMA_VERSION,
    canonicalization_version: int = CANONICALIZATION_VERSION,
    field_limit: int = DEFAULT_FIELD_LIMIT,
) -> str:
    """Hash every input that determines an issue's cached embedding."""

    if not model_id or not model_revision:
        raise ValueError("model_id and model_revision must be non-empty")
    payload = {
        "schema_version": schema_version,
        "canonicalization_version": canonicalization_version,
        "model_id": model_id,
        "model_revision": model_revision,
        "canonical_text": canonical_text(issue, field_limit=field_limit),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
