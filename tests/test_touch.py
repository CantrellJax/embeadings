import json
import subprocess

import pytest

from embead import cli
from embead.beads import BeadsAdapter, BeadsError
from embead.config import agent_note_prefixes
from embead.models import IssueComment, IssueRecord, WorkspaceSnapshot
from embead.provider import HashingProvider
from embead.touch import DEFAULT_AGENT_NOTE_PREFIXES, last_human_touch, note_entries


class FakeRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv):
        args = list(argv)
        self.calls.append(args)
        returncode, payload, stderr = self.responses.pop(0)
        return subprocess.CompletedProcess(args, returncode, json.dumps(payload), stderr)


def _record(**fields):
    return IssueRecord(
        id=fields.pop("id", "demo-1"),
        title="Synthetic",
        status="open",
        created_at=fields.pop("created_at", "2026-07-01T09:00:00Z"),
        updated_at=fields.pop("updated_at", "2026-10-21T03:00:00Z"),
        **fields,
    )


def test_agent_notes_do_not_count_as_a_human_touch() -> None:
    record = _record(
        notes=(
            "2026-07-20 reviewed with design; scope holds\n"
            "NIGHT TRIAGE: P1 -> P2, untouched since 2026-07-20\n"
            "2026-10-20 OWNERSHIP SWEEP: owner unchanged\n"
            "URGENCY REGRADE (owner 2026-10-21): P2 -> P3"
        )
    )

    assert last_human_touch(record) == ("2026-07-20T00:00:00Z", "note")


def test_comments_and_custom_prefixes_and_fallback_to_created() -> None:
    record = _record(
        notes="BOT PASS: 2026-10-01 relabelled",
        comments=(
            IssueComment("2026-08-02T10:15:00Z", "Confirmed with the owner."),
            IssueComment("2026-10-20T01:00:00Z", "NIGHT TRIAGE: still stale"),
        ),
    )

    assert last_human_touch(record) == ("2026-10-01T00:00:00Z", "note")
    assert last_human_touch(record, ("BOT PASS:", "NIGHT TRIAGE:")) == (
        "2026-08-02T10:15:00Z",
        "comment",
    )
    assert last_human_touch(_record(notes="NIGHT TRIAGE: 2026-10-20")) == (
        "2026-07-01T09:00:00Z",
        "created",
    )
    assert last_human_touch(IssueRecord(id="x", title="x")) == (None, "unknown")


def test_note_dates_outside_the_records_life_are_references_not_touches() -> None:
    record = _record(
        notes=(
            "Blocked until 2026-12-01 launch\n"
            "Same root cause as the 2026-03-02 incident\n"
            "Plan: ship after the review\n"
            "  2026-09-30 continuation of the plan line, not its own entry"
        )
    )

    assert last_human_touch(record) == ("2026-07-01T09:00:00Z", "created")
    assert note_entries("a\n  b\n- c\nd") == ["a\n  b\n- c", "d"]


def test_config_file_supplies_default_prefixes_and_flags_replace_them(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / "config.toml"
    monkeypatch.setenv("EMBEAD_CONFIG", str(config))
    assert agent_note_prefixes(None) == DEFAULT_AGENT_NOTE_PREFIXES

    config.write_text('[triage]\nagent_note_prefixes = ["SWEEP:"]\n')
    assert agent_note_prefixes(None) == ("SWEEP:",)
    assert agent_note_prefixes(["BOT:"]) == ("BOT:",)

    config.write_text('[triage]\nagent_note_prefixes = "SWEEP:"\n')
    with pytest.raises(ValueError, match="list of strings"):
        agent_note_prefixes(None)


def test_list_parses_created_at_comment_count_and_inline_comments() -> None:
    runner = FakeRunner(
        [
            (
                0,
                [
                    {
                        "id": "bd-1",
                        "title": "A",
                        "status": "open",
                        "created_at": "2026-07-01T00:00:00Z",
                        "comment_count": 1,
                        "comments": [
                            {"created_at": "2026-07-02T00:00:00Z", "text": "hi", "author": "a"}
                        ],
                    }
                ],
                "",
            )
        ]
    )

    record = BeadsAdapter(runner=runner).list_issues()[0]

    assert record.created_at == "2026-07-01T00:00:00Z"
    assert record.comment_count == 1
    assert record.comments == (IssueComment("2026-07-02T00:00:00Z", "hi", "a"),)


def test_comments_reader_is_read_only_and_refuses_subcommands() -> None:
    runner = FakeRunner([(0, [{"created_at": "2026-07-02T00:00:00Z", "text": "hi"}], "")])
    adapter = BeadsAdapter(runner=runner)

    assert adapter.comments("bd-1.2") == (IssueComment("2026-07-02T00:00:00Z", "hi"),)
    assert runner.calls == [["bd", "--readonly", "comments", "bd-1.2", "--json"]]
    for unsafe in ("add", "--json", "bd-1 add", ""):
        with pytest.raises(BeadsError, match="unsafe issue ID"):
            adapter.comments(unsafe)


TOUCH_ISSUES = (
    IssueRecord(
        id="demo-1",
        title="Persist authentication tokens",
        status="open",
        created_at="2026-07-01T00:00:00Z",
        updated_at="2026-10-21T00:00:00Z",
        comment_count=0,
    ),
    IssueRecord(
        id="demo-2",
        title="Persist authentication tokens again",
        status="open",
        notes="NIGHT TRIAGE: untouched",
        created_at="2026-07-03T00:00:00Z",
        updated_at="2026-10-21T00:00:00Z",
        comment_count=2,
    ),
)


class TouchAdapter:
    reads: list[str] = []

    def load(self):
        return WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"), TOUCH_ISSUES

    def comments(self, issue_id):
        TouchAdapter.reads.append(issue_id)
        return (
            IssueComment("2026-08-09T12:00:00Z", "Owner says keep this."),
            IssueComment("2026-10-20T00:00:00Z", "night triage: bump"),
        )


def test_neighbors_report_last_human_touch_and_fetch_comments_on_request(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(cli, "BeadsAdapter", TouchAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )
    TouchAdapter.reads = []

    assert cli.main(["neighbors", "demo-1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["neighbors"][0]["last_human_touch"] == "2026-07-03T00:00:00Z"
    assert TouchAdapter.reads == []

    assert cli.main(["neighbors", "demo-1", "--json", "--fetch-comments"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["neighbors"][0]["last_human_touch"] == "2026-08-09T12:00:00Z"
    # demo-1 is the seed and reports no comments; only the neighbor is read.
    assert TouchAdapter.reads == ["demo-2"]
    assert payload["filters"]["fetch_comments"] is True

    assert (
        cli.main(["neighbors", "demo-1", "--json", "--fetch-comments", "--agent-note-prefix", "X"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["neighbors"][0]["last_human_touch"] == "2026-10-20T00:00:00Z"
    assert payload["filters"]["agent_note_prefixes"] == ["X"]
