import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from embead import cli
from embead.beads import BeadsAdapter
from embead.match import candidate_content_hash
from embead.provider import HashingProvider

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "schemas" / "v1" / "match.schema.json").read_text(
        encoding="utf-8"
    )
)

RAW_ISSUES = [
    {
        "id": "demo-1",
        "title": "Persist authentication tokens",
        "description": "Keep users signed in across browser restarts",
        "status": "open",
        "issue_type": "task",
        "priority": 2,
        "parent_id": "demo-9",
    },
    {
        "id": "demo-2",
        "title": "Lazy load gallery images",
        "description": "Defer thumbnail loading until images are visible",
        "status": "closed",
        "issue_type": "feature",
        "priority": 3,
        "close_reason": "Shipped with the gallery rewrite",
    },
    {
        "id": "demo-3",
        "title": "Rotate signing keys quarterly",
        "description": "Schedule certificate renewal and key rotation",
        "status": "closed",
        "issue_type": "chore",
        "priority": 1,
        "parent_id": "demo-9",
    },
    {"id": "demo-9", "title": "Platform epic", "status": "open", "issue_type": "epic"},
]


class FakeBd:
    """Stands in for the bd binary and records every argv it is asked to run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        args = list(argv)
        self.calls.append(args)
        command = args[2]
        payload = {
            "version": {"version": "1.0.5"},
            "context": {"project_id": "workspace-match-test"},
            "list": RAW_ISSUES,
        }[command]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")


@pytest.fixture
def env(monkeypatch, tmp_path, capsys):
    bd = FakeBd()
    monkeypatch.setattr(cli, "BeadsAdapter", lambda: BeadsAdapter(runner=bd))
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=64))
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cli, "_workspace_paths", lambda _identity: (cache_dir, tmp_path / "state"))

    def invoke(*argv):
        assert cli.main(["match", "--json", *argv]) == 0
        payload = json.loads(capsys.readouterr().out)
        Draft202012Validator(SCHEMA).validate(payload)
        return payload

    invoke.bd = bd
    invoke.cache_dir = cache_dir
    return invoke


def test_candidate_matches_an_open_bead(env) -> None:
    payload = env("--title", "Persist authentication tokens across restarts")
    (candidate,) = payload["candidates"]
    assert candidate["status"] == "matches"
    assert candidate["no_match_reason"] is None
    top = candidate["neighbors"][0]
    assert top["issue_id"] == "demo-1"
    assert top["status"] == "open"
    assert top["is_closed"] is False
    assert top["resolution_evidence"] is None
    assert top["parent_id"] == "demo-9"
    assert top["parent_status"] == "live"
    assert top["rank"] == 1
    assert {"issue_type", "priority", "title", "similarity"} <= set(top)


def test_candidate_matches_a_closed_bead_and_carries_closed_status(env) -> None:
    payload = env("--title", "Lazy load gallery images", "--body", "Defer thumbnail loading")
    top = payload["candidates"][0]["neighbors"][0]
    assert top["issue_id"] == "demo-2"
    assert top["status"] == "closed"
    assert top["is_closed"] is True
    assert top["resolution_evidence"] == {
        "kind": "close_reason",
        "text": "Shipped with the gallery rewrite",
    }
    assert payload["filters"]["include_closed"] is True


def test_closed_without_a_close_reason_has_null_evidence_and_no_closed_flag_excludes(env) -> None:
    payload = env("--title", "Rotate signing keys quarterly")
    top = payload["candidates"][0]["neighbors"][0]
    assert (top["issue_id"], top["is_closed"], top["resolution_evidence"]) == (
        "demo-3",
        True,
        None,
    )
    excluded = env("--title", "Rotate signing keys quarterly", "--no-include-closed")
    ids = [row["issue_id"] for row in excluded["candidates"][0]["neighbors"]]
    assert "demo-3" not in ids and "demo-2" not in ids
    assert excluded["filters"]["include_closed"] is False


def test_no_match_reports_a_clear_status(env) -> None:
    payload = env("--title", "Rotate signing keys quarterly", "--min-similarity", "0.999999")
    (candidate,) = payload["candidates"]
    assert candidate["status"] == "no-match"
    assert candidate["no_match_reason"] == "below-min-similarity"
    assert candidate["neighbors"] == []
    assert candidate["records_compared"] == 4
    assert payload["summary"]["candidates_with_matches"] == 0
    assert payload["filters"]["min_similarity"] == 0.999999


def test_two_candidates_in_one_jsonl_share_one_tracker_load(env, tmp_path) -> None:
    path = tmp_path / "candidates.jsonl"
    path.write_text(
        json.dumps({"candidate_id": "c-b", "title": "Lazy load gallery images"})
        + "\n\n"
        + json.dumps(
            {
                "candidate_id": "c-a",
                "title": "Persist authentication tokens",
                "body": "Stay signed in",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = env("--candidates-file", str(path))
    assert [c["candidate_id"] for c in payload["candidates"]] == ["c-b", "c-a"]
    assert payload["candidates"][0]["neighbors"][0]["issue_id"] == "demo-2"
    assert payload["candidates"][1]["neighbors"][0]["issue_id"] == "demo-1"
    assert payload["summary"]["candidate_count"] == 2
    assert [call[2] for call in env.bd.calls].count("list") == 1


def test_candidate_id_and_hash_are_echoed_and_stable(env, tmp_path) -> None:
    first = env("--title", "T", "--body", "B", "--candidate-id", "intake-7")
    second = env("--title", "T", "--body", "B", "--candidate-id", "intake-7")
    changed = env("--title", "T", "--body", "B2", "--candidate-id", "intake-7")
    a, b, c = (p["candidates"][0] for p in (first, second, changed))
    assert a["candidate_id"] == "intake-7"
    assert a["content_hash"] == b["content_hash"] == candidate_content_hash("T", "B")
    assert c["content_hash"] != a["content_hash"]
    # The same payload through every input mode hashes identically.
    body_file = tmp_path / "body.txt"
    body_file.write_text("B", encoding="utf-8")
    json_file = tmp_path / "c.json"
    json_file.write_text(json.dumps({"title": "T", "body": "B"}), encoding="utf-8")
    via_file = env("--title", "T", "--body-file", str(body_file))["candidates"][0]
    via_json = env("--candidate-file", str(json_file))["candidates"][0]
    assert via_file["content_hash"] == via_json["content_hash"] == a["content_hash"]
    assert via_json["candidate_id"] == "candidate-1"


def test_policy_is_read_only_and_states_the_retrieval_lead_wording(env) -> None:
    payload = env("--title", "Persist authentication tokens")
    policy = payload["policy"]
    assert policy["read_only"] is True
    assert policy["tracker_mutation_allowed"] is False
    assert policy["advisory"] is True
    assert policy["creates_records"] is False
    assert policy["snippets_included"] is False
    assert payload["report_type"] == "match"
    notice = payload["candidates"][0]["notice"]
    assert "retrieval lead, not a duplicate verdict" in notice


def test_match_creates_no_record_and_never_caches_candidate_text(env, tmp_path) -> None:
    secret = "zebra-quartz-candidate-phrase"
    out = tmp_path / "out" / "match.json"
    out.parent.mkdir()
    env("--title", secret, "--body", secret, "--output", str(out))
    # Only read-only allowlisted bd calls were made.
    assert env.bd.calls
    assert all(call[:2] == ["bd", "--readonly"] for call in env.bd.calls)
    assert {call[2] for call in env.bd.calls} <= {"version", "context", "list"}
    # Candidate text is absent from the cache directory and from the report itself.
    for path in env.cache_dir.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8")
    assert secret not in out.read_text(encoding="utf-8")
    # Only the cache entries for the four existing records exist; no candidate vector.
    assert len([p for p in env.cache_dir.rglob("*.json")]) == len(RAW_ISSUES)


def test_ordering_is_similarity_then_issue_id_and_limit_applies(env) -> None:
    payload = env("--title", "x", "--limit", "3")
    rows = payload["candidates"][0]["neighbors"]
    assert len(rows) == 3
    keys = [(-row["similarity"], row["issue_id"]) for row in rows]
    assert keys == sorted(keys)
    assert [row["rank"] for row in rows] == [1, 2, 3]


def test_markdown_report_and_output_file(env, monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "match.md"
    assert cli.main(["match", "--title", "Lazy load gallery images", "--output", str(out)]) == 0
    text = capsys.readouterr().out
    assert text == out.read_text(encoding="utf-8")
    assert "retrieval lead, not a duplicate verdict" in text
    assert "demo-2" in text and "Shipped with the gallery rewrite" in text


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--body", "orphan body"],
        ["--title", "T", "--limit", "0"],
        ["--title", "T", "--body", "a", "--body-file", "b.txt"],
    ],
)
def test_bad_input_is_refused_before_touching_the_tracker(env, capsys, argv) -> None:
    assert cli.main(["match", *argv]) == 2
    assert "made no tracker changes" in capsys.readouterr().err
    assert env.bd.calls == []


def test_jsonl_requires_unique_candidate_ids(env, tmp_path, capsys) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text('{"title": "A"}\n', encoding="utf-8")
    assert cli.main(["match", "--candidates-file", str(path)]) == 2
    assert "line 1" in capsys.readouterr().err
    path.write_text(
        '{"candidate_id": "x", "title": "A"}\n{"candidate_id": "x", "title": "B"}\n',
        encoding="utf-8",
    )
    assert cli.main(["match", "--candidates-file", str(path)]) == 2
    assert "duplicate candidate_id: x" in capsys.readouterr().err
    assert env.bd.calls == []
