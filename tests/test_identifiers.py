from embead.identifiers import IssueIdResolver, issue_id_forms, prune_ancestors


def test_forms_cover_prefix_less_and_dashed_spellings() -> None:
    assert issue_id_forms("proj-abc12.4.1") == {
        "proj-abc12.4.1",
        "proj-abc12-4-1",
        "abc12.4.1",
        "abc12-4-1",
    }


def test_short_top_level_hash_below_minimum_is_not_a_form() -> None:
    # Three characters look like ordinary branch tokens ("c10"); a child suffix adds structure.
    assert issue_id_forms("proj-c10") == {"proj-c10"}
    assert "c10.2" in issue_id_forms("proj-c10.2")


def test_resolver_prefers_exact_ids_and_refuses_ambiguous_short_forms() -> None:
    resolver = IssueIdResolver(["one-abc12.4", "two-abc12.4", "one-xyz99.7"])

    assert resolver.resolve("ONE-ABC12.4") == "one-abc12.4"
    assert resolver.resolve("abc12.4") is None
    assert resolver.resolve("xyz99-7") == "one-xyz99.7"
    assert resolver.resolve("unknown") is None


def test_prune_ancestors_keeps_the_deepest_hierarchical_id() -> None:
    assert prune_ancestors(["p-a", "p-a.1", "p-a.1.2", "p-b"]) == ("p-a.1.2", "p-b")


def test_branch_candidates_read_runs_of_segments() -> None:
    resolver = IssueIdResolver(["proj-abc12", "proj-abc12.47", "proj-xyz99.3"])

    assert resolver.branch_candidates("cfg/abc12-47-call-doors") == ("proj-abc12.47",)
    assert resolver.branch_candidates("lane/xyz99.3-gate") == ("proj-xyz99.3",)
    assert resolver.branch_candidates("proj-abc12.47_fix") == ("proj-abc12.47",)
    assert resolver.branch_candidates("lane/unrelated-topic") == ()
