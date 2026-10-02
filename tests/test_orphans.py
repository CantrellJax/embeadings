import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from embead import cli
from embead.models import IssueRecord, WorkspaceSnapshot
from embead.orphans import find_dangling_parents, find_parentless

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas" / "v1"


def _issue(issue_id, status="open", parent=None, issue_type="task", **extra):
    return IssueRecord(
        id=issue_id,
        title=f"Synthetic {issue_id}",
        status=status,
        issue_type=issue_type,
        priority=2,
        parent_id=parent,
        **extra,
    )


ISSUES = (
    _issue("demo-1", "closed", issue_type="epic"),
    _issue("demo-2", "deferred", issue_type="epic"),
    _issue("demo-3", "in_progress", issue_type="epic"),
    _issue("demo-4", "blocked", issue_type="epic"),
    _issue("demo-5", "open", issue_type="epic"),
    _issue("demo-10", "open", parent="demo-1"),  # closed parent: flagged
    _issue("demo-11", "blocked", parent="demo-404", issue_type="bug"),  # missing parent: flagged
    _issue("demo-12", "open", parent="demo-2"),  # deferred parent: live
    _issue("demo-13", "open", parent="demo-3"),  # in_progress parent: live
    _issue("demo-14", "open", parent="demo-4"),  # blocked parent: live
    _issue("demo-15", "open", parent="demo-5"),  # open parent: live
    _issue("demo-16", "closed", parent="demo-1"),  # closed child: never flagged
    _issue("demo-17", "deferred", parent="demo-1"),  # deferred child of closed: flagged
    _issue("demo-18", "open", parent="demo-10"),  # parent is live (orphaned itself): not repeated
    _issue("demo-19", "closed", parent="demo-404"),  # closed child of missing parent
    _issue("demo-20", "open", parent="demo-1", ephemeral=True),
)


class Adapter:
    def load(self):
        return WorkspaceSnapshot(
            "workspace-test", "1.0.5", "/tmp/demo/.beads", tracker_version="1.0.5"
        ), ISSUES


@pytest.fixture
def run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "BeadsAdapter", Adapter)
    monkeypatch.setattr(cli, "_provider", lambda _n: pytest.fail("orphans must not load a model"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def invoke(*argv):
        assert cli.main(["orphans", "--json", *argv]) == 0
        return json.loads(capsys.readouterr().out)

    return invoke


def test_closed_and_missing_parents_are_flagged_in_deterministic_order() -> None:
    rows = find_dangling_parents(ISSUES)
    assert [(r.parent_id, r.issue_id, r.parent_status) for r in rows] == [
        ("demo-1", "demo-10", "closed"),
        ("demo-1", "demo-17", "closed"),
        ("demo-1", "demo-20", "closed"),
        ("demo-404", "demo-11", "missing"),
    ]
    assert find_dangling_parents(tuple(reversed(ISSUES))) == rows


@pytest.mark.parametrize("child", ["demo-12", "demo-13", "demo-14", "demo-15"])
def test_live_parents_are_not_orphan_parents(child: str) -> None:
    assert child not in {r.issue_id for r in find_dangling_parents(ISSUES)}


def test_closed_issues_and_orphaned_parents_children_are_not_reported() -> None:
    flagged = {r.issue_id for r in find_dangling_parents(ISSUES)}
    assert "demo-16" not in flagged and "demo-19" not in flagged
    assert "demo-18" not in flagged  # nearest broken link is demo-10's, reported once


def test_parentless_is_live_only_and_grouped_by_type() -> None:
    rows = find_parentless(ISSUES)
    assert [r.issue_id for r in rows] == ["demo-2", "demo-3", "demo-4", "demo-5"]
    assert "demo-1" not in {r.issue_id for r in rows}  # closed


def test_cli_excludes_parentless_by_default_and_ephemeral_children(run) -> None:
    payload = run()
    assert [r["issue_id"] for r in payload["dangling_parent"]] == ["demo-10", "demo-17", "demo-11"]
    assert payload["parentless"] == []
    assert payload["summary"]["parentless_count"] == 0
    assert payload["summary"]["dangling_parent_count"] == 3
    assert payload["summary"]["parent_status_counts"] == {"closed": 2, "missing": 1}
    assert payload["filters"]["include_parentless"] is False
    assert payload["dangling_parent"][0] == {
        "issue_id": "demo-10",
        "issue_type": "task",
        "status": "open",
        "priority": 2,
        "title": "Synthetic demo-10",
        "parent_id": "demo-1",
        "parent_status": "closed",
    }
    # Ephemeral records stay visible as parents but opt in as children.
    assert "demo-20" in {r["issue_id"] for r in run("--include-ephemeral")["dangling_parent"]}


def test_cli_include_parentless_is_a_separate_grouped_section(run) -> None:
    payload = run("--include-parentless")
    assert [r["issue_id"] for r in payload["dangling_parent"]] == ["demo-10", "demo-17", "demo-11"]
    assert [(g["issue_type"], g["count"]) for g in payload["parentless"]] == [("epic", 4)]
    assert [i["issue_id"] for i in payload["parentless"][0]["issues"]] == [
        "demo-2",
        "demo-3",
        "demo-4",
        "demo-5",
    ]
    assert payload["summary"]["parentless_count"] == 4


def test_cli_report_is_read_only_and_schema_valid(run) -> None:
    payload = run("--include-parentless")
    assert payload["report_type"] == "orphans"
    assert payload["policy"]["read_only"] is True
    assert payload["policy"]["tracker_mutation_allowed"] is False
    assert payload["policy"]["snippets_included"] is False
    schema = json.loads((SCHEMAS / "orphans.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)


def test_cli_markdown_and_output_file(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli, "BeadsAdapter", Adapter)
    monkeypatch.setattr(cli, "_provider", lambda _n: pytest.fail("orphans must not load a model"))
    target = tmp_path / "orphans.md"
    assert cli.main(["orphans", "--include-parentless", "--output", str(target)]) == 0
    rendered = capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == rendered
    assert "- Dangling parent: 3 (closed 2, missing 1)" in rendered
    assert "| demo-1 | closed | demo-10 | task | open | P2 | Synthetic demo-10 |" in rendered
    assert "### epic (4)" in rendered
