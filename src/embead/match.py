"""Candidate-text retrieval: nearest existing records for text that is not yet a record.

A candidate is embedded with the same canonical-text rules as a bead record (the body plays the
role of the description) and compared against the cached whole-record vectors.  Nothing is added to
the tracker, the index, or the vector cache; candidate text lives only in memory.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .models import IssueRecord, canonical_text
from .provider import Vector

DEFAULT_CANDIDATE_ID = "candidate-1"
CLOSED_STATUSES = frozenset({"closed", "done", "completed", "resolved"})
MATCH_NOTICE = (
    "Similarity is a retrieval lead, not a duplicate verdict. Semantic similarity is advisory "
    "evidence, not tracker truth: verify each neighbor against current project state before "
    "acting, and do not read a score as a duplicate decision, dependency, or status change."
)


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    title: str
    body: str

    @property
    def content_hash(self) -> str:
        return candidate_content_hash(self.title, self.body)

    def as_record(self) -> IssueRecord:
        return IssueRecord(id=self.candidate_id, title=self.title, description=self.body)


def candidate_content_hash(title: str, body: str) -> str:
    """SHA-256 over the exact title and body, so any payload change changes the hash."""

    encoded = json.dumps(
        {"body": body, "title": title}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_from_mapping(raw: Any, *, where: str, default_id: str | None) -> Candidate:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: candidate must be a JSON object")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{where}: candidate needs a non-empty string title")
    body = raw.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError(f"{where}: candidate body must be a string")
    candidate_id = raw.get("candidate_id", default_id)
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError(f"{where}: candidate needs a non-empty string candidate_id")
    return Candidate(candidate_id=candidate_id.strip(), title=title, body=body)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name}: not valid JSON (line {exc.lineno})") from exc


def load_candidates(
    *,
    title: str | None,
    body: str | None,
    body_file: Path | None,
    candidate_file: Path | None,
    candidates_file: Path | None,
    candidate_id: str | None,
) -> list[Candidate]:
    """Resolve exactly one input mode into candidates, rejecting duplicate or missing IDs."""

    modes = [
        name
        for name, present in (
            ("--title", title is not None),
            ("--candidate-file", candidate_file is not None),
            ("--candidates-file", candidates_file is not None),
        )
        if present
    ]
    if len(modes) != 1:
        raise ValueError("match needs exactly one of --title, --candidate-file, --candidates-file")
    if modes[0] != "--title" and (body is not None or body_file is not None):
        raise ValueError("--body and --body-file go with --title only")
    if body is not None and body_file is not None:
        raise ValueError("use --body or --body-file, not both")

    if title is not None:
        text = body if body is not None else ""
        if body_file is not None:
            text = body_file.read_text(encoding="utf-8")
        candidates = [
            _candidate_from_mapping(
                {
                    "title": title,
                    "body": text,
                    **({"candidate_id": candidate_id} if candidate_id else {}),
                },
                where="--title",
                default_id=DEFAULT_CANDIDATE_ID,
            )
        ]
    elif candidate_file is not None:
        raw = _read_json(candidate_file)
        if candidate_id is not None and isinstance(raw, dict):
            raw = {**raw, "candidate_id": candidate_id}
        candidates = [
            _candidate_from_mapping(raw, where="--candidate-file", default_id=DEFAULT_CANDIDATE_ID)
        ]
    else:
        assert candidates_file is not None
        candidates = []
        lines = candidates_file.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--candidates-file line {number}: not valid JSON") from exc
            candidates.append(
                _candidate_from_mapping(
                    raw, where=f"--candidates-file line {number}", default_id=None
                )
            )
        if not candidates:
            raise ValueError("--candidates-file holds no candidates")
    ids = [candidate.candidate_id for candidate in candidates]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise ValueError(f"duplicate candidate_id: {', '.join(duplicates)}")
    return candidates


def is_closed(issue: Any) -> bool:
    return str(issue.status).casefold() in CLOSED_STATUSES


def _parent_status(issue: IssueRecord, by_id: Mapping[str, IssueRecord]) -> str | None:
    if not issue.parent_id:
        return None
    parent = by_id.get(issue.parent_id)
    if parent is None:
        return "missing"
    return "closed" if is_closed(parent) else "live"


def _neighbor_row(
    issue: IssueRecord, similarity: float, rank: int, by_id: Mapping[str, IssueRecord]
) -> dict[str, Any]:
    closed = is_closed(issue)
    reason = issue.close_reason.strip()
    return {
        "issue_id": issue.id,
        "status": issue.status,
        "issue_type": issue.issue_type,
        "priority": issue.priority,
        "title": issue.title,
        "similarity": round(similarity, 6),
        "rank": rank,
        "is_closed": closed,
        "parent_id": issue.parent_id,
        "parent_status": _parent_status(issue, by_id),
        # Only what the tracker recorded: null when there is no close reason, never inferred.
        "resolution_evidence": (
            {"kind": "close_reason", "text": reason} if closed and reason else None
        ),
    }


def match_candidates(
    candidates: Sequence[Candidate],
    candidate_vectors: Sequence[Vector],
    issues: Sequence[IssueRecord],
    vectors: Mapping[str, Vector],
    *,
    limit: int,
    include_closed: bool,
    min_similarity: float | None,
) -> list[dict[str, Any]]:
    """Rank in-scope records per candidate: similarity descending, then issue ID."""

    if limit < 1:
        raise ValueError("--limit must be at least 1")
    by_id = {issue.id: issue for issue in issues}
    pool = sorted(
        (issue for issue in issues if include_closed or not is_closed(issue)),
        key=lambda issue: issue.id,
    )
    matrix = (
        np.asarray([vectors[issue.id] for issue in pool], dtype=np.float64)
        if pool
        else np.empty((0, 0))
    )
    if matrix.size:
        matrix = matrix / np.linalg.norm(matrix, axis=1)[:, np.newaxis]
    results = []
    for candidate, vector in zip(candidates, candidate_vectors, strict=True):
        ranked: list[tuple[float, IssueRecord]] = []
        if pool:
            query = np.asarray(vector, dtype=np.float64)
            if query.shape[0] != matrix.shape[1]:
                raise ValueError("candidate vector dimension does not match the record vectors")
            query = query / np.linalg.norm(query)
            scores = np.clip(matrix @ query, -1.0, 1.0)
            ranked = sorted(
                ((float(score), issue) for score, issue in zip(scores, pool, strict=True)),
                key=lambda item: (-round(item[0], 6), item[1].id),
            )
        above = [item for item in ranked if min_similarity is None or item[0] >= min_similarity]
        shown = above[:limit]
        if shown:
            status, reason = "matches", None
        elif not pool:
            status, reason = "no-match", "no-records-in-scope"
        else:
            status, reason = "no-match", "below-min-similarity"
        results.append(
            {
                "candidate_id": candidate.candidate_id,
                "content_hash": candidate.content_hash,
                "status": status,
                "no_match_reason": reason,
                "records_compared": len(pool),
                "matches_above_threshold": len(above),
                "notice": MATCH_NOTICE,
                "neighbors": [
                    _neighbor_row(issue, score, rank, by_id)
                    for rank, (score, issue) in enumerate(shown, start=1)
                ],
            }
        )
    return results


def candidate_texts(candidates: Iterable[Candidate]) -> list[str]:
    """Canonical embedding text, identical to the rules used for bead records."""

    return [canonical_text(candidate.as_record()) for candidate in candidates]
