"""Deterministic, metadata-only incremental review scoping."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import IssueRecord

CHECKPOINT_VERSION = 1


@dataclass(frozen=True, slots=True)
class IncrementalScope:
    mode: str
    changed_ids: frozenset[str]
    unchanged_ids: frozenset[str]
    unknown_timestamp_ids: frozenset[str]
    deleted_ids: frozenset[str]
    checkpoint_created_at: str | None = None


def parse_rfc3339(value: str, *, now: datetime | None = None) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("incremental timestamp must be valid RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("incremental timestamp must include a timezone")
    normalized = parsed.astimezone(UTC)
    if normalized > (now or datetime.now(UTC)):
        raise ValueError("incremental timestamp cannot be in the future")
    return normalized


def record_fingerprint(issue: IssueRecord) -> str:
    """Hash review-relevant state without placing issue text in the checkpoint."""

    values = asdict(issue)
    # Preserve legacy/Linear fingerprints when a Beads-only optional field is
    # absent while still observing every transition to, from, or between reasons.
    if not issue.close_reason:
        values.pop("close_reason")
    if not issue.assignee:
        values.pop("assignee")
    if issue.updated_at:
        values["updated_at"] = normalize_rfc3339(issue.updated_at)
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def scope_since_timestamp(
    issues: Sequence[IssueRecord], value: str, *, now: datetime | None = None
) -> IncrementalScope:
    cutoff = parse_rfc3339(value, now=now)
    changed: set[str] = set()
    unchanged: set[str] = set()
    unknown: set[str] = set()
    for issue in issues:
        if not issue.updated_at:
            changed.add(issue.id)
            unknown.add(issue.id)
        elif parse_rfc3339(issue.updated_at, now=datetime.max.replace(tzinfo=UTC)) > cutoff:
            changed.add(issue.id)
        else:
            unchanged.add(issue.id)
    return IncrementalScope(
        mode="changed-since",
        changed_ids=frozenset(changed),
        unchanged_ids=frozenset(unchanged),
        unknown_timestamp_ids=frozenset(unknown),
        deleted_ids=frozenset(),
    )


def normalize_rfc3339(value: str) -> str:
    return (
        parse_rfc3339(value, now=datetime.max.replace(tzinfo=UTC))
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_checkpoint(
    path: Path,
    issues: Sequence[IssueRecord],
    *,
    workspace_id: str,
    now: datetime | None = None,
) -> IncrementalScope:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("incremental checkpoint is unreadable or invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise ValueError("incremental checkpoint version is unsupported")
    if payload.get("workspace_id") != workspace_id:
        raise ValueError("incremental checkpoint belongs to a different workspace")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("incremental checkpoint created_at is missing")
    parse_rfc3339(created_at, now=now)
    prior = payload.get("issues")
    if not isinstance(prior, Mapping):
        raise ValueError("incremental checkpoint issue snapshot is invalid")
    for identifier, entry in prior.items():
        if not isinstance(identifier, str) or not isinstance(entry, Mapping):
            raise ValueError("incremental checkpoint issue snapshot is invalid")
        timestamp = entry.get("updated_at")
        fingerprint = entry.get("fingerprint")
        if timestamp is not None and not isinstance(timestamp, str):
            raise ValueError("incremental checkpoint issue timestamp is invalid")
        if timestamp:
            parse_rfc3339(timestamp, now=datetime.max.replace(tzinfo=UTC))
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("incremental checkpoint issue fingerprint is invalid")
        try:
            int(fingerprint, 16)
        except ValueError as exc:
            raise ValueError("incremental checkpoint issue fingerprint is invalid") from exc

    current = {issue.id: issue for issue in issues}
    changed: set[str] = set()
    unchanged: set[str] = set()
    unknown: set[str] = set()
    for identifier, issue in current.items():
        entry = prior.get(identifier)
        same = isinstance(entry, Mapping) and entry.get("fingerprint") == record_fingerprint(issue)
        (unchanged if same else changed).add(identifier)
        if not issue.updated_at:
            unknown.add(identifier)
    return IncrementalScope(
        mode="checkpoint",
        changed_ids=frozenset(changed),
        unchanged_ids=frozenset(unchanged),
        unknown_timestamp_ids=frozenset(unknown),
        deleted_ids=frozenset(set(prior) - set(current)),
        checkpoint_created_at=created_at,
    )


def build_checkpoint(
    issues: Sequence[IssueRecord], *, workspace_id: str, created_at: datetime | None = None
) -> dict[str, Any]:
    instant = (created_at or datetime.now(UTC)).astimezone(UTC)
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "workspace_id": workspace_id,
        "created_at": instant.isoformat().replace("+00:00", "Z"),
        "issues": {
            issue.id: {
                "updated_at": normalize_rfc3339(issue.updated_at) if issue.updated_at else None,
                "fingerprint": record_fingerprint(issue),
            }
            for issue in sorted(issues, key=lambda item: item.id)
        },
    }


def ensure_external_path(
    path: Path,
    workspace_path: str | None,
    *,
    purpose: str = "checkpoint output",
) -> None:
    """Reject checkpoint inputs or outputs inside the repository containing `.beads`."""

    if not workspace_path:
        return
    workspace = Path(workspace_path).resolve()
    repository = workspace.parent if workspace.name == ".beads" else workspace
    target = path.expanduser().resolve()
    if target == repository or repository in target.parents:
        raise ValueError(f"{purpose} must be outside the Beads repository")
