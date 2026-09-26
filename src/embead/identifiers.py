"""Resolve the short spellings people use for tracker IDs in branches and prose.

Beads IDs look like ``proj-abc12`` or ``proj-abc12.4.1``.  Branch names and notes routinely drop
the prefix (``abc12.4``), swap dots for dashes (``abc12-4``), or both.  The resolver maps every
such spelling back to one ID and refuses any spelling that more than one ID could claim.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

# A prefix-less hash shorter than these is too likely to be an ordinary token ("c10", "v2").  A
# child suffix (".4") is structure of its own, so child forms may use a shorter hash.
_MIN_SHORT_TOP_LEVEL_LENGTH = 4
_MIN_SHORT_CHILD_HASH_LENGTH = 3
_BRANCH_SEPARATOR_RE = re.compile(r"[/_-]+")


def issue_id_forms(issue_id: str) -> frozenset[str]:
    """Return the casefolded spellings that may stand for ``issue_id``."""

    identifier = issue_id.strip().casefold()
    forms = {identifier, identifier.replace(".", "-")}
    head, dot, tail = identifier.partition(".")
    prefix, separator, hash_part = head.rpartition("-")
    minimum = _MIN_SHORT_CHILD_HASH_LENGTH if dot else _MIN_SHORT_TOP_LEVEL_LENGTH
    if separator and prefix and len(hash_part) >= minimum:
        short = hash_part + dot + tail
        forms.update({short, short.replace(".", "-")})
    return frozenset(form for form in forms if form)


def is_hierarchical_ancestor(ancestor_id: str, descendant_id: str) -> bool:
    return descendant_id.casefold().startswith(ancestor_id.casefold() + ".")


def prune_ancestors(issue_ids: Iterable[str]) -> tuple[str, ...]:
    """Drop IDs whose hierarchical descendant is also present (``a.1`` beats ``a``)."""

    unique = sorted(set(issue_ids))
    return tuple(
        identifier
        for identifier in unique
        if not any(is_hierarchical_ancestor(identifier, other) for other in unique)
    )


class IssueIdResolver:
    """Map exact IDs and unambiguous short spellings to tracker IDs."""

    def __init__(self, issue_ids: Iterable[str]) -> None:
        self._exact: dict[str, str] = {}
        claims: dict[str, set[str]] = defaultdict(set)
        for issue_id in sorted(set(issue_ids)):
            self._exact[issue_id.casefold()] = issue_id
            for form in issue_id_forms(issue_id):
                claims[form].add(issue_id)
        self._forms = {
            form: next(iter(owners))
            for form, owners in claims.items()
            if len(owners) == 1 and form not in self._exact
        }

    def resolve(self, token: str) -> str | None:
        key = token.strip().casefold()
        return self._exact.get(key) or self._forms.get(key)

    def branch_candidates(self, branch: str) -> tuple[str, ...]:
        """Return IDs spelled by any run of separator-delimited branch segments."""

        segments = [segment for segment in _BRANCH_SEPARATOR_RE.split(branch) if segment]
        found: set[str] = set()
        for start in range(len(segments)):
            for end in range(start + 1, len(segments) + 1):
                if resolved := self.resolve("-".join(segments[start:end])):
                    found.add(resolved)
        return prune_ancestors(found)
