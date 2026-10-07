"""Triage guards: flags that tell a reviewer a record needs a human before any fold or close.

Coordinators apply house rules before acting on a neighbor: never fold an owner-labelled record,
never close work someone has in progress, and look twice at anything touching production data,
published output, privacy, security, or money.  These flags surface those rules in the report so
nobody needs a second lookup.  They are advisory, computed from labels, status, assignee and the
title only, and they never block or change anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

DEFAULT_GUARD_LABELS = ("owner-run", "ask:owner")
DEFAULT_GUARD_KEYWORDS = (
    "prod",
    "production",
    "published",
    "privacy",
    "phi",
    "hipaa",
    "security",
    "money",
    "payment",
    "billing",
    "payroll",
)
_IN_PROGRESS = {"in_progress", "in-progress", "started"}


def guard_flags(
    issue: Any,
    *,
    labels: Sequence[str] = DEFAULT_GUARD_LABELS,
    keywords: Sequence[str] = DEFAULT_GUARD_KEYWORDS,
) -> list[str]:
    """Return sorted flags: ``label:owner-run``, ``in-progress-assigned``, ``keyword:prod``."""

    flags: set[str] = set()
    wanted = {label.casefold() for label in labels}
    for label in getattr(issue, "labels", ()) or ():
        if label.casefold() in wanted:
            flags.add(f"label:{label}")
    status = str(getattr(issue, "status", "")).casefold()
    if status in _IN_PROGRESS and str(getattr(issue, "assignee", "") or "").strip():
        flags.add("in-progress-assigned")
    words = set(re.findall(r"[a-z0-9]+", str(getattr(issue, "title", "")).casefold()))
    words.update(
        part
        for label in getattr(issue, "labels", ()) or ()
        for part in re.findall(r"[a-z0-9]+", label.casefold())
    )
    flags.update(f"keyword:{word}" for word in keywords if word.casefold() in words)
    return sorted(flags)


def score_baseline(scores: Iterable[float]) -> dict[str, Any]:
    """Percentiles of one seed's similarity to every record in scope.

    Cosine scores are model- and corpus-specific: on a tracker whose records share vocabulary, the
    median unrelated pair can sit at 0.7.  Reporting where this seed's whole population falls lets
    a reader see whether 0.78 is a standout or just the neighbourhood.
    """

    ordered = sorted(scores)
    if not ordered:
        return {"population": 0}

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 6)

    return {
        "population": len(ordered),
        "p50": percentile(0.50),
        "p90": percentile(0.90),
        "p99": percentile(0.99),
    }
