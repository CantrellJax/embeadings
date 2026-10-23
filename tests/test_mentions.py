from embead.mentions import claims_between, extract_claims, kind_counts, resolved_elsewhere
from embead.models import DependencyLink, IssueRecord


def _issue(identifier: str, status: str = "open", **fields: str) -> IssueRecord:
    return IssueRecord(
        id=identifier, title=fields.pop("title", identifier), status=status, **fields
    )


def test_verbs_resolve_full_and_short_ids_across_fields() -> None:
    issues = (
        _issue("demo-abc12", notes="Scope absorbed by demo-xyz34.2 during planning."),
        _issue("demo-xyz34"),
        _issue("demo-xyz34.2", description="Supersedes abc12 and demo-qqq99."),
        _issue("demo-qqq99", status="closed"),
        _issue("demo-fff11", design="Duplicate of `demo-qqq99`.", close_reason="fixed by xyz34"),
    )

    claims = extract_claims(issues, issues)

    assert [(c.issue_id, c.kind, c.related_issue_id, c.source_fields) for c in claims] == [
        ("demo-fff11", "duplicate-of", "demo-qqq99", ("design",)),
        ("demo-abc12", "absorbed-by", "demo-xyz34.2", ("notes",)),
        ("demo-fff11", "fixed-by", "demo-xyz34", ("close_reason",)),
        ("demo-xyz34.2", "supersedes", "demo-abc12", ("description",)),
        ("demo-xyz34.2", "supersedes", "demo-qqq99", ("description",)),
    ]
    assert kind_counts(claims) == {
        "duplicate-of": 1,
        "absorbed-by": 1,
        "fixed-by": 1,
        "supersedes": 2,
    }


def test_denials_self_references_and_unknown_ids_are_not_claims() -> None:
    issues = (
        _issue("demo-abc12", notes="Not a duplicate of demo-xyz34. Superseded by demo-abc12."),
        _issue("demo-xyz34", notes="Folded into the parser epic; fixed by demo-zzz00."),
    )

    assert extract_claims(issues, issues) == ()


def test_tentative_claims_remain_review_evidence() -> None:
    issues = (_issue("demo-abc12", notes="May be a duplicate of demo-xyz34."), _issue("demo-xyz34"))

    assert [claim.kind for claim in extract_claims(issues, issues)] == ["duplicate-of"]


def test_typed_links_are_recorded_on_the_claim() -> None:
    issues = (
        IssueRecord(
            id="demo-abc12",
            title="linked by dependency",
            status="open",
            notes="Superseded by demo-xyz34.",
            dependency_links=(DependencyLink("demo-abc12", "demo-xyz34", "related"),),
        ),
        IssueRecord(
            id="demo-kid",
            title="linked by parent",
            status="open",
            parent_id="demo-xyz34",
            notes="Absorbed by demo-xyz34.",
        ),
        _issue("demo-lone", notes="Absorbed by demo-xyz34."),
        _issue("demo-xyz34"),
    )

    assert {(c.issue_id, c.typed_link) for c in extract_claims(issues, issues)} == {
        ("demo-abc12", True),
        ("demo-kid", True),
        ("demo-lone", False),
    }


def test_bare_mentions_are_opt_in_and_need_structured_spelling() -> None:
    issues = (
        _issue("demo-abc12", notes="See demo-xyz34.1 and qqq99 later; absorbed by demo-xyz34.1."),
        _issue("demo-xyz34.1"),
        _issue("demo-qqq99"),
        _issue("demo-mmm55", description="Context lives in xyz34.1."),
    )

    assert {c.kind for c in extract_claims(issues, issues)} == {"absorbed-by"}
    with_mentions = extract_claims(issues, issues, include_mentions=True)
    # A verb claim is not repeated as a mention, and the bare hash qqq99 is never a mention.
    assert [(c.issue_id, c.kind, c.related_issue_id) for c in with_mentions] == [
        ("demo-abc12", "absorbed-by", "demo-xyz34.1"),
        ("demo-mmm55", "mention", "demo-xyz34.1"),
    ]
    assert len(claims_between(with_mentions, "demo-xyz34.1", "demo-abc12")) == 1


def test_extraction_is_deterministic_under_input_order() -> None:
    issues = [
        _issue("demo-b", notes="Duplicate of demo-a."),
        _issue("demo-a", notes="Replaced by demo-c."),
        _issue("demo-c"),
    ]

    assert extract_claims(issues, issues) == extract_claims(issues[::-1], issues[::-1])


def test_claim_object_may_follow_a_short_noun_phrase_within_the_clause() -> None:
    issues = (
        _issue("demo-abc12", notes="Absorbed by the board door demo-xyz34.1. Related demo-qqq99."),
        _issue("demo-mmm55", notes="Same defect as xyz34.1; carried by demo-qqq99 now."),
        _issue("demo-nnn66", notes="Fixed by the parser rewrite; demo-qqq99 is unrelated."),
        _issue("demo-xyz34.1"),
        _issue("demo-qqq99"),
    )

    assert [(c.issue_id, c.kind, c.related_issue_id) for c in extract_claims(issues, issues)] == [
        ("demo-mmm55", "duplicate-of", "demo-xyz34.1"),
        ("demo-abc12", "absorbed-by", "demo-xyz34.1"),
        ("demo-mmm55", "absorbed-by", "demo-qqq99"),
    ]


def test_fold_notes_read_as_lineage_claims() -> None:
    issues = (
        IssueRecord(id="proj-aaaa1", title="a", status="open", notes="FOLDED into proj-bbbb2 (x)."),
        IssueRecord(id="proj-bbbb2", title="b", status="open", notes="Folded in: proj-cccc3"),
        IssueRecord(id="proj-cccc3", title="c", status="open"),
    )

    claims = {(c.issue_id, c.kind, c.related_issue_id) for c in extract_claims(issues, issues)}

    assert claims == {
        ("proj-aaaa1", "absorbed-by", "proj-bbbb2"),
        ("proj-bbbb2", "absorbs", "proj-cccc3"),
    }


def test_resolved_elsewhere_reads_folds_duplicates_and_pr_references() -> None:
    issues = (
        IssueRecord(id="proj-aaaa1", title="a", status="open", notes="FOLDED into proj-bbbb2 (x)."),
        IssueRecord(id="proj-bbbb2", title="b", status="open", notes="Folded in: proj-aaaa1"),
        IssueRecord(
            id="proj-cccc3", title="c", status="closed", close_reason="Superseded by #5495 (fix)"
        ),
        IssueRecord(id="proj-dddd4", title="d", status="open", notes="Merged in #5469 today"),
        IssueRecord(id="proj-eeee5", title="e", status="open", notes="Not superseded by #12."),
        IssueRecord(id="proj-ffff6", title="f", status="open", notes="See #12 for context."),
        IssueRecord(
            id="proj-gggg7",
            title="g",
            status="closed",
            close_reason="Duplicate of proj-bbbb2 item (1)",
        ),
    )

    assert resolved_elsewhere(issues, issues) == {
        "proj-aaaa1",
        "proj-cccc3",
        "proj-dddd4",
        "proj-gggg7",
    }


def test_duplicate_direction_reads_typed_links_before_text() -> None:
    from embead.mentions import duplicate_direction

    seed = _issue("demo-seed1")
    copy = IssueRecord(
        id="demo-copy1",
        title="copy",
        status="closed",
        dependency_links=(DependencyLink("demo-copy1", "demo-seed1", "duplicates"),),
    )
    replaced = IssueRecord(
        id="demo-old11",
        title="old",
        status="closed",
        dependency_links=(DependencyLink("demo-old11", "demo-seed1", "supersedes"),),
    )
    issues = (seed, copy, replaced)

    def direction(left, right):
        return duplicate_direction(left, right, extract_claims((left, right), issues))

    assert direction(seed, copy) == "neighbor_dup_of_seed"
    assert direction(copy, seed) == "seed_dup_of_neighbor"
    assert direction(seed, replaced) == "neighbor_dup_of_seed"


def test_duplicate_direction_reads_close_reasons_and_refuses_contradictions() -> None:
    from embead.mentions import duplicate_direction

    seed = _issue("demo-seed1")
    copy = _issue("demo-copy1", status="closed", close_reason="Duplicate of seed1")
    canonical = _issue("demo-canon", status="closed", close_reason="Done; supersedes seed1.")
    torn = _issue(
        "demo-torn1",
        status="closed",
        close_reason="Folded into demo-seed1",
        notes="Supersedes demo-seed1",
    )
    unrelated = _issue("demo-other", status="closed", close_reason="Fixed by demo-seed1")
    issues = (seed, copy, canonical, torn, unrelated)

    def direction(neighbor):
        return duplicate_direction(seed, neighbor, extract_claims((seed, neighbor), issues))

    assert direction(copy) == "neighbor_dup_of_seed"
    assert direction(canonical) == "seed_dup_of_neighbor"
    assert direction(torn) is None
    assert direction(unrelated) is None
