import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from embead import cli
from embead.models import DependencyLink, IssueComment, IssueRecord, WorkspaceSnapshot
from embead.packets import clip, compact, relation_counts
from embead.provider import HashingProvider

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas" / "v1"
AUTH = "Persist authentication tokens so users stay signed in across browser restarts"


def _issue(identifier, title, status="open", **fields):
    fields.setdefault("created_at", "2026-07-01T00:00:00Z")
    fields.setdefault("updated_at", "2026-10-20T00:00:00Z")
    fields.setdefault("comment_count", 0)
    return IssueRecord(id=identifier, title=title, status=status, priority=2, **fields)


ISSUES = (
    _issue("demo-epic", "Account work", "closed", issue_type="epic"),
    _issue(
        "demo-1",
        "Persist authentication tokens",
        description=AUTH,
        parent_id="demo-epic",
        notes="2026-08-01 scoped with design\nNIGHT TRIAGE: P1 -> P2",
        labels=("auth",),
        acceptance_criteria="Tokens survive a restart. " * 40,
        dependency_links=(
            DependencyLink("demo-1", "demo-epic", "parent-child"),
            DependencyLink("demo-1", "demo-9", "blocks"),
        ),
    ),
    _issue(
        "demo-2",
        "Persist authentication tokens (copy)",
        "closed",
        description=AUTH,
        close_reason="Duplicate of demo-1",
    ),
    _issue(
        "demo-3",
        "Persist authentication tokens in secure storage",
        "in_progress",
        description=AUTH,
        assignee="dev-a",
        comment_count=1,
    ),
    _issue("demo-4", "Lazy load gallery images", description="Defer thumbnails until visible"),
    _issue("demo-5", "Retire legacy session cookie", description="Remove the old cookie path"),
    _issue("demo-6", "Old gallery spike", "closed", description="Defer thumbnails until visible"),
    _issue(
        "demo-7",
        "Gallery placeholder art",
        description="Defer thumbnails until visible. Folded into demo-4.",
    ),
    _issue("demo-9", "Token storage API", description="Expose storage for tokens"),
    _issue("demo-10", "Cookie banner copy", "deferred", description="Banner text"),
    _issue("demo-11", "Cookie banner tests", description="Test the banner", parent_id="demo-10"),
)


class Adapter:
    comment_reads: list[str] = []

    def load(self):
        return WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"), ISSUES

    def comments(self, issue_id):
        Adapter.comment_reads.append(issue_id)
        return (IssueComment("2026-09-15T08:00:00Z", "Halfway through the storage swap."),)


@pytest.fixture
def run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "BeadsAdapter", Adapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=64))
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )
    loads = []
    original = cli._load_vectors
    monkeypatch.setattr(cli, "_load_vectors", lambda *a, **k: loads.append(1) or original(*a, **k))
    Adapter.comment_reads = []

    def invoke(*argv):
        output = tmp_path / "packets.jsonl"
        assert cli.main(["packets", *argv, "--output", str(output)]) == 0
        summary = capsys.readouterr().out
        packets = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        return {packet["id"]: packet for packet in packets}, summary, loads

    return invoke


def test_packets_answer_every_triage_question_from_one_load(run, tmp_path) -> None:
    ids = tmp_path / "seeds.txt"
    ids.write_text("demo-1\ndemo-4  # gallery\ndemo-11\n")
    changes = tmp_path / "changes.jsonl"
    changes.write_text(
        json.dumps({"number": 7, "title": "Lazy load gallery images (demo-4)", "body": ""})
        + "\n"
        + json.dumps({"number": 8, "title": "Persist authentication tokens", "body": AUTH})
        + "\n"
    )

    packets, summary, loads = run(
        "--ids-file",
        str(ids),
        "--include-closed",
        "--since",
        "2026-10-01",
        "--changes-file",
        str(changes),
        "--text-limit",
        "60",
    )

    assert loads == [1]
    assert summary.startswith("Wrote 3 packets")
    auth = packets["demo-1"]
    assert auth["report_type"] == "packet"
    assert auth["last_human_touch"] == "2026-08-01T00:00:00Z"
    assert auth["last_human_touch_source"] == "note"
    assert auth["parent"] == "demo-epic" and auth["parent_status"] == "closed"
    assert auth["flags"] == ["parent-closed"]
    assert auth["counts"] == {"dependencies": 1}
    assert len(auth["acceptance"]) <= 60 and auth["acceptance"].endswith("…")
    neighbors = {row["id"]: row for row in auth["neighbors"]}
    assert neighbors["demo-2"]["close_reason"] == "Duplicate of demo-1"
    assert neighbors["demo-2"]["duplicate_direction"] == "neighbor_dup_of_seed"
    assert all(row["similarity"] >= auth["score_baseline"]["p90"] for row in auth["neighbors"])
    assert auth["inflight_neighbors"] == [
        {
            "id": "demo-3",
            "title": "Persist authentication tokens in secure storage",
            "status": "in_progress",
            "holder": "dev-a",
            "similarity": auth["inflight_neighbors"][0]["similarity"],
            "rank": auth["inflight_neighbors"][0]["rank"],
            "reverse_rank": auth["inflight_neighbors"][0]["reverse_rank"],
            "last_human_touch": "2026-07-01T00:00:00Z",
        }
    ]
    assert auth["superseded"]["similar"][0]["change_id"] == "#8"
    assert "named" not in auth["superseded"]
    assert {claim["issue_id"] for claim in auth["claims"]} == {"demo-2"}

    gallery = packets["demo-4"]
    assert [change["change_id"] for change in gallery["superseded"]["named"]] == ["#7"]
    assert gallery["claims"] == [
        {
            "issue_id": "demo-7",
            "kind": "absorbed-by",
            "related_issue_id": "demo-4",
            "other_status": "open",
            "typed_link": False,
        }
    ]
    assert packets["demo-11"]["flags"] == ["parent-deferred"]

    schema = json.loads((SCHEMAS / "packet.schema.json").read_text(encoding="utf-8"))
    for packet in packets.values():
        Draft202012Validator(schema).validate(packet)


def test_packets_skip_closed_seeds_and_closed_neighbors_by_default(run) -> None:
    packets, summary, _loads = run("demo-1", "demo-2")

    assert set(packets) == {"demo-1"}
    assert "skipped 1 closed seeds" in summary
    assert "demo-2" not in {row["id"] for row in packets["demo-1"]["neighbors"]}
    assert "superseded" not in packets["demo-1"]


def test_packets_fetch_comments_only_when_asked(run) -> None:
    packets, _summary, _loads = run("demo-1", "--fetch-comments")

    assert Adapter.comment_reads == ["demo-3"]
    assert packets["demo-1"]["inflight_neighbors"][0]["last_human_touch"] == (
        "2026-09-15T08:00:00Z"
    )


def test_packets_inflight_status_is_configurable(run) -> None:
    packets, _summary, _loads = run("demo-1", "--inflight-status", "blocked")

    assert packets["demo-1"]["inflight_neighbors"] == []


def test_packets_need_seeds_and_known_ids(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli, "BeadsAdapter", Adapter)
    assert cli.main(["packets"]) == 2
    assert "packets needs at least one ISSUE_ID" in capsys.readouterr().err
    assert cli.main(["packets", "demo-404"]) == 2
    assert "issue not found: demo-404" in capsys.readouterr().err


def test_packet_helpers() -> None:
    assert clip("a  b\n c", 10) == "a b c"
    assert clip("abcdefghij", 5) == "abcd…"
    assert compact({"id": "x", "neighbors": [], "notes": "", "priority": 0, "labels": []}) == {
        "id": "x",
        "neighbors": [],
        "priority": 0,
    }
    counts = relation_counts(ISSUES)
    assert counts["demo-9"] == {
        "dependencies": 0,
        "dependents": 1,
        "children": 0,
        "live_children": 0,
    }
    assert counts["demo-10"]["live_children"] == 1
