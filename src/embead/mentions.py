"""Lineage claims written in tracker text rather than recorded as typed links.

People close the loop in prose far more often than in the tracker graph: "absorbed by X",
"superseded by X", "duplicate of X", "fixed by X".  Those records look unrelated to every
structural lane and often sit below semantic thresholds.  This module finds each record that names
another with one of those verbs, resolves the ID (full or short form), and reports whether a typed
link already backs the claim.  Only IDs, the claim kind, and field names leave this module; the
surrounding text never does.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .identifiers import IssueIdResolver

# Ordered by how directly a claim implies the pair should be linked or one side closed.
CLAIM_KINDS = (
    "duplicate-of",
    "superseded-by",
    "absorbed-by",
    "fixed-by",
    "supersedes",
    "absorbs",
    "continued-in",
    "mention",
)
_CLAIM_FIELDS = (
    "title",
    "description",
    "acceptance_criteria",
    "design",
    "notes",
    "close_reason",
)
_ID_TOKEN_RE = re.compile(r"(?:[a-z0-9][a-z0-9_]*-)*[a-z0-9]+(?:\.[0-9]+)*")
# The object of a claim sits in the same clause, within a few words of the verb: "absorbed by
# the board door abc12.4", "superseded by abc12 and def34".  A clause ends at ; ! ? a newline or a
# sentence period (a period inside an ID such as abc12.4 is not followed by whitespace).
_CLAUSE_END_RE = re.compile(r"[;!?\n]|\.(?=\s|$)")
_OBJECT_WINDOW_CHARACTERS = 96
_OBJECT_WINDOW_TOKENS = 5
_VERBS = (
    (
        "duplicate-of",
        r"duplicate\s+of|dup(?:e)?\s+of|duplicates|same\s+as"
        r"|same\s+(?:bug|defect|issue|problem|fix|work|scope)\s+as",
    ),
    ("superseded-by", r"superseded\s+by|replaced\s+by|obsoleted\s+by|moot(?:ed)?\s+by"),
    (
        "absorbed-by",
        r"(?:absorbed|folded|merged|subsumed|rolled)\s+(?:by|into)|(?:covered|carried)\s+by"
        r"|(?:moved|rehomed)\s+(?:to|into|under)",
    ),
    (
        "fixed-by",
        r"(?:fixed|resolved|addressed|closed|done|implemented|landed|shipped|delivered)"
        r"\s+(?:by|in|via)",
    ),
    ("supersedes", r"supersedes|replaces"),
    ("absorbs", r"absorbs|subsumes|folds\s+in|folded\s+in"),
    ("continued-in", r"continued\s+in|continues\s+in|tracked\s+in|split\s+(?:to|into)"),
)
_CLAIM_RES = tuple((kind, re.compile(rf"\b(?:{verbs})\b")) for kind, verbs in _VERBS)


_NEGATION_RE = re.compile(r"(?:\b(?:not|never)\b|n't)[^.!?;\n]{0,20}$")


def _negated(text: str, start: int) -> bool:
    # Tentative claims ("may be a duplicate of X") are still review evidence; denials are not.
    return bool(_NEGATION_RE.search(text[max(0, start - 28) : start]))


@dataclass(frozen=True, slots=True)
class TextClaim:
    issue_id: str
    related_issue_id: str
    kind: str
    source_fields: tuple[str, ...]
    typed_link: bool


def _field(issue: Any, name: str) -> str:
    value = getattr(issue, name, "")
    return value if isinstance(value, str) else ""


def _claim_objects(text: str, verb_end: int, resolver: IssueIdResolver) -> list[str]:
    window = text[verb_end : verb_end + _OBJECT_WINDOW_CHARACTERS]
    if clause_end := _CLAUSE_END_RE.search(window):
        window = window[: clause_end.start()]
    tokens = _ID_TOKEN_RE.findall(window)[:_OBJECT_WINDOW_TOKENS]
    return [resolved for token in tokens if (resolved := resolver.resolve(token)) is not None]


def _raw_claims(
    issue: Any, resolver: IssueIdResolver, *, include_mentions: bool
) -> list[tuple[str, str, str]]:
    """Return (related_id, kind, field) triples found in one record's text."""

    issue_id = str(getattr(issue, "id", ""))
    found: list[tuple[str, str, str]] = []
    for field in _CLAIM_FIELDS:
        text = _field(issue, field).casefold()
        if not text:
            continue
        claimed: set[str] = set()
        for kind, pattern in _CLAIM_RES:
            for match in pattern.finditer(text):
                if _negated(text, match.start()):
                    continue
                for related in _claim_objects(text, match.end(), resolver):
                    if related != issue_id:
                        found.append((related, kind, field))
                        claimed.add(related)
        if include_mentions:
            # Bare mentions accept only spellings that carry structure (a dash or a dot), so a
            # short hash that happens to be a word cannot become a claim without a verb.
            for token in _ID_TOKEN_RE.findall(text):
                if "-" not in token and "." not in token:
                    continue
                related = resolver.resolve(token)
                if related and related != issue_id and related not in claimed:
                    found.append((related, "mention", field))
    return found


def _typed_link(left: Any, right: Any) -> bool:
    def points_at(source: Any, target_id: str) -> bool:
        if getattr(source, "parent_id", None) == target_id:
            return True
        if target_id in (getattr(source, "dependencies", ()) or ()):
            return True
        return any(
            getattr(link, "target_id", None) == target_id
            for link in getattr(source, "dependency_links", ()) or ()
        )

    return points_at(left, str(right.id)) or points_at(right, str(left.id))


def extract_claims(
    claimants: Iterable[Any],
    all_issues: Sequence[Any],
    *,
    include_mentions: bool = False,
    resolver: IssueIdResolver | None = None,
) -> tuple[TextClaim, ...]:
    """Find every lineage claim made by ``claimants`` about records in ``all_issues``."""

    by_id = {str(issue.id): issue for issue in all_issues}
    resolver = resolver or IssueIdResolver(by_id)
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for issue in claimants:
        issue_id = str(issue.id)
        verb_pairs = set()
        raw = _raw_claims(issue, resolver, include_mentions=include_mentions)
        for related, kind, _field_name in raw:
            if kind != "mention":
                verb_pairs.add(related)
        for related, kind, field_name in raw:
            if kind == "mention" and related in verb_pairs:
                continue
            grouped.setdefault((issue_id, related, kind), set()).add(field_name)
    claims = [
        TextClaim(
            issue_id=issue_id,
            related_issue_id=related,
            kind=kind,
            source_fields=tuple(field for field in _CLAIM_FIELDS if field in fields),
            typed_link=_typed_link(by_id[issue_id], by_id[related]),
        )
        for (issue_id, related, kind), fields in grouped.items()
        if issue_id in by_id and related in by_id
    ]
    return tuple(sorted(claims, key=claim_sort_key))


def claim_sort_key(claim: TextClaim) -> tuple[int, str, str]:
    return (CLAIM_KINDS.index(claim.kind), claim.issue_id, claim.related_issue_id)


def claims_between(claims: Iterable[TextClaim], left_id: str, right_id: str) -> list[TextClaim]:
    return [
        claim for claim in claims if {claim.issue_id, claim.related_issue_id} == {left_id, right_id}
    ]


def kind_counts(claims: Iterable[TextClaim]) -> dict[str, int]:
    counts = Counter(claim.kind for claim in claims)
    return {kind: counts[kind] for kind in CLAIM_KINDS if counts[kind]}
