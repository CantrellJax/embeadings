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
# Parents that may be finished without anyone noticing; in_progress means someone holds it.
DONE_PARENT_STATUSES = frozenset({"open", "blocked", "deferred"})
DEFERRED_STATUS = "deferred"


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
class DoneParent:
    issue_id: str
    issue_type: str
    status: str
    priority: int | None
    title: str
    child_count: int


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


def find_done_parents(
    issues: Sequence[Any], *, include_epics: bool = False, reportable: Iterable[Any] | None = None
) -> tuple[DoneParent, ...]:
    """Return open, blocked, or deferred parents whose children are all closed.

    Children are the records in ``issues`` that name the parent; a parent with none is not
    reported.  ``reportable`` narrows which parents may be reported (for example, to drop
    ephemeral ones) without hiding a live child.  Epics are left out unless ``include_epics``: a
    finished epic is often kept open on purpose until a later phase is filed.
    """

    population = list(issues if reportable is None else reportable)
    children: dict[str, list[Any]] = {}
    for issue in issues:
        if parent_id := getattr(issue, "parent_id", None):
            children.setdefault(parent_id, []).append(issue)
    rows = [
        DoneParent(
            issue_id=str(issue.id),
            issue_type=issue.issue_type,
            status=issue.status,
            priority=issue.priority,
            title=issue.title,
            child_count=len(children[str(issue.id)]),
        )
        for issue in population
        if str(issue.status).casefold() in DONE_PARENT_STATUSES
        and (include_epics or str(issue.issue_type).casefold() != "epic")
        and children.get(str(issue.id))
        and not any(is_live(child) for child in children[str(issue.id)])
    ]
    return tuple(sorted(rows, key=lambda row: row.issue_id))


def find_deferred_parent_children(
    issues: Sequence[Any], *, reportable: Iterable[Any] | None = None
) -> tuple[DanglingParent, ...]:
    """Return open, in-progress, or blocked children of a deferred parent.

    A deferred parent is live, so these are not dangling; but active work under parked work is
    worth a look.  Deferred children of a deferred parent are consistent and are left out.
    """

    by_id = {str(issue.id): issue for issue in issues}
    rows = []
    for issue in issues if reportable is None else reportable:
        parent = by_id.get(getattr(issue, "parent_id", None) or "")
        status = str(issue.status).casefold()
        if parent is None or not is_live(issue) or status == DEFERRED_STATUS:
            continue
        if str(parent.status).casefold() != DEFERRED_STATUS:
            continue
        rows.append(
            DanglingParent(
                issue_id=str(issue.id),
                issue_type=issue.issue_type,
                status=issue.status,
                priority=issue.priority,
                title=issue.title,
                parent_id=str(parent.id),
                parent_status=DEFERRED_STATUS,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.parent_id, row.issue_id)))
