"""Pure semantic ranking and deterministic batching algorithms."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

Vector = Sequence[float]


@dataclass(frozen=True, slots=True)
class Neighbor:
    issue_id: str
    similarity: float
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewBatch:
    """One bounded agent handoff containing one or more explicit review units."""

    issues: tuple[Any, ...]
    kind: str
    review_units: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class BatchDiagnostics:
    component_count: int
    singleton_component_count: int
    agent_envelope_count: int
    fragmented_component_count: int
    cross_batch_candidate_edges: int
    max_batch_size: int
    configured_max_batch_size: int


@dataclass(frozen=True, slots=True)
class CandidatePackaging:
    batches: tuple[ReviewBatch, ...]
    diagnostics: BatchDiagnostics


class SimilarityIndex:
    """Validate vectors once and reuse their pairwise cosine similarities.

    Providers and the on-disk cache already return normalized vectors, but the
    index deliberately validates and normalizes its input once at the analysis
    boundary.  The dense score matrix keeps a 1,000-record workspace small
    (about 8 MB with float64 values) while avoiding repeated Python dot products.
    """

    def __init__(self, vectors: Mapping[str, Vector]) -> None:
        self._ids = tuple(sorted(vectors))
        self._positions = {identifier: index for index, identifier in enumerate(self._ids)}
        if not self._ids:
            self._scores = np.empty((0, 0), dtype=np.float64)
            return

        rows: list[list[float]] = []
        dimension: int | None = None
        for identifier in self._ids:
            values = [float(value) for value in vectors[identifier]]
            if not values or any(not math.isfinite(value) for value in values):
                raise ValueError(f"vector for {identifier!r} must be non-empty and finite")
            if dimension is None:
                dimension = len(values)
            elif len(values) != dimension:
                raise ValueError("vectors must have the same dimension")
            rows.append(values)

        matrix = np.asarray(rows, dtype=np.float64)
        norms = np.linalg.norm(matrix, axis=1)
        if np.any(norms == 0):
            identifier = self._ids[int(np.flatnonzero(norms == 0)[0])]
            raise ValueError(f"cannot normalize zero vector for {identifier!r}")
        matrix /= norms[:, np.newaxis]
        self._scores = matrix @ matrix.T
        np.clip(self._scores, -1.0, 1.0, out=self._scores)

    @property
    def ids(self) -> tuple[str, ...]:
        return self._ids

    def score(self, left_id: str, right_id: str) -> float:
        """Return a cached cosine score for two indexed IDs."""

        return float(self._scores[self._position(left_id), self._position(right_id)])

    def ranked(self, query_id: str, candidate_ids: Sequence[str]) -> list[tuple[str, float]]:
        """Rank candidate IDs by descending score, then ascending ID."""

        self._position(query_id)
        unique_ids = set(candidate_ids)
        ranked = [(identifier, self.score(query_id, identifier)) for identifier in unique_ids]
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return ranked

    def positions(self, identifiers: Sequence[str]) -> np.ndarray:
        """Return index positions for a reusable candidate pool (IDs sort like positions)."""

        return np.asarray([self._position(item) for item in identifiers], dtype=np.intp)

    def rank_of(self, query_id: str, target_id: str, pool: np.ndarray) -> int:
        """Return the 1-based rank of ``target_id`` among ``pool`` for ``query_id``.

        Ties break by ascending ID, matching :meth:`ranked`; the query itself never ranks.
        """

        query = self._position(query_id)
        target = self._position(target_id)
        row = self._scores[query, pool]
        target_score = self._scores[query, target]
        eligible = (pool != query) & (pool != target)
        ahead = eligible & ((row > target_score) | ((row == target_score) & (pool < target)))
        return int(np.count_nonzero(ahead)) + 1

    def top_ranks(
        self,
        query_ids: Sequence[str],
        candidate_ids: Sequence[str],
        limit: int,
    ) -> dict[tuple[str, str], int]:
        """Return exact deterministic top ranks without per-pair Python score calls."""

        if limit < 1:
            return {}
        candidates = list(dict.fromkeys(candidate_ids))
        candidate_positions = np.asarray(
            [self._position(identifier) for identifier in candidates],
            dtype=np.intp,
        )
        ranks: dict[tuple[str, str], int] = {}
        query_positions = np.asarray(
            [self._position(identifier) for identifier in query_ids],
            dtype=np.intp,
        )
        # Chunking bounds temporary memory while selecting top-k rows in bulk.
        for block_start in range(0, len(query_ids), 128):
            block_end = min(block_start + 128, len(query_ids))
            block_positions = query_positions[block_start:block_end]
            block_scores = self._scores[np.ix_(block_positions, candidate_positions)]
            for row, query_position in enumerate(block_positions):
                block_scores[row, candidate_positions == query_position] = -np.inf
            count = min(limit, len(candidates))
            if not count:
                continue
            if count < len(candidates):
                np.negative(block_scores, out=block_scores)
                selected_by_row = np.argpartition(block_scores, count - 1, axis=1)[:, :count]
                np.negative(block_scores, out=block_scores)
            else:
                selected_by_row = np.tile(np.arange(len(candidates)), (len(block_positions), 1))
            for row, selected in enumerate(selected_by_row):
                query_id = query_ids[block_start + row]
                scores = block_scores[row]
                selected = selected[np.isfinite(scores[selected])]
                if not len(selected):
                    continue
                cutoff = float(np.min(scores[selected]))
                higher = np.flatnonzero(scores > cutoff)
                tied = np.flatnonzero(scores == cutoff)
                needed = count - len(higher)
                selected_indexes = np.concatenate(
                    (
                        higher,
                        np.asarray(
                            sorted(tied, key=lambda index: candidates[index])[:needed],
                            dtype=np.intp,
                        ),
                    )
                )
                ordered = sorted(
                    selected_indexes,
                    key=lambda index: (-float(scores[index]), candidates[index]),
                )
                ranks.update(
                    {
                        (query_id, candidates[index]): rank
                        for rank, index in enumerate(ordered, start=1)
                    }
                )
        return ranks

    def pairs_at_or_above(
        self,
        left_ids: Sequence[str],
        right_ids: Sequence[str],
        threshold: float,
        *,
        upper_triangle: bool = False,
        eligible_ids: frozenset[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Return deterministic threshold-qualified pairs using vectorized score rows."""

        left = list(left_ids)
        right = list(right_ids)
        right_positions = np.asarray(
            [self._position(identifier) for identifier in right],
            dtype=np.intp,
        )
        right_eligible = (
            np.asarray([identifier in eligible_ids for identifier in right], dtype=np.bool_)
            if eligible_ids is not None
            else None
        )
        pairs: list[tuple[str, str]] = []
        for left_index, left_id in enumerate(left):
            start = left_index + 1 if upper_triangle else 0
            if start >= len(right):
                continue
            scores = self._scores[self._position(left_id), right_positions[start:]]
            admitted = scores >= threshold
            if eligible_ids is not None and left_id not in eligible_ids:
                admitted &= right_eligible[start:]
            for offset in np.flatnonzero(admitted):
                right_id = right[start + int(offset)]
                if right_id != left_id:
                    pairs.append((left_id, right_id))
        return pairs

    def sum_scores(
        self,
        query_ids: Sequence[str],
        candidate_ids: Sequence[str],
    ) -> dict[str, float]:
        """Return vectorized score sums for density updates."""

        queries = list(query_ids)
        candidates = list(candidate_ids)
        if not candidates:
            return {identifier: 0.0 for identifier in queries}
        query_positions = [self._position(identifier) for identifier in queries]
        candidate_positions = [self._position(identifier) for identifier in candidates]
        totals = self._scores[np.ix_(query_positions, candidate_positions)].sum(axis=1)
        return dict(zip(queries, (float(value) for value in totals), strict=True))

    def _position(self, identifier: str) -> int:
        try:
            return self._positions[identifier]
        except KeyError as exc:
            raise KeyError(f"missing vector for issue {identifier!r}") from exc


class MultiViewSimilarityIndex:
    """Use whole-record recall plus addressable field-local similarities.

    The maximum score is an intentionally simple experimental union rule. The
    individual channel scores remain available so evaluation can determine
    whether a future calibrated fusion is warranted without hiding provenance.
    """

    def __init__(
        self,
        whole_record: SimilarityIndex,
        field_vectors: Mapping[str, Mapping[str, Vector]],
    ) -> None:
        self._whole_record = whole_record
        self._fields = {
            name: SimilarityIndex(vectors)
            for name, vectors in sorted(field_vectors.items())
            if vectors
        }

    @property
    def ids(self) -> tuple[str, ...]:
        return self._whole_record.ids

    @property
    def channels(self) -> tuple[str, ...]:
        return ("whole_record", *self._fields)

    def score(self, left_id: str, right_id: str) -> float:
        return max(score for _channel, score in self.channel_scores(left_id, right_id))

    def channel_scores(self, left_id: str, right_id: str) -> tuple[tuple[str, float], ...]:
        scores = [("whole_record", self._whole_record.score(left_id, right_id))]
        for name, index in self._fields.items():
            if left_id in index.ids and right_id in index.ids:
                scores.append((name, index.score(left_id, right_id)))
        return tuple(sorted(scores, key=lambda item: (-item[1], item[0])))

    def channel_rank(
        self,
        channel: str,
        query_id: str,
        target_id: str,
        candidate_ids: Sequence[str],
    ) -> int | None:
        index = self._whole_record if channel == "whole_record" else self._fields.get(channel)
        if index is None or query_id not in index.ids or target_id not in index.ids:
            return None
        available = [identifier for identifier in candidate_ids if identifier in index.ids]
        ranked = index.ranked(query_id, available)
        return next(
            (
                position
                for position, (identifier, _score) in enumerate(ranked, start=1)
                if identifier == target_id
            ),
            None,
        )

    def pairs_at_or_above(
        self,
        left_ids: Sequence[str],
        right_ids: Sequence[str],
        threshold: float,
        *,
        upper_triangle: bool = False,
        eligible_ids: frozenset[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Union exact threshold pairs from every semantic view."""

        pairs = set(
            self._whole_record.pairs_at_or_above(
                left_ids,
                right_ids,
                threshold,
                upper_triangle=upper_triangle,
                eligible_ids=eligible_ids,
            )
        )
        for index in self._fields.values():
            available = set(index.ids)
            field_left = [identifier for identifier in left_ids if identifier in available]
            field_right = [identifier for identifier in right_ids if identifier in available]
            pairs.update(
                index.pairs_at_or_above(
                    field_left,
                    field_right,
                    threshold,
                    upper_triangle=upper_triangle,
                    eligible_ids=eligible_ids,
                )
            )
        left_order = {identifier: position for position, identifier in enumerate(left_ids)}
        right_order = {identifier: position for position, identifier in enumerate(right_ids)}
        return sorted(pairs, key=lambda pair: (left_order[pair[0]], right_order[pair[1]]))


def normalize(vector: Vector) -> list[float]:
    """Return a unit vector, rejecting malformed embedding output."""

    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("vectors must be non-empty and finite")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        raise ValueError("cannot normalize a zero vector")
    return [value / norm for value in values]


def cosine_similarity(left: Vector, right: Vector) -> float:
    """Compute cosine similarity without requiring NumPy."""

    if len(left) != len(right):
        raise ValueError("vectors must have the same dimension")
    normalized_left = normalize(left)
    normalized_right = normalize(right)
    score = sum(a * b for a, b in zip(normalized_left, normalized_right, strict=True))
    # Avoid surprising values such as 1.0000000000000002 in reports and thresholds.
    return max(-1.0, min(1.0, score))


def nearest_neighbors(
    query: Any,
    candidates: Sequence[Any],
    vectors: Mapping[str, Vector],
    *,
    limit: int = 10,
    include_closed: bool = False,
    similarity_index: SimilarityIndex | None = None,
) -> list[Neighbor]:
    """Rank candidates by similarity, breaking equal scores by issue ID.

    Records may be mappings or objects and need only expose ``id`` (or
    ``issue_id``) and, when lifecycle filtering matters, ``status``.
    """

    if limit < 0:
        raise ValueError("limit cannot be negative")
    query_id = issue_id(query)
    if query_id not in vectors:
        raise KeyError(f"missing vector for query issue {query_id!r}")
    index = similarity_index or SimilarityIndex(vectors)

    ranked: list[Neighbor] = []
    for candidate in candidates:
        candidate_id = issue_id(candidate)
        if candidate_id == query_id:
            continue
        status = issue_status(candidate)
        if not include_closed and _is_closed(status):
            continue
        if candidate_id not in vectors:
            raise KeyError(f"missing vector for candidate issue {candidate_id!r}")
        ranked.append(
            Neighbor(
                issue_id=candidate_id,
                status=status,
                similarity=index.score(query_id, candidate_id),
            )
        )
    ranked.sort(key=lambda neighbor: (-neighbor.similarity, neighbor.issue_id))
    return ranked[:limit]


def balanced_batches(
    issues: Sequence[Any],
    vectors: Mapping[str, Vector],
    *,
    target_size: int = 9,
    similarity_index: SimilarityIndex | None = None,
) -> list[list[Any]]:
    """Partition issues into deterministic, balanced semantic neighborhoods.

    The densest remaining record is selected as each seed; its closest remaining
    records fill the batch. Every input appears exactly once, and batch sizes
    differ by at most one.
    """

    if target_size < 1:
        raise ValueError("target_size must be positive")
    if not issues:
        return []

    by_id: dict[str, Any] = {}
    for issue in issues:
        identifier = issue_id(issue)
        if identifier in by_id:
            raise ValueError(f"duplicate issue ID: {identifier}")
        if identifier not in vectors:
            raise KeyError(f"missing vector for issue {identifier!r}")
        by_id[identifier] = issue

    count = len(by_id)
    # Round half up rather than using Python's even-number tie breaking.
    batch_count = max(1, (count + target_size // 2) // target_size)
    batch_count = min(batch_count, count)
    quotient, remainder = divmod(count, batch_count)
    capacities = [quotient + (index < remainder) for index in range(batch_count)]

    index = similarity_index or SimilarityIndex(
        {identifier: vectors[identifier] for identifier in by_id}
    )
    remaining = set(by_id)
    ordered_ids = sorted(remaining)
    totals = index.sum_scores(ordered_ids, ordered_ids)
    for identifier in ordered_ids:
        totals[identifier] -= index.score(identifier, identifier)
    batches: list[list[Any]] = []
    for capacity in capacities:
        if len(remaining) == 1:
            seed = next(iter(remaining))
        else:
            seed = min(
                remaining,
                key=lambda identifier: (-totals[identifier] / (len(remaining) - 1), identifier),
            )
        ordered = [
            identifier for identifier, _score in index.ranked(seed, sorted(remaining - {seed}))
        ]
        member_ids = [seed, *ordered[: capacity - 1]]
        batches.append([by_id[identifier] for identifier in member_ids])
        remaining.difference_update(member_ids)
        if remaining:
            reductions = index.sum_scores(sorted(remaining), member_ids)
            for identifier, reduction in reductions.items():
                totals[identifier] -= reduction
    return batches


def candidate_batches(
    issues: Sequence[Any],
    candidates: Sequence[Mapping[str, Any]],
    vectors: Mapping[str, Vector],
    *,
    target_size: int = 9,
    similarity_index: SimilarityIndex | None = None,
) -> list[list[Any]]:
    """Batch only issues participating in accepted review signals.

    Candidate pairs form an undirected graph over the supplied issue population.
    Each connected component is batched independently, so unrelated signal
    clusters and echo-only singleton records are never forced together merely to
    reach the requested size. Closed echo targets are naturally omitted because
    they are not members of ``issues``.
    """

    return [
        list(batch.issues)
        for batch in package_candidate_batches(
            issues,
            candidates,
            vectors,
            max_batch_size=target_size,
            similarity_index=similarity_index,
        ).batches
    ]


def package_candidate_batches(
    issues: Sequence[Any],
    candidates: Sequence[Mapping[str, Any]],
    vectors: Mapping[str, Vector],
    *,
    max_batch_size: int = 9,
    similarity_index: SimilarityIndex | None = None,
) -> CandidatePackaging:
    """Create bounded review handoffs and deterministic fragmentation diagnostics.

    Connected candidate components are split into connected induced review units.
    Independent singleton components are explicitly packaged into bounded envelopes;
    they remain separate one-issue review units rather than being presented as a
    semantic cluster.
    """

    if max_batch_size < 1:
        raise ValueError("max_batch_size must be positive")

    by_id: dict[str, Any] = {}
    for issue in issues:
        identifier = issue_id(issue)
        if identifier in by_id:
            raise ValueError(f"duplicate issue ID: {identifier}")
        if identifier not in vectors:
            raise KeyError(f"missing vector for issue {identifier!r}")
        by_id[identifier] = issue

    adjacency: dict[str, set[str]] = {}
    for candidate in candidates:
        left_id = str(candidate.get("issue_id", ""))
        right_id = str(candidate.get("related_issue_id", ""))
        active_endpoints = [identifier for identifier in (left_id, right_id) if identifier in by_id]
        for identifier in active_endpoints:
            adjacency.setdefault(identifier, set())
        if len(active_endpoints) == 2 and active_endpoints[0] != active_endpoints[1]:
            left_active, right_active = active_endpoints
            adjacency[left_active].add(right_active)
            adjacency[right_active].add(left_active)

    components: list[list[str]] = []
    unseen = set(adjacency)
    while unseen:
        start = min(unseen)
        component: list[str] = []
        pending = [start]
        unseen.remove(start)
        while pending:
            identifier = pending.pop()
            component.append(identifier)
            neighbors = sorted(adjacency[identifier] & unseen, reverse=True)
            unseen.difference_update(neighbors)
            pending.extend(neighbors)
        components.append(sorted(component))

    index = similarity_index or SimilarityIndex(vectors)
    singleton_ids = [component[0] for component in components if len(component) == 1]
    non_singletons = [component for component in components if len(component) > 1]
    review_batches: list[ReviewBatch] = []
    assignment: dict[str, int] = {}
    fragmented = 0

    for component in non_singletons:
        units = _split_connected_component(component, adjacency, index, max_batch_size)
        fragmented += len(units) > 1
        for unit in units:
            batch_number = len(review_batches)
            assignment.update({identifier: batch_number for identifier in unit})
            review_batches.append(
                ReviewBatch(
                    issues=tuple(by_id[identifier] for identifier in unit),
                    kind="connected-component",
                    review_units=(tuple(unit),),
                )
            )

    for offset in range(0, len(singleton_ids), max_batch_size):
        envelope = singleton_ids[offset : offset + max_batch_size]
        batch_number = len(review_batches)
        assignment.update({identifier: batch_number for identifier in envelope})
        review_batches.append(
            ReviewBatch(
                issues=tuple(by_id[identifier] for identifier in envelope),
                kind="singleton-envelope",
                review_units=tuple((identifier,) for identifier in envelope),
            )
        )

    edges = {
        tuple(sorted((identifier, neighbor)))
        for identifier, neighbors in adjacency.items()
        for neighbor in neighbors
        if identifier != neighbor
    }
    cut_edges = sum(assignment[left] != assignment[right] for left, right in edges)
    diagnostics = BatchDiagnostics(
        component_count=len(components),
        singleton_component_count=len(singleton_ids),
        agent_envelope_count=(len(singleton_ids) + max_batch_size - 1) // max_batch_size,
        fragmented_component_count=fragmented,
        cross_batch_candidate_edges=cut_edges,
        max_batch_size=max((len(batch.issues) for batch in review_batches), default=0),
        configured_max_batch_size=max_batch_size,
    )
    return CandidatePackaging(tuple(review_batches), diagnostics)


def _split_connected_component(
    component: Sequence[str],
    adjacency: Mapping[str, set[str]],
    index: SimilarityIndex,
    max_size: int,
) -> list[list[str]]:
    """Greedily peel connected units while favoring candidate edges kept inside."""

    pending_components = [set(component)]
    units: list[list[str]] = []
    while pending_components:
        remaining = pending_components.pop(0)
        if len(remaining) <= max_size:
            units.append(sorted(remaining))
            continue
        seed = min(
            remaining,
            # Start at a boundary node so bridge-shaped components are peeled
            # from the edge rather than split into several remainder islands.
            key=lambda identifier: (len(adjacency[identifier] & remaining), identifier),
        )
        unit = {seed}
        while len(unit) < max_size:
            frontier = {neighbor for member in unit for neighbor in adjacency[member] & remaining}
            frontier.difference_update(unit)
            if not frontier:
                break
            candidate = min(
                frontier,
                key=lambda identifier: (
                    -len(adjacency[identifier] & unit),
                    -sum(index.score(identifier, member) for member in unit),
                    identifier,
                ),
            )
            unit.add(candidate)
        units.append(sorted(unit))
        leftover = remaining - unit
        pending_components = _connected_id_components(leftover, adjacency) + pending_components
    return units


def _connected_id_components(
    identifiers: set[str], adjacency: Mapping[str, set[str]]
) -> list[set[str]]:
    components: list[set[str]] = []
    unseen = set(identifiers)
    while unseen:
        start = min(unseen)
        component = {start}
        pending = [start]
        unseen.remove(start)
        while pending:
            current = pending.pop()
            neighbors = adjacency[current] & unseen
            unseen.difference_update(neighbors)
            component.update(neighbors)
            pending.extend(sorted(neighbors, reverse=True))
        components.append(component)
    return components


def issue_id(issue: Any) -> str:
    value = _field(issue, "id", _field(issue, "issue_id", None))
    if value is None or str(value) == "":
        raise ValueError("issue must have a non-empty id or issue_id")
    return str(value)


def issue_status(issue: Any) -> str | None:
    value = _field(issue, "status", None)
    return None if value is None else str(value)


def issue_text(issue: Any) -> str:
    """Read pre-canonicalized text from a duck-typed issue record."""

    value = _field(issue, "text", _field(issue, "canonical_text", None))
    if value is None:
        raise ValueError("issue must have text or canonical_text")
    return str(value)


def _field(issue: Any, name: str, default: Any) -> Any:
    if isinstance(issue, Mapping):
        return issue.get(name, default)
    return getattr(issue, name, default)


def _is_closed(status: str | None) -> bool:
    return status is not None and status.casefold() in {"closed", "done", "completed", "resolved"}
