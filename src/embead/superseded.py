"""Which live records did a range of merged changes already do?

After a sprint the coordinator's question is not "which records look like these records" but
"which open records did today's merges make redundant".  This module reads merged changes (git
first-parent commits, or a JSONL list of pull requests), builds query text from each change's
title, body and changed paths, and ranks live records against every change.  Two cheap lexical
signals sit beside the cosine score: a change that names a record's ID, and code identifiers
(file stems, function names, routes) that the change and the record share.  Change text is encoded
in memory only and never reaches the vector cache.  Nothing here writes to git or the tracker.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from .identifiers import IssueIdResolver
from .models import IssueRecord, canonical_text
from .surfaces import GitRunner, _default_git_runner

CLOSED_STATUSES = frozenset({"closed", "done", "completed", "resolved"})
# Each shared identifier lifts the combined score by this much, up to the cap.  Identifiers are
# the strongest real signal in code trackers, but one shared file stem is common, so it is bounded.
IDENTIFIER_WEIGHT = 0.02
IDENTIFIER_CAP = 3
# An identifier counts only when at most this many records (or this share of them) carry it.
RARE_IDENTIFIER_FLOOR = 3
RARE_IDENTIFIER_FRACTION = 0.005
_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"
_PR_NUMBER_RE = re.compile(r"(?:\(#(\d+)\)\s*$|^Merge pull request #(\d+)\b)")
_ID_TOKEN_RE = re.compile(r"(?:[a-z0-9][a-z0-9_]*-)*[a-z0-9]+(?:\.[0-9]+)*")
_CAMEL_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]*)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_KEBAB_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){2,}\b")
_ROUTE_RE = re.compile(r"(?<![\w.])/[a-z][\w-]*(?:/[\w\[\]().-]+)+")
_GENERIC_STEMS = frozenset({"index", "page", "layout", "route", "test", "spec", "types", "utils"})
_NOISE_IDENTIFIERS = frozenset({"co-authored-by", "signed-off-by"})
# A squash merge lists each commit as "* <subject>" followed by a blank line and its body.
_SQUASH_SECTION_RE = re.compile(r"(?:\A|\n\n)\* (?=\S[^\n]*\n\n)")


@dataclass(frozen=True, slots=True)
class Change:
    """One merged change: a squash or merge commit, or a pull request from a file."""

    change_id: str
    title: str
    body: str = ""
    paths: tuple[str, ...] = ()
    pr_number: int | None = None
    commit: str | None = None
    merged_at: str | None = None
    branch: str | None = None

    def query_text(self) -> str:
        paths = "\n".join(self.paths)
        description = f"{self.body}\n\nChanged files:\n{paths}" if paths else self.body
        return canonical_text(
            IssueRecord(id=self.change_id, title=self.title, description=description)
        )

    def facets(self) -> list[tuple[str | None, str]]:
        """The whole change, then each squashed commit on its own when there are several."""

        whole: list[tuple[str | None, str]] = [(None, self.query_text())]
        sections = [part.strip() for part in _SQUASH_SECTION_RE.split(self.body) if part.strip()]
        if len(sections) < 2:
            return whole
        for section in sections:
            subject, _, body = section.partition("\n")
            record = IssueRecord(id=self.change_id, title=subject.strip(), description=body)
            whole.append((subject.strip(), canonical_text(record)))
        return whole

    def evidence(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "pr_number": self.pr_number,
            "commit": self.commit,
            "merged_at": self.merged_at,
            "title": self.title,
        }


@dataclass(slots=True)
class _Hit:
    issue: IssueRecord
    best: dict[str, Any]
    changes: set[str] = field(default_factory=set)


def git_since_arguments(since: str, ref: str) -> list[str]:
    """Translate ``--since`` into a git revision range: a date, a timestamp, or a commit."""

    value = since.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        date.fromisoformat(value)
        # git reads a bare date as "that date at the current time of day"; mean midnight.
        return [f"--since={value}T00:00:00", ref]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ][\d:]+(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", value):
        return [f"--since={value}", ref]
    if not re.fullmatch(r"[0-9A-Za-z_./^~-]+", value) or value.startswith("-"):
        raise ValueError(f"--since must be a date, a timestamp, or a commit: {since!r}")
    return [f"{value}..{ref}"]


def changes_from_git(
    repository: Path, since: str, *, ref: str = "HEAD", runner: GitRunner | None = None
) -> list[Change]:
    """Read first-parent commits on ``ref`` since a date or commit, with their changed paths."""

    run = runner or _default_git_runner
    fmt = _RECORD_SEPARATOR + _FIELD_SEPARATOR.join(("%H", "%cI", "%s", "%b")) + _FIELD_SEPARATOR
    result = run(
        repository,
        [
            "log",
            "--first-parent",
            "-m",
            "--name-only",
            f"--format={fmt}",
            *git_since_arguments(since, ref),
            "--",
        ],
    )
    if result.returncode != 0:
        raise RuntimeError(f"git log failed in {repository}: {result.stderr.strip()}")
    changes = []
    for record in result.stdout.split(_RECORD_SEPARATOR):
        if not record.strip():
            continue
        commit, merged_at, subject, body, files = (record.split(_FIELD_SEPARATOR) + [""] * 5)[:5]
        number_match = _PR_NUMBER_RE.search(subject)
        number = (
            next((int(group) for group in number_match.groups() if group), None)
            if number_match
            else None
        )
        branch = None
        title = subject
        if subject.startswith("Merge pull request #"):
            # A merge commit's subject names the branch; the PR title is the body's first line.
            branch = subject.split(" from ", 1)[1].strip() if " from " in subject else None
            first, _, rest = body.strip().partition("\n")
            title, body = (first or subject), rest
        changes.append(
            Change(
                change_id=f"#{number}" if number else commit[:12],
                title=title.strip(),
                body=body.strip(),
                paths=tuple(
                    dict.fromkeys(line.strip() for line in files.splitlines() if line.strip())
                ),
                pr_number=number,
                commit=commit,
                merged_at=merged_at or None,
                branch=branch,
            )
        )
    return changes


def changes_from_file(path: Path) -> list[Change]:
    """Read pull requests as JSONL: title, body, files (paths or {"path": ...}), number."""

    changes = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(raw, Mapping) or not isinstance(raw.get("title"), str):
            raise ValueError(f"{path}:{line_number}: each change needs a string title")
        number = raw.get("number")
        files = raw.get("files") or []
        paths = tuple(
            str(item.get("path")) if isinstance(item, Mapping) else str(item) for item in files
        )
        commit = raw.get("commit") or raw.get("mergeCommit")
        if isinstance(commit, Mapping):
            commit = commit.get("oid")
        changes.append(
            Change(
                change_id=f"#{number}"
                if isinstance(number, int)
                else str(raw.get("id") or line_number),
                title=raw["title"],
                body=str(raw.get("body") or ""),
                paths=paths,
                pr_number=number if isinstance(number, int) else None,
                commit=str(commit) if commit else None,
                merged_at=raw.get("merged_at") or raw.get("mergedAt"),
                branch=raw.get("branch") or raw.get("headRefName"),
            )
        )
    return changes


def code_identifiers(text: str, paths: Iterable[str] = ()) -> set[str]:
    """Identifiers a change and a record can share: function names, file stems, routes."""

    found: set[str] = set()
    for pattern in (_CAMEL_RE, _SNAKE_RE):
        found.update(match.casefold() for match in pattern.findall(text))
    lowered = text.casefold()
    found.update(_KEBAB_RE.findall(lowered))
    found.update(route.rstrip("/.") for route in _ROUTE_RE.findall(lowered))
    for path in paths:
        stem = PurePosixPath(path).name.split(".", 1)[0].casefold()
        if len(stem) >= 4 and stem not in _GENERIC_STEMS:
            found.add(stem)
    # A record often names a file with its extension; the stem is the comparable unit.
    return {
        re.sub(r"\.(?:tsx?|jsx?|py|sql|go|rs|rb|md)$", "", item)
        for item in found
        if len(item) >= 4 and item not in _NOISE_IDENTIFIERS
    }


def named_records(change: Change, resolver: IssueIdResolver) -> set[str]:
    """Record IDs a change names in its title, body, or branch (structured spellings only)."""

    text = f"{change.title}\n{change.body}".casefold()
    named = {
        resolved
        for token in _ID_TOKEN_RE.findall(text)
        if ("-" in token or "." in token) and (resolved := resolver.resolve(token))
    }
    if change.branch:
        named.update(resolver.branch_candidates(change.branch))
    return named


def _record_identifiers(issue: IssueRecord) -> set[str]:
    return code_identifiers(
        "\n".join(
            (issue.title, issue.description, issue.acceptance_criteria, issue.design, issue.notes)
        )
    )


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def earliest_merge(changes: Iterable[Change]) -> datetime | None:
    instants = [instant for change in changes if (instant := _instant(change.merged_at))]
    return min(instants) if instants else None


def in_scope(issue: IssueRecord, *, include_epics: bool, closed_since: datetime | None) -> bool:
    if not include_epics and issue.issue_type.casefold() == "epic":
        return False
    if issue.status.casefold() not in CLOSED_STATUSES:
        return True
    # Replaying a past sweep: a record closed after the first merge was live when it merged.
    updated = _instant(issue.updated_at)
    return bool(closed_since and updated and updated >= closed_since)


def _rare_identifiers(record_identifiers: Mapping[str, set[str]]) -> set[str]:
    """Identifiers few records share; a name in hundreds of records says nothing about one."""

    counts = Counter(name for names in record_identifiers.values() for name in names)
    ceiling = max(RARE_IDENTIFIER_FLOOR, int(RARE_IDENTIFIER_FRACTION * len(record_identifiers)))
    return {name for name, count in counts.items() if count <= ceiling}


def superseded_candidates(
    changes: Sequence[Change],
    facet_vectors: Sequence[Sequence[Sequence[float]]],
    issues: Sequence[IssueRecord],
    vectors: Mapping[str, Sequence[float]],
    resolver: IssueIdResolver,
    *,
    per_change: int,
    identifier_weight: float = IDENTIFIER_WEIGHT,
) -> list[dict[str, Any]]:
    """Rank records against every change; keep each record's strongest evidence.

    ``facet_vectors[i]`` holds one vector per entry of ``changes[i].facets()``.  A record's score
    for a change is its best facet: a squash merge that bundles a fix with unrelated work still
    points straight at the record the fix closes.
    """

    pool = sorted(issues, key=lambda issue: issue.id)
    if not pool or not changes:
        return []
    position = {issue.id: index for index, issue in enumerate(pool)}
    named_by = [named_records(change, resolver) & position.keys() for change in changes]
    # A record any change names is reported as named; leaving it in the similarity ranking would
    # spend every change's first place on the record its own title already names.
    ranked_pool = [issue for issue in pool if not any(issue.id in named for named in named_by)]
    hits: dict[str, _Hit] = {}

    def record(issue: IssueRecord, change: Change, evidence: dict[str, Any]) -> None:
        hit = hits.get(issue.id)
        if hit is None:
            hit = hits[issue.id] = _Hit(issue, evidence)
        elif _evidence_key(evidence) < _evidence_key(hit.best):
            hit.best = evidence
        hit.changes.add(change.change_id)

    for change, named in zip(changes, named_by, strict=True):
        for issue_id in sorted(named):
            record(
                pool[position[issue_id]],
                change,
                {
                    **change.evidence(),
                    "basis": "named",
                    "facet": None,
                    "similarity": None,
                    "combined_score": None,
                    "rank_in_change": None,
                    "lead": None,
                    "shared_identifiers": [],
                },
            )
    if not ranked_pool:
        return _sorted_rows(hits)
    matrix = np.asarray([vectors[issue.id] for issue in ranked_pool], dtype=np.float64)
    matrix = matrix / np.linalg.norm(matrix, axis=1)[:, np.newaxis]
    record_identifiers = {issue.id: _record_identifiers(issue) for issue in ranked_pool}
    rare = _rare_identifiers(record_identifiers)
    for change, change_facet_vectors in zip(changes, facet_vectors, strict=True):
        identifiers = code_identifiers(f"{change.title}\n{change.body}", change.paths) & rare
        lift = np.zeros(len(ranked_pool))
        shared_by_index: dict[int, list[str]] = {}
        for index, issue in enumerate(ranked_pool):
            if shared := sorted(identifiers & record_identifiers[issue.id]):
                shared_by_index[index] = shared
                lift[index] = identifier_weight * min(len(shared), IDENTIFIER_CAP)
        # Each facet is its own ranking: a squashed fix commit should not have to outscore the
        # whole pull request's broader text to reach the record it closes.
        for (facet, _text), vector in zip(change.facets(), change_facet_vectors, strict=True):
            query = np.asarray(vector, dtype=np.float64)
            scores = np.clip(matrix @ (query / np.linalg.norm(query)), -1.0, 1.0)
            combined = scores + lift
            order = sorted(
                range(len(ranked_pool)),
                key=lambda index: (-round(combined[index], 6), ranked_pool[index].id),
            )
            for rank, index in enumerate(order[:per_change], start=1):
                # How far the record leads the next one: standing out for one change is a
                # stronger lead than being one of many records that all look alike.
                lead = combined[index] - combined[order[rank]] if rank < len(order) else 0.0
                record(
                    ranked_pool[index],
                    change,
                    {
                        **change.evidence(),
                        "basis": "similar",
                        "facet": facet,
                        "similarity": round(float(scores[index]), 6),
                        "combined_score": round(float(combined[index]), 6),
                        "rank_in_change": rank,
                        "lead": round(float(lead), 6),
                        "shared_identifiers": shared_by_index.get(index, [])[:8],
                    },
                )
    return _sorted_rows(hits)


def _sorted_rows(hits: Mapping[str, _Hit]) -> list[dict[str, Any]]:
    rows = [
        {"issue": hit.issue, "evidence": hit.best, "change_count": len(hit.changes)}
        for hit in hits.values()
    ]
    # A record named by any change is reported as named, with that change as its evidence.
    return sorted(rows, key=lambda row: (*_evidence_key(row["evidence"]), row["issue"].id))


def _evidence_key(evidence: Mapping[str, Any]) -> tuple[int, int, float, float, str]:
    if evidence["basis"] == "named":
        return (0, 0, 0.0, 0.0, str(evidence["change_id"]))
    return (
        1,
        int(evidence["rank_in_change"]),
        -float(evidence["lead"]),
        -float(evidence["combined_score"]),
        str(evidence["change_id"]),
    )
