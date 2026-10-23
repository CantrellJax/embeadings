"""One compact, self-contained review packet per seed, for a human or LLM triage reviewer.

A backlog triage asks the same questions of every record: what is it, who touched it last, what
else looks like it (and which of the pair is the copy), did a merge already do it, does any text
claim it moved, is its parent finished or parked, and is someone already working on something that
looks like it.  ``neighbors``, ``superseded``, ``mentions`` and ``orphans`` each answer one; a
packet answers all of them for one seed from a single tracker and model load.  Nothing here writes
to the tracker.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .analysis import SimilarityIndex
from .guards import guard_flags, score_baseline
from .mentions import TextClaim, duplicate_direction
from .orphans import (
    find_dangling_parents,
    find_deferred_parent_children,
    find_done_parents,
)
from .ranking import NO_STRUCTURAL_LINK, structural_context

CLOSED_STATUSES = frozenset({"closed", "done", "completed", "resolved"})
PARENT_CHILD = "parent-child"
INFLIGHT_DEPTH = 3
# Keys every packet keeps even when empty, so a reader can tell "none" from "not computed".
_REQUIRED_KEYS = ("schema_version", "report_type", "id", "title", "status", "neighbors")
_REQUIRED_KEYS += ("inflight_neighbors", "claims", "flags")


@dataclass(frozen=True, slots=True)
class PacketOptions:
    limit: int
    text_limit: int
    inflight_statuses: frozenset[str]
    guard_labels: tuple[str, ...]
    guard_keywords: tuple[str, ...]


def is_closed(issue: Any) -> bool:
    return str(issue.status).casefold() in CLOSED_STATUSES


def clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut to ``limit`` characters, marking a cut with an ellipsis."""

    flat = " ".join(str(text or "").split())
    if limit <= 0 or len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def relation_counts(issues: Iterable[Any]) -> dict[str, dict[str, int]]:
    """Per record: typed links out and in (parent-child excluded), and children total and live."""

    counts: dict[str, Counter[str]] = {}
    for issue in issues:
        mine = counts.setdefault(issue.id, Counter())
        for link in issue.dependency_links:
            if link.relationship_type.casefold() == PARENT_CHILD:
                continue
            mine["dependencies"] += 1
            counts.setdefault(link.target_id, Counter())["dependents"] += 1
        if issue.parent_id:
            parent = counts.setdefault(issue.parent_id, Counter())
            parent["children"] += 1
            parent["live_children"] += not is_closed(issue)
    return {
        identifier: {
            name: int(counter[name])
            for name in ("dependencies", "dependents", "children", "live_children")
        }
        for identifier, counter in counts.items()
    }


def structure_flags(issues: Sequence[Any], reportable: Sequence[Any]) -> dict[str, list[str]]:
    """``parent-closed``/``parent-missing``, ``parent-deferred``, and ``done-parent`` by record."""

    flags: dict[str, list[str]] = {}
    for row in find_dangling_parents(issues, reportable=reportable):
        flags.setdefault(row.issue_id, []).append(f"parent-{row.parent_status}")
    for row in find_deferred_parent_children(issues, reportable=reportable):
        flags.setdefault(row.issue_id, []).append("parent-deferred")
    for row in find_done_parents(issues, include_epics=True, reportable=reportable):
        flags.setdefault(row.issue_id, []).append("done-parent")
    return flags


def _claim_rows(
    seed_id: str, claims: Sequence[TextClaim], by_id: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    for claim in claims:
        other = claim.related_issue_id if claim.issue_id == seed_id else claim.issue_id
        rows.append(
            {
                "issue_id": claim.issue_id,
                "kind": claim.kind,
                "related_issue_id": claim.related_issue_id,
                "other_status": by_id[other].status if other in by_id else None,
                "typed_link": claim.typed_link,
            }
        )
    return rows


def _neighbor_row(
    seed: Any,
    related: Any,
    *,
    similarity: float,
    rank: int,
    reverse_rank: int,
    claims: Sequence[TextClaim],
    options: PacketOptions,
) -> dict[str, Any]:
    context = structural_context(seed, related)
    closed = is_closed(related)
    pair_claims = [
        f"{claim.issue_id} {claim.kind} {claim.related_issue_id}"
        for claim in claims
        if {claim.issue_id, claim.related_issue_id} == {seed.id, related.id}
    ]
    return {
        "id": related.id,
        "title": related.title,
        "status": related.status,
        "priority": related.priority,
        "similarity": round(similarity, 4),
        "rank": rank,
        "reverse_rank": reverse_rank,
        "structural_context": None if context == NO_STRUCTURAL_LINK else context,
        "assignee": related.assignee or None,
        "last_human_touch": None,
        "guards": guard_flags(
            related, labels=options.guard_labels, keywords=options.guard_keywords
        ),
        "close_reason": clip(related.close_reason, options.text_limit) if closed else None,
        "duplicate_direction": duplicate_direction(seed, related, claims),
        "text_claims": pair_claims,
    }


def build_packets(
    seeds: Sequence[Any],
    issues: Sequence[Any],
    pool_ids: Sequence[str],
    index: SimilarityIndex,
    claims: Sequence[TextClaim],
    *,
    options: PacketOptions,
    leads: Mapping[str, Mapping[str, list[dict[str, Any]]]] | None = None,
    reportable: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build one uncompacted packet per seed; ``last_human_touch`` values are filled later."""

    by_id = {issue.id: issue for issue in issues}
    pool_ids = sorted(set(pool_ids))
    pool = index.positions(pool_ids)
    counts = relation_counts(issues)
    flags = structure_flags(issues, reportable if reportable is not None else issues)
    claims_by_record: dict[str, list[TextClaim]] = {}
    for claim in claims:
        claims_by_record.setdefault(claim.issue_id, []).append(claim)
        if claim.related_issue_id != claim.issue_id:
            claims_by_record.setdefault(claim.related_issue_id, []).append(claim)
    seed_ids = [seed.id for seed in seeds]
    depth = max(options.limit, INFLIGHT_DEPTH)
    seed_top = index.top_ranks(seed_ids, pool_ids, depth)
    inflight_ids = [
        identifier
        for identifier in pool_ids
        if str(by_id[identifier].status).casefold() in options.inflight_statuses
    ]
    inflight_set = frozenset(inflight_ids)
    inflight_top = index.top_ranks(inflight_ids, pool_ids, INFLIGHT_DEPTH)
    ranked_by_seed: dict[str, list[tuple[int, str]]] = {}
    for (query, candidate), rank in seed_top.items():
        ranked_by_seed.setdefault(query, []).append((rank, candidate))
    inflight_ranking_seed: dict[str, dict[str, int]] = {}
    for (query, candidate), rank in inflight_top.items():
        inflight_ranking_seed.setdefault(candidate, {})[query] = rank

    packets = []
    for seed in seeds:
        seed_claims = claims_by_record.get(seed.id, [])
        baseline = score_baseline(index.scores_against(seed.id, pool).tolist())
        floor = baseline.get("p90")

        def above(score: float, floor: float | None = floor) -> bool:
            return floor is None or score > floor

        neighbors = []
        for rank, candidate in sorted(ranked_by_seed.get(seed.id, [])):
            score = index.score(seed.id, candidate)
            if len(neighbors) >= options.limit or not above(score):
                continue
            neighbors.append(
                _neighbor_row(
                    seed,
                    by_id[candidate],
                    similarity=score,
                    rank=rank,
                    reverse_rank=index.rank_of(candidate, seed.id, pool),
                    claims=seed_claims,
                    options=options,
                )
            )
        seed_ranks = {candidate: rank for rank, candidate in ranked_by_seed.get(seed.id, [])}
        inflight = []
        candidates = {
            candidate
            for candidate, rank in seed_ranks.items()
            if rank <= INFLIGHT_DEPTH and candidate in inflight_set
        } | set(inflight_ranking_seed.get(seed.id, {}))
        for candidate in sorted(candidates - {seed.id}):
            score = index.score(seed.id, candidate)
            if not above(score):
                continue
            related = by_id[candidate]
            inflight.append(
                {
                    "id": candidate,
                    "title": related.title,
                    "status": related.status,
                    "holder": related.assignee or None,
                    "similarity": round(score, 4),
                    "rank": seed_ranks.get(candidate) or index.rank_of(seed.id, candidate, pool),
                    "reverse_rank": inflight_ranking_seed.get(seed.id, {}).get(candidate)
                    or index.rank_of(candidate, seed.id, pool),
                    "last_human_touch": None,
                }
            )
        inflight.sort(key=lambda row: (-row["similarity"], row["id"]))
        parent = by_id.get(seed.parent_id) if seed.parent_id else None
        record_counts = counts.get(seed.id, {})
        packet: dict[str, Any] = {
            "schema_version": 1,
            "report_type": "packet",
            "id": seed.id,
            "title": seed.title,
            "priority": seed.priority,
            "status": seed.status,
            "type": seed.issue_type or None,
            "created": seed.created_at or None,
            "updated": seed.updated_at or None,
            "last_human_touch": None,
            "last_human_touch_source": None,
            "assignee": seed.assignee or None,
            "labels": list(seed.labels),
            "parent": seed.parent_id,
            "parent_status": (parent.status if parent else "missing") if seed.parent_id else None,
            "counts": {name: value for name, value in record_counts.items() if value},
            "comment_count": seed.comment_count or None,
            "description": clip(seed.description, options.text_limit),
            "notes": clip(seed.notes, options.text_limit),
            "acceptance": clip(seed.acceptance_criteria, options.text_limit),
            "close_reason": clip(seed.close_reason, options.text_limit)
            if is_closed(seed)
            else None,
            "score_baseline": {
                key: round(baseline[key], 4) for key in ("p50", "p90") if key in baseline
            },
            "neighbors": neighbors,
            "inflight_neighbors": inflight,
            "claims": _claim_rows(seed.id, seed_claims, by_id),
            "flags": sorted(flags.get(seed.id, [])),
        }
        if leads is not None:
            packet["superseded"] = {
                "named": [
                    _change_row(change) for change in leads.get(seed.id, {}).get("named", [])
                ],
                "similar": [
                    _change_row(change) for change in leads.get(seed.id, {}).get("similar", [])
                ],
            }
        packets.append(packet)
    return packets


def _change_row(change: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("change_id", "title", "merged_at", "similarity", "rank_in_change", "facet")
    keys += ("shared_identifiers",)
    row = {key: change[key] for key in keys if key in change}
    if row.get("similarity") is not None:
        row["similarity"] = round(row["similarity"], 4)
    return row


def packet_record_ids(packets: Iterable[Mapping[str, Any]]) -> set[str]:
    """Every record a packet reports a ``last_human_touch`` for."""

    ids = set()
    for packet in packets:
        ids.add(packet["id"])
        ids.update(row["id"] for row in packet["neighbors"])
        ids.update(row["id"] for row in packet["inflight_neighbors"])
    return ids


def fill_touches(
    packets: Iterable[dict[str, Any]],
    touch: Callable[[str], tuple[str | None, str]],
) -> None:
    for packet in packets:
        packet["last_human_touch"], packet["last_human_touch_source"] = touch(packet["id"])
        for row in (*packet["neighbors"], *packet["inflight_neighbors"]):
            row["last_human_touch"] = touch(row["id"])[0]


def _empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, (list, dict)) and not value)


def compact(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields at every level so the packet stays short for a reader."""

    def strip(value: Any, *, keep: Sequence[str] = ()) -> Any:
        if isinstance(value, Mapping):
            return {
                key: strip(item)
                for key, item in value.items()
                if key in keep or not _empty(strip(item))
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        if isinstance(value, (np.floating, np.integer)):
            return value.item()
        return value

    return strip(packet, keep=_REQUIRED_KEYS)
