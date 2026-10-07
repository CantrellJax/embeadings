import json
import subprocess
from pathlib import Path

import pytest

from embead import cli
from embead.identifiers import IssueIdResolver
from embead.models import IssueRecord, WorkspaceSnapshot
from embead.provider import HashingProvider
from embead.superseded import (
    Change,
    changes_from_file,
    changes_from_git,
    code_identifiers,
    git_since_arguments,
    named_records,
    superseded_candidates,
)

SQUASH_BODY = (
    "* [proj-fix1] Validation reads across the edge\n\n"
    "The server reads the neighbouring month.\n\n"
    "* [proj-fix1] Picker reads the validator rows (review M1)\n\n"
    "The picker hands the condition context the scenario rows plus the reach."
)


def test_git_since_accepts_dates_timestamps_and_commits() -> None:
    assert git_since_arguments("2026-10-07", "main") == ["--since=2026-10-07T00:00:00", "main"]
    assert git_since_arguments("2026-10-07T12:00:00Z", "HEAD") == [
        "--since=2026-10-07T12:00:00Z",
        "HEAD",
    ]
    assert git_since_arguments("a07f7f5c0c", "HEAD") == ["a07f7f5c0c..HEAD"]
    with pytest.raises(ValueError):
        git_since_arguments("--output=/tmp/x", "HEAD")
    with pytest.raises(ValueError):
        git_since_arguments("2026-13-40", "HEAD")


def test_changes_from_git_reads_squash_and_merge_commits() -> None:
    record = "\x1e{}\x1f2026-10-07T15:15:15-07:00\x1f{}\x1f{}\x1f\n\n{}\n"
    stdout = record.format(
        "a" * 40,
        "[proj-fix1] Att validation reads across the edge (#5495)",
        SQUASH_BODY,
        "src/lib/att-scenario-validation.ts\nsrc/hooks/use-att-condition-reach-query.ts",
    ) + record.format(
        "b" * 40, "Merge pull request #12 from me/proj-x6g06-hide", "Hide inactive\nbody", "a.py"
    )
    seen = []

    def runner(cwd, arguments):
        seen.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    first, second = changes_from_git(Path("."), "2026-10-07", ref="main", runner=runner)

    assert seen[0][:4] == ["log", "--first-parent", "-m", "--name-only"]
    assert seen[0][-3:] == ["--since=2026-10-07T00:00:00", "main", "--"]
    assert (first.change_id, first.pr_number) == ("#5495", 5495)
    assert first.paths == (
        "src/lib/att-scenario-validation.ts",
        "src/hooks/use-att-condition-reach-query.ts",
    )
    assert [label for label, _ in first.facets()] == [
        None,
        "[proj-fix1] Validation reads across the edge",
        "[proj-fix1] Picker reads the validator rows (review M1)",
    ]
    assert (second.change_id, second.title, second.branch) == (
        "#12",
        "Hide inactive",
        "me/proj-x6g06-hide",
    )


def test_changes_from_file_reads_gh_shaped_jsonl(tmp_path) -> None:
    path = tmp_path / "prs.jsonl"
    path.write_text(
        json.dumps(
            {
                "number": 7,
                "title": "Fix picker",
                "body": "",
                "files": [{"path": "src/picker.ts"}],
                "mergedAt": "2026-10-07T10:00:00Z",
            }
        )
        + "\n\n"
    )

    (change,) = changes_from_file(path)

    assert (change.change_id, change.paths, change.merged_at) == (
        "#7",
        ("src/picker.ts",),
        "2026-10-07T10:00:00Z",
    )
    path.write_text('{"body": "no title"}\n')
    with pytest.raises(ValueError, match="needs a string title"):
        changes_from_file(path)


def test_code_identifiers_keep_names_stems_and_routes() -> None:
    found = code_identifiers(
        "attRulesReadNeighbourWork replaces save_att_rule on /att/admin/schedule. "
        "Co-Authored-By: someone",
        ["src/lib/att-scenario-validation.ts", "src/app/page.tsx"],
    )

    assert {
        "attrulesreadneighbourwork",
        "save_att_rule",
        "/att/admin/schedule",
        "att-scenario-validation",
    } <= found
    assert "page" not in found
    assert "co-authored-by" not in found


def test_named_records_resolve_title_and_branch_spellings() -> None:
    resolver = IssueIdResolver(["proj-x6g06", "proj-fix1", "proj-other"])
    change = Change("#1", "[proj-fix1] Fix", "see fix1 later", branch="me/x6g06-hide")

    assert named_records(change, resolver) == {"proj-fix1", "proj-x6g06"}


def _issues():
    return (
        IssueRecord(
            id="proj-fix1",
            title="Validation edge",
            status="closed",
            updated_at="2026-10-07T22:20:00Z",
        ),
        IssueRecord(
            id="proj-picker",
            title="Picker reads the validator rows",
            description="The picker hands the condition context the scenario rows plus the reach",
            status="open",
            labels=("owner-run",),
        ),
        IssueRecord(id="proj-other", title="Unrelated billing export", status="open"),
        IssueRecord(id="proj-epic", title="Picker epic", status="open", issue_type="epic"),
        IssueRecord(
            id="proj-late",
            title="Picker validator rows",
            status="closed",
            updated_at="2026-10-07T23:00:00Z",
        ),
        IssueRecord(
            id="proj-old",
            title="Picker validator rows",
            status="closed",
            updated_at="2026-10-01T23:00:00Z",
        ),
    )


def test_squashed_facets_rank_on_their_own() -> None:
    provider = HashingProvider(dimension=64)
    issues = _issues()[1:3]
    change = Change("#5495", "[proj-fix1] Validation edge (#5495)", SQUASH_BODY)
    vectors = dict(
        zip(
            (issue.id for issue in issues),
            provider.encode([cli.canonical_text(issue) for issue in issues]),
            strict=True,
        )
    )
    facets = [provider.encode([text for _label, text in change.facets()])]

    rows = superseded_candidates(
        [change], facets, issues, vectors, IssueIdResolver(["proj-fix1"]), per_change=1
    )

    assert [row["issue"].id for row in rows] == ["proj-picker"]
    evidence = rows[0]["evidence"]
    assert evidence["facet"] == "[proj-fix1] Picker reads the validator rows (review M1)"
    assert evidence["rank_in_change"] == 1
    assert evidence["lead"] > 0


class SupersededAdapter:
    def load(self):
        return WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"), _issues()


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "BeadsAdapter", SupersededAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=64))
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )


def test_superseded_cli_separates_named_and_similar(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    changes = tmp_path / "changes.jsonl"
    changes.write_text(
        json.dumps(
            {
                "number": 5495,
                "title": "[proj-fix1] Validation edge",
                "body": SQUASH_BODY,
                "files": ["src/picker.ts"],
                "mergedAt": "2026-10-07T22:15:00Z",
            }
        )
        + "\n"
    )

    assert (
        cli.main(["superseded", "--since", "2026-10-07", "--changes-file", str(changes), "--json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["report_type"] == "superseded"
    assert payload["policy"]["tracker_mutation_allowed"] is False
    # The named record is closed and out of scope; epics are excluded by default.
    assert payload["named"] == []
    similar = {row["id"]: row for row in payload["similar"]}
    assert set(similar) == {"proj-picker", "proj-other"}
    assert similar["proj-picker"]["guards"] == ["label:owner-run"]
    assert payload["summary"]["records_compared"] == 2

    assert (
        cli.main(
            [
                "superseded",
                "--since",
                "2026-10-07",
                "--changes-file",
                str(changes),
                "--include-closed-since",
                "--include-epics",
            ]
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert "## Named by a merged change and still in scope" in rendered
    assert "| proj-fix1 | closed |" in rendered
    assert "proj-late" in rendered
    assert "proj-epic" in rendered
    assert "proj-old" not in rendered


def test_superseded_rejects_bad_limits(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["superseded", "--since", "2026-10-07", "--limit", "0"]) == 2
    assert "--limit and --per-change" in capsys.readouterr().err


def test_superseded_cli_output_matches_the_schema(monkeypatch, tmp_path, capsys) -> None:
    from jsonschema import Draft202012Validator

    _configure(monkeypatch, tmp_path)
    changes = tmp_path / "changes.jsonl"
    changes.write_text(json.dumps({"number": 1, "title": "[proj-fix1] Picker", "body": ""}) + "\n")
    assert (
        cli.main(["superseded", "--since", "2026-10-07", "--changes-file", str(changes), "--json"])
        == 0
    )
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/v1/superseded.schema.json").read_text()
    )
    payload = json.loads(capsys.readouterr().out)
    payload["snapshot"]["tracker_version"] = "1.0.5"  # the fake adapter leaves it blank
    Draft202012Validator(schema).validate(payload)
