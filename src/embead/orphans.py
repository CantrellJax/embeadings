"""Structural orphan detection: live issues whose parent is closed or missing.

This is pure structure over the full tracker listing; it needs no embedding model.  A parent that
is deferred, in progress, or blocked is a live parent, so the whole workspace (closed records
included) must be loaded before judging anyone: an open-only listing hides those parents and makes
every child of a deferred epic look orphaned.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

CLOSED_STATUS = "closed"
PARENT_STATUSES = ("closed", "missing")


@dataclass(frozen=True, slots=True)
class DanglingParent:
    issue_id: str
    issue_type: str
    status: str
    priority: int | None
    title: str
    parent_id: str
    parent_status: str


@dataclass(frozen=True, slots=True)
class Parentless:
    issue_id: str
    issue_type: str
    status: str
    priority: int | None
    title: str


def is_live(issue: Any) -> bool:
    """Every status except closed counts as live (open, in_progress, blocked, deferred, ...)."""

    return str(issue.status).casefold() != CLOSED_STATUS


def find_dangling_parents(
    issues: Sequence[Any], *, reportable: Iterable[Any] | None = None
) -> tuple[DanglingParent, ...]:
    """Return live issues whose direct parent is closed or absent from ``issues``.

    ``issues`` is the full tracker listing used to look parents up.  ``reportable`` narrows which
    children may be reported (for example, to drop ephemeral records) without hiding their
    parents.  Only the nearest link is judged: a child of a live parent is never reported, even
    when that parent is itself orphaned, so each broken link appears once.
    """

    by_id = {str(issue.id): issue for issue in issues}
    rows = []
    for issue in issues if reportable is None else reportable:
        parent_id = getattr(issue, "parent_id", None)
        if not parent_id or not is_live(issue):
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            parent_status = "missing"
        elif not is_live(parent):
            parent_status = "closed"
        else:
            continue
        rows.append(
            DanglingParent(
                issue_id=str(issue.id),
                issue_type=issue.issue_type,
                status=issue.status,
                priority=issue.priority,
                title=issue.title,
                parent_id=parent_id,
                parent_status=parent_status,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.parent_id, row.issue_id)))


def find_parentless(issues: Iterable[Any]) -> tuple[Parentless, ...]:
    """Return live issues with no parent, ordered by issue type then ID."""

    rows = [
        Parentless(
            issue_id=str(issue.id),
            issue_type=issue.issue_type,
            status=issue.status,
            priority=issue.priority,
            title=issue.title,
        )
        for issue in issues
        if not getattr(issue, "parent_id", None) and is_live(issue)
    ]
    return tuple(sorted(rows, key=lambda row: (row.issue_type, row.issue_id)))


def group_parentless(rows: Iterable[Parentless]) -> list[dict[str, Any]]:
    """Group parentless rows by issue type for the report; empty types are labelled ``unknown``."""

    groups: dict[str, list[Parentless]] = {}
    for row in sorted(rows, key=lambda item: (item.issue_type, item.issue_id)):
        groups.setdefault(row.issue_type or "unknown", []).append(row)
    return [
        {
            "issue_type": issue_type,
            "count": len(items),
            "issues": [
                {
                    "issue_id": item.issue_id,
                    "status": item.status,
                    "priority": item.priority,
                    "title": item.title,
                }
                for item in items
            ],
        }
        for issue_type, items in sorted(groups.items())
    ]


def parent_status_counts(rows: Iterable[DanglingParent]) -> dict[str, int]:
    counts = Counter(row.parent_status for row in rows)
    return {status: counts[status] for status in PARENT_STATUSES if counts[status]}
