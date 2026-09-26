from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from embead.models import IssueRecord
from embead.surfaces import (
    _associate_worktrees,
    _Worktree,
    analyze_code_surfaces,
    compile_branch_patterns,
    extract_explicit_pointers,
    parse_worktree_mappings,
)


def completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr="")


def test_explicit_paths_are_conservative_and_never_copy_snippets() -> None:
    issue = IssueRecord(
        id="proj-1",
        title="Change `src/parser/dependencies.py::parse_dependency` and `README.md`",
        description=(
            "Coordinate foo/bar prose, https://example.com/src/private.py, and package version "
            "releases/1.2.3. Update docs/parser/ as well."
        ),
    )

    pointers = extract_explicit_pointers(issue, revision="abc123")

    assert [(item.path, item.symbol) for item in pointers] == [
        ("README.md", None),
        ("docs/parser/", None),
        ("src/parser/dependencies.py", "parse_dependency"),
    ]
    assert {item.source_field for item in pointers} == {"description", "title"}
    assert {item.edit_intent for item in pointers} == {"likely-edit"}
    assert all(not hasattr(item, "snippet") for item in pointers)


def test_explicit_pointers_ignore_templates_and_bound_reproduction_context(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    existing = repository / "src" / "worker.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("", encoding="utf-8")
    issue = IssueRecord(
        id="proj-1",
        title="Update `src/worker.py`",
        description=(
            "<!-- Run ruff path/to/template.py --fix. -->\n"
            "Reproduction:\n```sh\nruff examples/example.py --fix\n```"
        ),
    )

    pointers = extract_explicit_pointers(
        issue,
        revision="abc123",
        repository_path=repository,
    )

    assert [pointer.path for pointer in pointers] == ["examples/example.py", "src/worker.py"]
    by_path = {pointer.path: pointer for pointer in pointers}
    assert by_path["src/worker.py"].context_kind == "prose"
    assert by_path["src/worker.py"].path_presence == "existing"
    assert by_path["examples/example.py"].context_kind == "code-fence"
    assert by_path["examples/example.py"].path_presence == "missing"
    assert all(pointer.path != "path/to/template.py" for pointer in pointers)


def test_explicit_edit_intent_is_bounded_and_orders_without_suppression() -> None:
    issues = [
        IssueRecord(id="edit-a", title="Edit src/edit/core.py"),
        IssueRecord(id="edit-b", title="Update src/edit/core.py"),
        IssueRecord(id="mixed-a", title="Refactor src/mixed/core.py"),
        IssueRecord(id="mixed-b", title="Review src/mixed/core.py"),
        IssueRecord(id="unknown-a", title="src/unknown/core.py"),
        IssueRecord(id="unknown-b", title="src/unknown/core.py"),
        IssueRecord(id="reference-a", title="Review src/reference/core.py"),
        IssueRecord(id="reference-b", title="Inspect src/reference/core.py"),
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=None)

    assert len(analysis.collisions) == 4
    assert [collision.edit_intent for collision in analysis.collisions] == [
        "likely-edit",
        "mixed",
        "unknown",
        "reference-only",
    ]
    assert all(collision.intent_source_fields == ("title",) for collision in analysis.collisions)
    assert all(collision.confidence == "explicit" for collision in analysis.collisions)


def test_explicit_pointer_order_breaks_same_surface_intent_ties() -> None:
    issue = IssueRecord(
        id="task-1",
        title="Repeated surface",
        description=(
            "Inspect `src/worker.py` before planning. "
            + ("Unrelated context. " * 12)
            + "Then update `src/worker.py` during implementation."
        ),
    )

    pointers = extract_explicit_pointers(issue, revision="abc123")

    assert [(pointer.path, pointer.edit_intent) for pointer in pointers] == [
        ("src/worker.py", "likely-edit"),
        ("src/worker.py", "reference-only"),
    ]


def test_collision_order_prefers_grounded_prose_without_suppressing_references(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    existing = repository / "src" / "existing.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("", encoding="utf-8")

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command == ("rev-parse", "--is-inside-work-tree"):
            return completed("true\n")
        if command == ("rev-parse", "HEAD"):
            return completed("head\n")
        if command == ("rev-parse", "--verify", "origin/main"):
            return completed("base\n")
        if command == ("rev-parse", "origin/main"):
            return completed("base\n")
        if command == ("worktree", "list", "--porcelain"):
            return completed(f"worktree {repository}\nHEAD head\nbranch refs/heads/main\n")
        if command[:2] == ("diff", "--name-only"):
            return completed()
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    issues = [
        IssueRecord(id="existing-a", title="src/existing.py"),
        IssueRecord(id="existing-b", title="src/existing.py"),
        IssueRecord(id="missing-a", title="src/missing.py"),
        IssueRecord(id="missing-b", title="src/missing.py"),
        IssueRecord(
            id="fenced-a", title="Reproduction", description="```\nexamples/example.py\n```"
        ),
        IssueRecord(
            id="fenced-b", title="Reproduction", description="```\nexamples/example.py\n```"
        ),
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=repository, runner=runner)

    assert [collision.shared_paths for collision in analysis.collisions] == [
        ("src/existing.py",),
        ("src/missing.py",),
        ("examples/example.py",),
    ]
    assert [collision.context_kind for collision in analysis.collisions] == [
        "prose",
        "prose",
        "code-fence-only",
    ]
    assert [collision.path_presence for collision in analysis.collisions] == [
        "all-existing",
        "all-missing",
        "all-missing",
    ]


def test_explicit_surfaces_report_exact_collisions_and_suppress_module_only_pairs() -> None:
    issues = [
        IssueRecord(id="proj-1", title="Edit src/parser/dependencies.py"),
        IssueRecord(id="proj-2", title="Repair src/parser/dependencies.py"),
        IssueRecord(id="proj-3", title="Test src/parser/payload.py"),
        IssueRecord(id="proj-4", title="Unrelated prose"),
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=None)

    assert analysis.repository_available is False
    assert analysis.pointer_count == 3
    assert analysis.issues_without_surfaces == 1
    assert [item.kind for item in analysis.collisions] == ["exact-file"]
    assert analysis.pairs_omitted_by_module_guard == 2
    exact = analysis.collisions[0]
    assert exact.shared_paths == ("src/parser/dependencies.py",)
    assert exact.confidence == "explicit"
    assert exact.context_kind == "prose"
    assert exact.path_presence == "unavailable"
    assert exact.revision_relation == "unavailable"
    assert analysis.warnings == (
        "No Git repository was available; only explicit code references were analyzed.",
    )


def test_explicit_only_shared_directory_pointer_is_suppressed() -> None:
    issues = [
        IssueRecord(id="proj-1", title="Update `src/parser/`"),
        IssueRecord(id="proj-2", title="Refactor `src/parser/`"),
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=None)

    assert analysis.collisions == ()
    assert analysis.pairs_omitted_by_module_guard == 1


def test_incremental_collision_scope_keeps_only_pairs_touching_eligible_issue() -> None:
    issues = [
        IssueRecord(id="changed", title="Update `src/shared.py`"),
        IssueRecord(
            id="unchanged-a",
            title="Modify `src/shared.py` and `src/other.py`",
        ),
        IssueRecord(id="unchanged-b", title="Refactor `src/other.py`"),
    ]

    analysis = analyze_code_surfaces(
        issues,
        workspace_path=None,
        eligible_issue_ids=frozenset({"changed"}),
    )

    assert {
        (collision.issue_id, collision.related_issue_id) for collision in analysis.collisions
    } == {("changed", "unchanged-a")}


def test_shared_path_symbol_is_preserved_as_bounded_evidence() -> None:
    issues = [
        IssueRecord(id="proj-1", title="Change `src/parser/core.py::parse`"),
        IssueRecord(id="proj-2", title="Repair `src/parser/core.py::parse`"),
    ]

    collision = analyze_code_surfaces(issues, workspace_path=None).collisions[0]

    assert collision.shared_paths == ("src/parser/core.py",)
    assert collision.shared_symbols == ("src/parser/core.py::parse",)


def test_explicit_hub_path_is_summarized_instead_of_emitting_every_pair() -> None:
    issues = [
        IssueRecord(id=f"proj-{index}", title="Review docs/architecture/session.md")
        for index in range(6)
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=None, hub_surface_limit=5)

    assert analysis.collisions == ()
    assert analysis.pairs_omitted_by_hub_guard == 15
    assert analysis.hub_surfaces == (
        {
            "kind": "path",
            "surface": "docs/architecture/session.md",
            "issue_count": 6,
        },
        {"kind": "module", "surface": "docs/architecture", "issue_count": 6},
    )


def test_hub_guard_sample_names_the_suppressing_surfaces() -> None:
    issues = [
        IssueRecord(id=f"proj-{index}", title="Review docs/architecture/session.md")
        for index in range(6)
    ]

    analysis = analyze_code_surfaces(
        issues,
        workspace_path=None,
        hub_surface_limit=5,
        hub_guard_sample_limit=2,
    )

    assert analysis.pairs_omitted_by_hub_guard == 15
    assert analysis.hub_guard_sample == (
        {
            "issue_id": "proj-0",
            "related_issue_id": "proj-1",
            "suppressing_paths": ["docs/architecture/session.md"],
            "suppressing_modules": ["docs/architecture"],
        },
        {
            "issue_id": "proj-0",
            "related_issue_id": "proj-2",
            "suppressing_paths": ["docs/architecture/session.md"],
            "suppressing_modules": ["docs/architecture"],
        },
    )


def test_hub_guard_sample_is_empty_unless_requested() -> None:
    issues = [
        IssueRecord(id=f"proj-{index}", title="Review docs/architecture/session.md")
        for index in range(6)
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=None, hub_surface_limit=5)

    assert analysis.pairs_omitted_by_hub_guard == 15
    assert analysis.hub_guard_sample == ()


def test_non_hub_second_path_preserves_one_pair_behind_hub_guard() -> None:
    issues = [
        IssueRecord(
            id=f"proj-{index}",
            title=(
                "Review docs/architecture/session.md and src/specific/pair.py"
                if index < 2
                else "Review docs/architecture/session.md"
            ),
        )
        for index in range(6)
    ]

    analysis = analyze_code_surfaces(issues, workspace_path=None, hub_surface_limit=5)

    assert len(analysis.collisions) == 1
    assert analysis.collisions[0].shared_paths == ("src/specific/pair.py",)
    assert analysis.pairs_omitted_by_hub_guard == 14


def test_worktree_diffs_are_observed_and_revision_bound(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first = tmp_path / "worktree-1"
    second = tmp_path / "worktree-2"
    root.mkdir()
    first.mkdir()
    second.mkdir()

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command == ("rev-parse", "--is-inside-work-tree"):
            return completed("true\n")
        if command == ("rev-parse", "HEAD"):
            return completed("root-head\n")
        if command == ("rev-parse", "--verify", "origin/main"):
            return completed("base-head\n")
        if command == ("rev-parse", "origin/main"):
            return completed("base-head\n")
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {first}\nHEAD head-one\nbranch refs/heads/codex/bead-1-parser\n\n"
                f"worktree {second}\nHEAD head-two\nbranch refs/heads/codex/bead-2-cache\n"
            )
        if command[:4] == ("diff", "--name-only", "--diff-filter=ACMRD", "-z"):
            changed = {
                first.resolve(): (
                    "src/shared/cache.py\0src/first/only.py\0.beads/interactions.jsonl\0"
                ),
                second.resolve(): "src/shared/cache.py\0src/second/only.py\0",
            }
            return completed(changed.get(cwd.resolve(), ""))
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    issues = [IssueRecord(id="proj.1", title="First"), IssueRecord(id="proj.2", title="Second")]
    analysis = analyze_code_surfaces(issues, workspace_path=root, runner=runner)

    assert analysis.issues_with_observed_surfaces == 2
    assert analysis.base_revision == "base-head"
    assert analysis.worktrees_discovered == 3
    assert analysis.worktrees_associated == 2
    assert analysis.source_counts == {"active-worktree-diff": 4}
    assert len(analysis.collisions) == 1
    collision = analysis.collisions[0]
    assert collision.kind == "exact-file"
    assert collision.confidence == "observed"
    assert collision.edit_intent == "observed-edit"
    assert collision.intent_source_fields == ()
    assert collision.context_kind == "observed-worktree"
    assert collision.path_presence == "all-missing"
    assert collision.shared_paths == ("src/shared/cache.py",)
    assert collision.revision_relation == "different"


def test_observed_shared_module_remains_a_collision_lead(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first = tmp_path / "worktree-1"
    second = tmp_path / "worktree-2"
    root.mkdir()
    first.mkdir()
    second.mkdir()

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD"): "root-head\n",
            ("rev-parse", "--verify", "origin/main"): "base-head\n",
            ("rev-parse", "origin/main"): "base-head\n",
        }
        if command in responses:
            return completed(responses[command])
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {first}\nHEAD head-one\nbranch refs/heads/bead-1\n\n"
                f"worktree {second}\nHEAD head-two\nbranch refs/heads/bead-2\n"
            )
        if command[:2] == ("diff", "--name-only"):
            changed = {
                first.resolve(): "src/parser/left.py\0",
                second.resolve(): "src/parser/right.py\0",
            }
            return completed(changed.get(cwd.resolve(), ""))
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    analysis = analyze_code_surfaces(
        [
            IssueRecord(id="proj.1", title="First"),
            IssueRecord(id="proj.2", title="Second"),
            IssueRecord(id="proj.3", title="Plan src/parser/third.py"),
        ],
        workspace_path=root,
        runner=runner,
    )

    assert len(analysis.collisions) == 3
    assert {collision.kind for collision in analysis.collisions} == {"shared-module"}
    assert [collision.confidence for collision in analysis.collisions] == [
        "observed",
        "corroborated",
        "corroborated",
    ]
    assert {collision.shared_modules for collision in analysis.collisions} == {("src/parser",)}
    assert analysis.pairs_omitted_by_module_guard == 0


def test_invoking_worktree_controls_revision_provenance(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    invoking = tmp_path / "worktree"
    common = owner / ".git"
    owner.mkdir()
    invoking.mkdir()
    common.mkdir()
    implementation_path = invoking / "src" / "parser" / "core.py"
    implementation_path.parent.mkdir(parents=True)
    implementation_path.write_text("", encoding="utf-8")

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command == ("rev-parse", "--show-toplevel"):
            return completed(f"{cwd.resolve()}\n")
        if command == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            return completed(f"{common.resolve()}\n")
        if command == ("rev-parse", "--is-inside-work-tree"):
            return completed("true\n")
        if command == ("rev-parse", "HEAD"):
            return completed(
                "invoking-head\n" if cwd.resolve() == invoking.resolve() else "stale-head\n"
            )
        if command == ("rev-parse", "--verify", "origin/main"):
            return completed("base-head\n")
        if command == ("rev-parse", "origin/main"):
            return completed("base-head\n")
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {owner}\nHEAD stale-head\nbranch refs/heads/main\n\n"
                f"worktree {invoking}\nHEAD invoking-head\nbranch refs/heads/bead-1\n"
            )
        if command[:2] == ("diff", "--name-only"):
            return completed()
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    analysis = analyze_code_surfaces(
        [IssueRecord(id="proj.1", title="Change src/parser/core.py")],
        workspace_path=owner,
        invocation_path=invoking,
        runner=runner,
    )

    assert analysis.repository_context == "invocation-worktree"
    assert analysis.repository_revision == "invoking-head"
    assert analysis.base_revision == "base-head"
    assert analysis.surfaces[0]["pointers"][0]["revision"] == "invoking-head"
    assert analysis.warnings == ()


def test_unrelated_invocation_falls_back_to_tracker_workspace(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    invoking = tmp_path / "unrelated"
    owner.mkdir()
    invoking.mkdir()

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        if command == ("rev-parse", "--show-toplevel"):
            return completed(f"{cwd.resolve()}\n")
        if command == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            return completed(f"{(cwd / '.git').resolve()}\n")
        if command == ("rev-parse", "--is-inside-work-tree"):
            return completed("true\n")
        if command == ("rev-parse", "HEAD"):
            return completed("owner-head\n")
        if command == ("rev-parse", "--verify", "origin/main"):
            return completed("base-head\n")
        if command == ("rev-parse", "origin/main"):
            return completed("base-head\n")
        if command == ("worktree", "list", "--porcelain"):
            return completed(f"worktree {owner}\nHEAD owner-head\nbranch refs/heads/main\n")
        if command[:2] == ("diff", "--name-only"):
            return completed()
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    analysis = analyze_code_surfaces(
        [], workspace_path=owner, invocation_path=invoking, runner=runner
    )

    assert analysis.repository_context == "tracker-workspace"
    assert analysis.repository_revision == "owner-head"
    assert analysis.warnings == (
        "Invoking Git repository does not share the tracker repository; provenance uses the "
        "tracker workspace.",
    )


def test_observed_to_explicit_exact_path_bypasses_hub_guard(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    worktree = tmp_path / "worktree-1"
    root.mkdir()
    worktree.mkdir()

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD"): "root-head\n",
            ("rev-parse", "--verify", "origin/main"): "base-head\n",
            ("rev-parse", "origin/main"): "base-head\n",
        }
        if command in responses:
            return completed(responses[command])
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {worktree}\nHEAD work-head\nbranch refs/heads/codex/bead-1\n"
            )
        if command[:2] == ("diff", "--name-only"):
            return completed("src/shared/cache.py\0")
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    issues = [
        IssueRecord(id="proj.1", title="Observed implementation"),
        IssueRecord(id="proj.2", title="Plan src/shared/cache.py"),
    ]
    analysis = analyze_code_surfaces(
        issues,
        workspace_path=root,
        runner=runner,
        hub_surface_limit=1,
    )

    assert len(analysis.collisions) == 1
    collision = analysis.collisions[0]
    assert collision.shared_paths == ("src/shared/cache.py",)
    assert collision.confidence == "corroborated"
    assert collision.evidence_sources == ("active-worktree-diff", "explicit-reference")
    assert analysis.pairs_omitted_by_hub_guard == 0


def test_pathological_base_diff_is_excluded_but_working_changes_remain(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    worktree = tmp_path / "worktree-1"
    root.mkdir()
    worktree.mkdir()
    historical_paths = "".join(f"src/history/file-{index}.py\0" for index in range(251))

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD"): "root-head\n",
            ("rev-parse", "--verify", "origin/main"): "base-head\n",
            ("rev-parse", "origin/main"): "base-head\n",
        }
        if command in responses:
            return completed(responses[command])
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {worktree}\nHEAD work-head\nbranch refs/heads/codex/bead-1\n"
            )
        if command == (
            "diff",
            "--name-only",
            "--diff-filter=ACMRD",
            "-z",
            "origin/main...HEAD",
        ):
            return completed(historical_paths)
        if command == ("diff", "--name-only", "--diff-filter=ACMRD", "-z", "HEAD"):
            return completed("src/live/tracked.py\0")
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed("src/live/untracked.py\0")
        raise AssertionError((cwd, arguments))

    analysis = analyze_code_surfaces(
        [IssueRecord(id="proj.1", title="Active implementation")],
        workspace_path=root,
        runner=runner,
    )

    assert analysis.base_reference == "origin/main"
    assert analysis.base_revision == "base-head"
    assert analysis.pointer_count == 2
    assert analysis.source_counts == {"active-worktree-diff": 2}
    assert [pointer["path"] for pointer in analysis.surfaces[0]["pointers"]] == [
        "src/live/tracked.py",
        "src/live/untracked.py",
    ]
    assert analysis.warnings == (
        "Observed committed diff for issue proj.1 was excluded: base reference 'origin/main' "
        "produced 251 eligible code-surface paths, above the safety limit of 250. Tracked and "
        "untracked working-tree changes remain included; choose a current --base-ref to restore "
        "committed-change evidence.",
    )


def test_failed_base_diff_is_diagnostic_and_working_changes_remain(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    worktree = tmp_path / "worktree-1"
    root.mkdir()
    worktree.mkdir()

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD"): "root-head\n",
            ("rev-parse", "--verify", "origin/main"): "base-head\n",
            ("rev-parse", "origin/main"): "base-head\n",
        }
        if command in responses:
            return completed(responses[command])
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {worktree}\nHEAD work-head\nbranch refs/heads/codex/bead-1\n"
            )
        if command[-1:] == ("origin/main...HEAD",):
            return completed(returncode=128)
        if command == ("diff", "--name-only", "--diff-filter=ACMRD", "-z", "HEAD"):
            return completed("src/live/tracked.py\0")
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    analysis = analyze_code_surfaces(
        [IssueRecord(id="proj.1", title="Active implementation")],
        workspace_path=root,
        runner=runner,
    )

    assert analysis.pointer_count == 1
    assert analysis.warnings == (
        "Observed committed diff for issue proj.1 could not be computed from base reference "
        "'origin/main'. Tracked and untracked working-tree changes remain included; verify "
        "--base-ref for committed-change evidence.",
    )


def test_explicit_mapping_validates_issue_and_registered_worktree(tmp_path: Path) -> None:
    mappings = parse_worktree_mappings([f"proj.1={tmp_path}"])
    assert mappings == {"proj.1": tmp_path.resolve()}

    with pytest.raises(ValueError, match="ISSUE_ID=PATH"):
        parse_worktree_mappings(["proj.1"])
    with pytest.raises(ValueError, match="duplicate"):
        parse_worktree_mappings([f"proj.1={tmp_path}", f"proj.1={tmp_path}"])


def test_explicit_mapping_reserves_worktree_from_version_suffix_match(tmp_path: Path) -> None:
    release = _Worktree(
        path=tmp_path.resolve(),
        head="release-head",
        branch="codex/embead-kct-release-v0.4.0",
    )

    associations = _associate_worktrees(
        ("embead-3ur.67.4", "embead-kct"),
        (release,),
        {"embead-kct": tmp_path},
    )

    assert associations == {"embead-kct": release}


def test_version_component_is_not_a_numeric_suffix_match(tmp_path: Path) -> None:
    release = _Worktree(
        path=tmp_path.resolve(),
        head="release-head",
        branch="codex/release-v0.4.0",
    )

    assert _associate_worktrees(("embead-3ur.67.4",), (release,), {}) == {}


def test_full_id_and_bead_token_auto_association_remain_supported(tmp_path: Path) -> None:
    exact = _Worktree(tmp_path / "exact", "exact-head", "codex/proj.20-parser")
    suffix = _Worktree(tmp_path / "suffix", "suffix-head", "codex/bead-21-cache")

    associations = _associate_worktrees(("proj.20", "proj.21"), (exact, suffix), {})

    assert associations == {"proj.20": exact, "proj.21": suffix}


def test_duplicate_explicit_worktree_ownership_is_rejected(tmp_path: Path) -> None:
    worktree = _Worktree(tmp_path.resolve(), "head", "codex/shared")

    with pytest.raises(ValueError, match="only one issue"):
        _associate_worktrees(
            ("proj.first", "proj.second"),
            (worktree,),
            {"proj.first": tmp_path, "proj.second": tmp_path},
        )


def test_mapping_outside_population_explains_active_filter_requirements(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    worktree = tmp_path / "worktree-1"
    root.mkdir()
    worktree.mkdir()

    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD"): "root-head\n",
            ("rev-parse", "--verify", "origin/main"): "base-head\n",
            ("rev-parse", "origin/main"): "base-head\n",
        }
        if command in responses:
            return completed(responses[command])
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {worktree}\nHEAD work-head\nbranch refs/heads/bead-closed\n"
            )
        raise AssertionError((cwd, arguments))

    with pytest.raises(
        ValueError,
        match=(
            r"issue outside the evaluated population: proj.closed; mappings require an active "
            r"included issue \(check --status and --include-epics"
        ),
    ):
        analyze_code_surfaces(
            [IssueRecord(id="proj.active", title="Included")],
            workspace_path=root,
            worktree_mappings={"proj.closed": worktree},
            runner=runner,
        )


def test_short_bead_forms_associate_worktrees(tmp_path: Path) -> None:
    dashed = _Worktree(tmp_path / "dashed", "head-1", "cfg/abc12-47-call-doors")
    dotted = _Worktree(tmp_path / "dotted", "head-2", "lane/xyz99.3-gate-block")
    unrelated = _Worktree(tmp_path / "unrelated", "head-3", "lane/r1-rest-before")

    associations = _associate_worktrees(
        # The epic's short form also appears in the dashed branch; the child is the work.
        ("proj-abc12", "proj-abc12.47", "proj-xyz99.3"),
        (dashed, dotted, unrelated),
        {},
    )

    assert associations == {"proj-abc12.47": dashed, "proj-xyz99.3": dotted}


def test_branch_naming_two_unrelated_issues_stays_unassociated(tmp_path: Path) -> None:
    shared = _Worktree(tmp_path / "shared", "head", "cfg/abc12-1-2-xyz99")

    assert _associate_worktrees(("proj-abc12.1.2", "proj-xyz99"), (shared,), {}) == {}


def test_branch_pattern_reads_ids_the_default_segmentation_misses(tmp_path: Path) -> None:
    worktree = _Worktree(tmp_path / "custom", "head", "topicXabc12.47")
    patterns = compile_branch_patterns([r"X(?P<id>[a-z0-9.]+)$"])

    assert _associate_worktrees(("proj-abc12.47",), (worktree,), {}) == {}
    assert _associate_worktrees(("proj-abc12.47",), (worktree,), {}, patterns) == {
        "proj-abc12.47": worktree
    }
    assert _associate_worktrees(
        ("proj-abc12.47",), (worktree,), {}, compile_branch_patterns([r"X([a-z0-9.]+)$"])
    ) == {"proj-abc12.47": worktree}


def test_branch_pattern_must_compile_and_capture_an_id() -> None:
    with pytest.raises(ValueError, match="must capture"):
        compile_branch_patterns([r"lane/.*"])
    with pytest.raises(ValueError, match="invalid --branch-pattern"):
        compile_branch_patterns([r"lane/(?P<id>"])


def _shared_module_runner(root: Path, first: Path, second: Path):
    def runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = tuple(arguments)
        responses = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "HEAD"): "root-head\n",
            ("rev-parse", "--verify", "origin/main"): "base-head\n",
            ("rev-parse", "origin/main"): "base-head\n",
        }
        if command in responses:
            return completed(responses[command])
        if command == ("worktree", "list", "--porcelain"):
            return completed(
                f"worktree {root}\nHEAD root-head\nbranch refs/heads/main\n\n"
                f"worktree {first}\nHEAD head-one\nbranch refs/heads/bead-1\n\n"
                f"worktree {second}\nHEAD head-two\nbranch refs/heads/bead-2\n"
            )
        if command[:2] == ("diff", "--name-only"):
            changed = {
                first.resolve(): "src/parser/left.py\0",
                second.resolve(): "src/parser/right.py\0",
            }
            return completed(changed.get(cwd.resolve(), ""))
        if command == ("ls-files", "--others", "--exclude-standard", "-z"):
            return completed()
        raise AssertionError((cwd, arguments))

    return runner


@pytest.mark.parametrize(
    ("floor", "kept", "omitted"),
    [("explicit", 3, 0), ("corroborated", 3, 0), ("observed", 1, 2)],
)
def test_min_confidence_filters_and_counts_weaker_leads(
    tmp_path: Path, floor: str, kept: int, omitted: int
) -> None:
    root, first, second = tmp_path / "repo", tmp_path / "worktree-1", tmp_path / "worktree-2"
    for path in (root, first, second):
        path.mkdir()

    analysis = analyze_code_surfaces(
        [
            IssueRecord(id="proj.1", title="First"),
            IssueRecord(id="proj.2", title="Second"),
            IssueRecord(id="proj.3", title="Plan src/parser/third.py"),
        ],
        workspace_path=root,
        runner=_shared_module_runner(root, first, second),
        min_confidence=floor,
    )

    assert len(analysis.collisions) == kept
    assert analysis.pairs_omitted_by_confidence_filter == omitted
    assert analysis.min_confidence == floor
    if floor == "observed":
        assert {item.confidence for item in analysis.collisions} == {"observed"}


def test_min_confidence_rejects_unknown_levels(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="min_confidence"):
        analyze_code_surfaces([], workspace_path=None, min_confidence="certain")
