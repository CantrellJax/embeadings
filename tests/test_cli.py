import json
import os
import stat
from argparse import Namespace

import pytest

import embead.ranking as ranking_module
from embead import __version__, cli
from embead.models import DependencyLink, IssueRecord, WorkspaceSnapshot
from embead.provider import HashingProvider
from embead.ranking import CandidateRanking, LaneMetrics

ISSUES = (
    IssueRecord(
        id="demo-1",
        title="Persist authentication tokens",
        description="Keep users signed in across browser restarts",
        status="open",
    ),
    IssueRecord(
        id="demo-2",
        title="Remember login state",
        description="Store authentication tokens between sessions",
        status="closed",
    ),
    IssueRecord(
        id="demo-3",
        title="Lazy load gallery images",
        description="Defer thumbnail loading until images are visible",
        status="open",
    ),
)


class FakeAdapter:
    def load(self):
        return WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"), ISSUES


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_workspace_paths_restrict_cache_and_state_roots_on_posix(monkeypatch, tmp_path) -> None:
    cache_base = tmp_path / "cache" / "embeadings"
    state_base = tmp_path / "state" / "embeadings"
    monkeypatch.setattr(cli, "user_cache_path", lambda _name: cache_base)
    monkeypatch.setattr(cli, "user_state_path", lambda _name: state_base)

    cache_path, state_path = cli._workspace_paths("workspace-test")

    assert cache_path == cache_base / "vectors" / "workspace-test"
    assert state_path == state_base / "runs" / "workspace-test"
    assert stat.S_IMODE(cache_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "BeadsAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )


def test_readiness_is_corpus_free_and_machine_readable(monkeypatch, capsys) -> None:
    class FailingAdapter:
        def load(self):
            raise AssertionError("readiness must not load Beads records")

    monkeypatch.setattr(cli, "BeadsAdapter", FailingAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))

    assert cli.main(["readiness", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "corpus_loaded": False,
        "model_id": "hashing/32",
        "model_revision": "1",
        "network_policy": "prefetch-allowed",
        "readiness_version": 1,
        "status": "ready",
        "vector_dimension": 32,
    }


def test_readiness_offline_sets_huggingface_policy(monkeypatch, capsys) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=8))

    assert cli.main(["readiness", "--offline", "--json"]) == 0

    assert cli.os.environ["HF_HUB_OFFLINE"] == "1"
    assert json.loads(capsys.readouterr().out)["network_policy"] == "offline"


def test_capabilities_is_corpus_free_and_machine_readable(monkeypatch, capsys) -> None:
    class FailingAdapter:
        def load(self):
            raise AssertionError("capabilities must not load tracker records")

    monkeypatch.setattr(cli, "BeadsAdapter", FailingAdapter)

    assert cli.main(["capabilities", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "document_type": "embeadings-capabilities",
        "protocol_version": 1,
        "role": "producer",
        "schema_versions": [1],
        "report_types": [
            "neighbors",
            "neighbors-batch",
            "batch",
            "sweep",
            "triage",
            "collisions",
            "mentions",
            "orphans",
            "match",
        ],
        "capabilities": [
            "additive-fields",
            "advisory-evidence",
            "read-only-review",
            "code-surface-pointers",
        ],
        "required_capabilities": ["read-only-review"],
    }


def test_cli_version_uses_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out == f"embead {__version__}\n"


def test_explicit_echo_objective_emits_field_provenance(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    output = tmp_path / "objective-sweep"

    assert (
        cli.main(
            [
                "--provider",
                "hashing",
                "sweep",
                "--objective",
                "echo",
                "--semantic-view",
                "fields",
                "--echo-threshold",
                "-1",
                "--reciprocal-rank",
                "0",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    candidate = payload["candidates"][0]
    assert candidate["objective"] == "echo"
    assert payload["parameters"]["filters"]["review_objectives"] == ["echo"]
    assert payload["parameters"]["filters"]["semantic_view"] == "fields"
    assert candidate["retrieval_provenance"]["selection_rule"] == "max-semantic-view"
    assert {receipt["channel"] for receipt in candidate["retrieval_provenance"]["channels"]} >= {
        "whole-record-semantic",
        "field-semantic:title",
    }
    assert payload["cache"]["field_view_counts"]["title"] == len(ISSUES)
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert "Review objective: echo" in markdown
    assert "Retrieval provenance: max-semantic-view" in markdown


def test_closed_only_incremental_disposition_is_counted_without_leaking_reason(
    monkeypatch, tmp_path, capsys
) -> None:
    raw_close_reason = "Rehomed as demo-active; not completed."
    issues = (
        IssueRecord(
            id="demo-active",
            title="Coordinate cache invalidation",
            status="open",
            updated_at="2026-07-01T00:00:00Z",
        ),
        IssueRecord(
            id="demo-closed",
            title="Coordinate cache invalidation",
            status="closed",
            close_reason=raw_close_reason,
            updated_at="2026-07-14T00:00:00Z",
        ),
    )

    class CloseReasonAdapter:
        def load(self):
            return WorkspaceSnapshot("close-reason-test", "1.0.5", None), issues

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", CloseReasonAdapter)
    output = tmp_path / "close-reason-sweep"

    assert (
        cli.main(
            [
                "--provider",
                "hashing",
                "sweep",
                "--objective",
                "echo",
                "--echo-threshold",
                "-1",
                "--reciprocal-rank",
                "0",
                "--changed-since",
                "2026-07-10T00:00:00Z",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    json_report = (output / "report.json").read_text(encoding="utf-8")
    markdown = (output / "report.md").read_text(encoding="utf-8")
    assert payload["candidates"] == []
    assert payload["parameters"]["filters"]["incremental_scope"] == {
        "checkpoint_created_at": None,
        "changed_active_count": 0,
        "changed_closed_count": 1,
        "deleted_since_checkpoint_count": 0,
        "mode": "changed-since",
        "unchanged_active_count": 1,
        "unknown_timestamp_count": 0,
    }
    assert payload["parameters"]["candidate_policy"]["echo_disposition_omissions"] == {
        "rehomed-not-completed": 1
    }
    assert raw_close_reason not in json_report
    assert raw_close_reason not in markdown
    assert "`rehomed-not-completed`: 1" in markdown


def test_readiness_help_is_tracker_neutral(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "without reading tracker issues" in help_text
    assert "without reading Beads issues" not in help_text


def test_neighbors_json_stdout_is_machine_readable(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["neighbors", "demo-1", "--include-closed", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["report_type"] == "neighbors"
    assert payload["policy"]["read_only"] is True
    assert {item["id"] for item in payload["neighbors"]} == {"demo-2", "demo-3"}


LINKED_ISSUES = (
    IssueRecord(
        id="demo-1",
        title="Persist authentication tokens",
        description="Keep users signed in across browser restarts",
        status="open",
    ),
    IssueRecord(
        id="demo-2",
        title="Keep authentication tokens across restarts",
        description="Store signed-in authentication tokens between browser sessions",
        status="open",
        dependency_links=(DependencyLink("demo-2", "demo-1", "relates-to"),),
    ),
    IssueRecord(
        id="demo-3",
        title="Remember login state",
        description="Store authentication tokens between sessions",
        status="open",
    ),
)


class LinkedAdapter:
    def load(self):
        return (
            WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"),
            LINKED_ISSUES,
        )


def test_neighbors_orphans_only_keeps_unlinked_neighbors(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", LinkedAdapter)

    assert cli.main(["neighbors", "demo-1", "--json"]) == 0
    everything = json.loads(capsys.readouterr().out)
    # The linked neighbor outranks the orphan, so the filter is doing real work.
    assert [item["id"] for item in everything["neighbors"]] == ["demo-2", "demo-3"]

    assert cli.main(["neighbors", "demo-1", "--orphans-only", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [item["id"] for item in payload["neighbors"]] == ["demo-3"]
    assert all(item["structural_context"] == "none recorded" for item in payload["neighbors"])
    assert payload["filters"]["structural_link"] == "none-recorded"


def test_neighbors_orphans_only_limit_counts_surviving_neighbors(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", LinkedAdapter)

    # demo-2 outranks demo-3 but is linked; a rank-then-filter implementation
    # would spend the budget on it and return nothing.
    assert cli.main(["neighbors", "demo-1", "--limit", "1", "--orphans-only", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [item["id"] for item in payload["neighbors"]] == ["demo-3"]


def test_neighbors_orphans_only_reports_an_empty_filtered_population(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", LinkedAdapter)

    assert cli.main(["neighbors", "demo-2", "--orphans-only", "--limit", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["neighbors"] == []

    assert cli.main(["neighbors", "demo-2", "--orphans-only", "--limit", "0"]) == 0

    assert (
        "No semantic neighbors without a recorded structural link were found"
        in capsys.readouterr().out
    )


class DivergentAdapter:
    def load(self):
        snapshot = WorkspaceSnapshot(
            "workspace-test",
            "1.0.5",
            "/tmp/demo/.beads",
            live_issue_count=len(ISSUES),
            export_issue_count=len(ISSUES) - 1,
            source_divergence_reasons=("record_count", "issue_identity"),
            source_warnings=(
                "Live Beads data contains 3 issues while the discoverable JSONL export "
                "contains 2; live data was used.",
            ),
        )
        return snapshot, ISSUES


@pytest.mark.parametrize(
    "argv",
    [
        ["neighbors", "demo-1", "--include-closed", "--json"],
        ["collisions", "--json"],
        ["triage", "--json"],
    ],
)
def test_fail_on_divergence_exits_nonzero_after_writing_the_report(
    monkeypatch, tmp_path, capsys, argv
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", DivergentAdapter)

    assert cli.main(argv) == 0
    capsys.readouterr()

    assert cli.main(["--fail-on-divergence", *argv]) == cli.DIVERGENCE_EXIT_CODE

    captured = capsys.readouterr()
    assert captured.out
    assert "record_count, issue_identity" in captured.err
    assert "contains 2" in captured.err


def test_divergence_exit_code_is_distinct_from_the_error_exit_code() -> None:
    assert cli.DIVERGENCE_EXIT_CODE == 3


def test_fail_on_divergence_is_quiet_when_the_export_agrees(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["--fail-on-divergence", "neighbors", "demo-1", "--json"]) == 0
    assert capsys.readouterr().err == ""


def test_neighbors_can_load_linear_source(monkeypatch, tmp_path, capsys) -> None:
    captured: list[str] = []

    class FakeLinearAdapter:
        def __init__(self, *, team: str):
            captured.append(team)

        def load(self):
            return (
                WorkspaceSnapshot(
                    "linear-workspace",
                    None,
                    tracker_name="linear",
                    tracker_version="graphql-current",
                ),
                ISSUES,
            )

    monkeypatch.setattr(cli, "LinearAdapter", FakeLinearAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )

    assert (
        cli.main(
            [
                "--source",
                "linear",
                "--linear-team",
                "ENG",
                "neighbors",
                "demo-1",
                "--json",
            ]
        )
        == 0
    )
    assert captured == ["ENG"]
    assert json.loads(capsys.readouterr().out)["snapshot"]["tracker_name"] == "linear"


def test_linear_source_requires_team(capsys) -> None:
    assert cli.main(["--source", "linear", "neighbors", "demo-1"]) == 2
    assert "--linear-team or LINEAR_TEAM is required" in capsys.readouterr().err


def test_sweep_writes_versioned_reports_outside_workspace(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    output = tmp_path / "artifacts"

    assert cli.main(["sweep", "--size", "1", "--output", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["artifact_output"] == {
        "mode": "artifact-directory",
        "format": "json-and-markdown",
    }
    assert payload["output_directory"] == str(output)
    assert payload["parameters"]["candidate_policy"]["max_per_issue"] == 3
    assert payload["parameters"]["candidate_policy"]["max_echoes_per_target"] == 2
    assert payload["parameters"]["candidate_policy"]["max_echo_alternatives_per_active"] == 3
    assert payload["parameters"]["candidate_policy"]["max_dependencies_per_issue"] == 3
    assert payload["parameters"]["candidate_policy"]["max_total"] == 250
    assert payload["parameters"]["candidate_policy"]["lane_caps"] == {
        "dependency": 75,
        "echo": 125,
        "overlap": 125,
    }
    assert set(payload["parameters"]["candidate_policy"]["lanes"]) == {
        "dependency",
        "echo",
        "overlap",
    }
    assert payload["capped_typed_dependencies"] == []
    assert payload["parameters"]["candidate_policy"]["echo_target_hubs"] == []
    assert payload["parameters"]["candidate_policy"]["echo_backfills"] == []
    assert payload["batches"] == []
    assert payload["no_signal"] == {"count": 2, "issue_ids": ["demo-1", "demo-3"]}
    assert set(payload["timings_ms"]) == {
        "acquisition",
        "embedding_and_cache",
        "similarity_scoring",
        "candidate_analysis",
        "batching",
        "code_surface_analysis",
    }
    assert all(value >= 0 for value in payload["timings_ms"].values())
    assert (output / "report.json").is_file()
    assert (output / "report.md").is_file()
    assert list(output.glob("batch-*.json")) == []


@pytest.mark.parametrize(
    ("command", "report_type"),
    [
        ("sweep", "sweep"),
        ("batch", "sweep"),
        ("triage", "triage"),
    ],
)
def test_multi_artifact_commands_honor_json_report_file(
    monkeypatch, tmp_path, capsys, command, report_type
) -> None:
    _configure(monkeypatch, tmp_path)
    output = tmp_path / f"{command}.json"

    assert cli.main([command, "--output", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert payload["report_type"] == report_type
    assert payload["artifact_output"] == {
        "mode": "report-file",
        "format": "json",
    }
    if report_type == "triage":
        assert payload["source_report"]["model"]["model_revision"] == "1"
        assert "parameters" not in payload["source_report"]
    assert not (tmp_path / "state").exists()


def test_sweep_report_file_extension_controls_format_independently_of_stdout(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    output = tmp_path / "sweep.md"

    assert cli.main(["sweep", "--output", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["artifact_output"] == {
        "mode": "report-file",
        "format": "markdown",
    }
    assert output.read_text(encoding="utf-8").startswith("# emBEADings sweep")
    assert not output.is_dir()


def test_sweep_output_dir_is_unambiguous_and_complete(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    output = tmp_path / "report.json"

    assert cli.main(["sweep", "--output-dir", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert output.is_dir()
    assert payload["artifact_output"] == {
        "mode": "artifact-directory",
        "format": "json-and-markdown",
    }
    assert payload["output_directory"] == str(output)
    assert (output / "report.json").is_file()
    assert (output / "report.md").is_file()


def test_sweep_rejects_conflicting_output_spellings(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["sweep", "--output", "report.json", "--output-dir", "artifacts"])

    assert raised.value.code == 2
    assert "not allowed with argument --output" in capsys.readouterr().err


def test_analysis_help_distinguishes_report_files_from_artifact_directories(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["sweep", "--help"])

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "--output PATH" in help_text
    assert "--output-dir DIRECTORY" in help_text
    assert "Write one .json/.md report file" in help_text
    assert "complete report and per-batch artifact set" in help_text


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_sweep_restricts_run_reports_and_state_root_on_posix(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir(mode=0o755)

    assert cli.main(["sweep", "--size", "1", "--json"]) == 0
    capsys.readouterr()

    run = next(state.iterdir())

    def mode(path):
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(state) == 0o700
    assert mode(run) == 0o700
    assert mode(run / "report.json") == 0o600
    assert mode(run / "report.md") == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_sweep_restricts_explicit_report_file_on_posix(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    output = tmp_path / "report.json"

    assert cli.main(["sweep", "--output", str(output), "--json"]) == 0
    capsys.readouterr()

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_triage_is_opinionated_compact_and_deterministic(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    first_output = tmp_path / "triage-first"
    second_output = tmp_path / "triage-second"

    assert (
        cli.main(
            [
                "--provider",
                "hashing",
                "triage",
                "--review-budget",
                "2",
                "--output",
                str(first_output),
                "--json",
            ]
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert (
        cli.main(
            [
                "--provider",
                "hashing",
                "triage",
                "--review-budget",
                "2",
                "--output",
                str(second_output),
                "--json",
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)

    assert first == second
    assert first["report_type"] == "triage"
    assert first["review_budget"]["candidate_limit"] == 2
    assert first["source_report"]["analysis_fingerprint"] == first["analysis_fingerprint"]
    assert (first_output / "report.json").is_file()
    assert (first_output / "triage.json").is_file()
    assert json.loads((first_output / "triage.json").read_text()) == first
    full_report = json.loads((first_output / "report.json").read_text())
    assert full_report["analysis_fingerprint"] == first["analysis_fingerprint"]


def test_triage_returns_semantic_lanes_with_count_only_degradation_receipt(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    original = ranking_module._dependency_discovery_funnel

    def broken_funnel(*args, enabled, **kwargs):
        if enabled:
            return ranking_module.DependencyFunnel(
                total_non_parent_typed=1,
                eligible=1,
                eligible_candidates=2,
            )
        return original(*args, enabled=enabled, **kwargs)

    monkeypatch.setattr(ranking_module, "_dependency_discovery_funnel", broken_funnel)
    output = tmp_path / "degraded-triage"
    repeat_output = tmp_path / "degraded-triage-repeat"

    def run(target):
        assert (
            cli.main(
                [
                    "--provider",
                    "hashing",
                    "triage",
                    "--output",
                    str(target),
                    "--json",
                ]
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    packet = run(output)
    repeated_packet = run(repeat_output)
    report = json.loads((output / "report.json").read_text())
    assert repeated_packet == packet
    assert packet["analysis_status"] == "degraded"
    assert packet["degradations"] == report["degradations"]
    assert packet["degradations"][0]["stage"] == "edge-to-candidate"
    assert packet["degradations"][0]["degraded_lanes"] == ["dependency"]
    assert packet["degradations"][0]["retained_lanes"] == ["echo", "overlap"]
    assert set(packet["degradations"][0]["counts"]) == {
        "total_non_parent_typed",
        "inactive_or_closed_only",
        "below_qualification",
        "eligible",
        "eligible_candidates",
        "coalesced_eligible_edges",
        "admitted",
        "omitted_by_per_issue_cap",
        "omitted_by_lane_cap",
        "omitted_by_run_cap",
    }
    assert packet["lane_summary"]["dependency"] == {
        "qualified": 0,
        "admitted": 0,
        "omitted": 0,
    }
    assert {candidate["lane"] for candidate in packet["candidates"]} <= {"echo", "overlap"}
    assert packet["degradations"][0]["balance"] == {
        "input": {"name": "eligible", "count": 1},
        "parts": [
            {"name": "eligible_candidates", "count": 2},
            {"name": "coalesced_eligible_edges", "count": 0},
        ],
        "unaccounted": -1,
    }
    delta = "eligible=1, eligible_candidates=2, coalesced_eligible_edges=0, unaccounted=-1"
    assert f"Dependency lane unavailable: its conservation audit failed ({delta})" in "\n".join(
        packet["warnings"]
    )
    triage_markdown = (output / "triage.md").read_text()
    assert "Analysis status: **degraded**" in triage_markdown
    assert f"conservation ({delta})" in triage_markdown


def test_degraded_triage_report_file_retains_diagnostic_provenance(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    original = ranking_module._dependency_discovery_funnel

    def broken_funnel(*args, enabled, **kwargs):
        if enabled:
            return ranking_module.DependencyFunnel(
                total_non_parent_typed=1,
                eligible=1,
                eligible_candidates=2,
            )
        return original(*args, enabled=enabled, **kwargs)

    monkeypatch.setattr(ranking_module, "_dependency_discovery_funnel", broken_funnel)
    output = tmp_path / "degraded-triage.json"

    assert (
        cli.main(
            [
                "--provider",
                "hashing",
                "triage",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )

    packet = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == packet
    assert packet["analysis_status"] == "degraded"
    assert packet["source_report"]["analysis_fingerprint"] == packet["analysis_fingerprint"]
    assert packet["source_report"]["model"] == {
        "model_id": "hashing/32",
        "model_revision": "1",
    }
    assert packet["source_report"]["parameters"]["candidate_policy"]["max_total"] == 20
    assert packet["degradations"][0]["stage"] == "edge-to-candidate"


def test_empty_structure_sweep_skips_embeddings_and_preserves_funnel(
    monkeypatch, tmp_path, capsys
) -> None:
    issues = (
        IssueRecord(id="active", title="Active work", status="open"),
        IssueRecord(
            id="closed-a",
            title="Closed work A",
            status="closed",
            dependency_links=(DependencyLink("closed-a", "closed-b", "blocks"),),
        ),
        IssueRecord(id="closed-b", title="Closed work B", status="closed"),
    )

    class ClosedOnlyAdapter:
        def load(self):
            return WorkspaceSnapshot("closed-only", "1.0.5", "/tmp/demo/.beads"), issues

    def no_vectors(*_args, **_kwargs):
        raise AssertionError("empty structural audits must not load semantic vectors")

    monkeypatch.setattr(cli, "BeadsAdapter", ClosedOnlyAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(cli, "_load_vectors", no_vectors)
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )

    output = tmp_path / "structure-only"
    assert (
        cli.main(
            [
                "sweep",
                "--objective",
                "structure",
                "--weekly-review-budget",
                "20",
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == []
    assert payload["cache"] == {
        "hits": 0,
        "misses": 0,
        "embedding_skipped": True,
        "skip_reason": "no-reviewable-typed-relationships",
        "skipped_records": 3,
    }
    assert payload["parameters"]["candidate_policy"]["dependency_funnel"] == {
        "total_non_parent_typed": 1,
        "inactive_or_closed_only": 1,
        "below_qualification": 0,
        "eligible": 0,
        "eligible_candidates": 0,
        "coalesced_eligible_edges": 0,
        "admitted": 0,
        "omitted_by_per_issue_cap": 0,
        "omitted_by_lane_cap": 0,
        "omitted_by_run_cap": 0,
    }
    assert "Semantic embedding skipped" in payload["warnings"][0]
    assert not (tmp_path / "cache").exists()


def test_collisions_is_corpus_read_only_and_does_not_load_embedding_provider(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)

    def no_provider(_name):
        raise AssertionError("collision analysis must not load an embedding provider")

    analysis = {
        "repository_available": True,
        "repository_revision": "revision",
        "base_reference": "origin/main",
        "base_revision": "base-revision",
        "issue_count": 2,
        "pointer_count": 2,
        "issues_with_explicit_surfaces": 0,
        "issues_with_observed_surfaces": 2,
        "issues_without_surfaces": 0,
        "worktrees_discovered": 3,
        "worktrees_associated": 2,
        "source_counts": {"active-worktree-diff": 2},
        "hub_surface_limit": 5,
        "hub_surfaces": [],
        "pairs_omitted_by_hub_guard": 0,
        "surfaces": [],
        "collisions": [
            {
                "issue_id": "demo-1",
                "related_issue_id": "demo-3",
                "kind": "exact-file",
                "confidence": "observed",
                "shared_paths": ["src/cache/index.py"],
                "shared_symbols": [],
                "shared_modules": ["src/cache"],
                "evidence_sources": ["active-worktree-diff"],
                "revision_relation": "same",
                "what_to_verify": "Coordinate the shared file.",
            }
        ],
        "warnings": [],
    }
    monkeypatch.setattr(cli, "_provider", no_provider)
    monkeypatch.setattr(cli, "_surface_analysis", lambda *_args: analysis)

    output = tmp_path / "collisions.json"
    assert cli.main(["collisions", "--output", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["report_type"] == "collisions"
    assert payload["policy"]["snippets_included"] is False
    assert payload["code_surface_analysis"]["collisions"][0]["kind"] == "exact-file"
    assert json.loads(output.read_text()) == payload


def test_explain_hub_guard_requests_and_renders_a_bounded_sample(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    requested: list[int] = []

    def analysis(args, _snapshot, _issues, eligible_issue_ids=None):
        requested.append(args.explain_hub_guard)
        return {
            "repository_available": True,
            "issue_count": 2,
            "hub_surface_limit": 5,
            "hub_surfaces": [
                {"kind": "path", "surface": "docs/CLAUDE.md", "issue_count": 9},
            ],
            "pairs_omitted_by_hub_guard": 1297,
            "hub_guard_sample": [
                {
                    "issue_id": "demo-1",
                    "related_issue_id": "demo-3",
                    "suppressing_paths": ["docs/CLAUDE.md"],
                    "suppressing_modules": [],
                }
            ][: args.explain_hub_guard],
            "surfaces": [],
            "collisions": [],
            "warnings": [],
        }

    monkeypatch.setattr(cli, "_surface_analysis", analysis)

    assert cli.main(["collisions"]) == 0
    assert "Hub guard sample" not in capsys.readouterr().out
    assert requested == [0]

    assert cli.main(["collisions", "--explain-hub-guard"]) == 0
    rendered = capsys.readouterr().out

    assert requested == [0, 5]
    assert "### Hub guard sample" in rendered
    assert "1 of 1297 suppressed pairs" in rendered
    assert "`demo-1` ↔ `demo-3`" in rendered
    assert "docs/CLAUDE.md" in rendered

    assert cli.main(["collisions", "--explain-hub-guard", "25"]) == 0
    capsys.readouterr()
    assert requested == [0, 5, 25]


def test_collisions_excludes_deferred_by_default_but_keeps_it_opt_in(monkeypatch, capsys) -> None:
    snapshot = WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads")
    issues = (
        IssueRecord("open", "Current", status="open"),
        IssueRecord("deferred", "Later", status="deferred"),
    )
    populations: list[list[str]] = []
    monkeypatch.setattr(cli, "_load_source", lambda _args: (snapshot, issues))
    monkeypatch.setattr(
        cli,
        "_surface_analysis",
        lambda _args, _snapshot, population: (
            populations.append([issue.id for issue in population]) or {"collisions": []}
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_collisions_payload",
        lambda _analysis, *, snapshot, filters: {
            "schema_version": 1,
            "report_type": "collisions",
            "snapshot": snapshot,
            "filters": filters,
        },
    )

    assert cli.main(["collisions", "--json"]) == 0
    default = json.loads(capsys.readouterr().out)
    assert populations[-1] == ["open"]
    assert default["filters"]["status"] == ["blocked", "in_progress", "open"]

    assert cli.main(["collisions", "--status", "deferred", "--json"]) == 0
    explicit = json.loads(capsys.readouterr().out)
    assert populations[-1] == ["deferred"]
    assert explicit["filters"]["status"] == ["deferred"]


def test_surface_analysis_passes_the_invoking_worktree(monkeypatch, tmp_path) -> None:
    invoking = tmp_path / "current-worktree"
    invoking.mkdir()
    monkeypatch.chdir(invoking)
    captured = {}

    class Analysis:
        def to_dict(self):
            return {"repository_context": "invocation-worktree"}

    def analyze(_issues, **kwargs):
        captured.update(kwargs)
        return Analysis()

    monkeypatch.setattr(cli, "analyze_code_surfaces", analyze)
    result = cli._surface_analysis(
        Namespace(
            worktree_map=[],
            base_ref="origin/main",
            max_hub_surface_issues=5,
            explain_hub_guard=0,
        ),
        WorkspaceSnapshot("workspace-test", "1.0.5", str(tmp_path / "owner")),
        [],
    )

    assert result == {"repository_context": "invocation-worktree"}
    assert captured["workspace_path"] == str(tmp_path / "owner")
    assert captured["invocation_path"] == invoking


def test_sweep_can_include_code_surface_analysis(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    analysis = {
        "repository_available": False,
        "repository_revision": None,
        "base_reference": None,
        "base_revision": None,
        "issue_count": 2,
        "pointer_count": 0,
        "issues_with_explicit_surfaces": 0,
        "issues_with_observed_surfaces": 0,
        "issues_without_surfaces": 2,
        "worktrees_discovered": 0,
        "worktrees_associated": 0,
        "source_counts": {},
        "hub_surface_limit": 5,
        "hub_surfaces": [],
        "pairs_omitted_by_hub_guard": 0,
        "surfaces": [],
        "collisions": [],
        "warnings": ["No Git repository was available."],
    }
    monkeypatch.setattr(cli, "_surface_analysis", lambda *_args: analysis)

    output = tmp_path / "surface-sweep"
    assert cli.main(["sweep", "--code-surfaces", "--output", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["code_surface_analysis"] == analysis
    assert payload["timings_ms"]["code_surface_analysis"] >= 0
    assert "Code-surface collision evidence" in (output / "report.md").read_text()


def test_sweep_propagates_non_sensitive_source_warning(monkeypatch, tmp_path, capsys) -> None:
    warning = (
        "Live Beads data contains 3 issues while the discoverable JSONL export contains 2; "
        "live data was used."
    )

    class StaleExportAdapter:
        def load(self):
            return (
                WorkspaceSnapshot(
                    "workspace-test",
                    "1.0.5",
                    "/tmp/demo/.beads",
                    acquisition_source="live-beads-cli",
                    live_issue_count=3,
                    export_issue_count=2,
                    source_warnings=(warning,),
                ),
                ISSUES,
            )

    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", StaleExportAdapter)

    assert cli.main(["sweep", "--output", str(tmp_path / "out"), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["warnings"] == [warning]
    assert payload["snapshot"]["acquisition_source"] == "live-beads-cli"
    assert payload["snapshot"]["live_issue_count"] == 3
    assert payload["snapshot"]["export_issue_count"] == 2


def test_sweep_rejects_invalid_candidate_volume_controls(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["sweep", "--max-candidates", "0", "--json"]) == 2

    assert "run candidate cap must be positive" in capsys.readouterr().err

    assert cli.main(["sweep", "--weekly-review-budget", "0", "--json"]) == 2

    assert "run candidate cap must be positive" in capsys.readouterr().err

    assert cli.main(["sweep", "--max-echoes-per-target", "0", "--json"]) == 2

    assert "completed-target echo cap must be positive" in capsys.readouterr().err

    assert cli.main(["sweep", "--max-echo-alternatives-per-active", "0", "--json"]) == 2

    assert "per-active echo alternative cap must be positive" in capsys.readouterr().err


def test_review_lane_reservations_respect_explicit_lane_caps() -> None:
    assert cli._review_lane_reservations(
        20,
        {"dependency": 3, "echo": 2, "overlap": 0},
    ) == {"dependency": 3, "echo": 2, "overlap": 0}


def test_weekly_budget_composes_with_incremental_scope_and_dependency_allowance(
    monkeypatch, tmp_path, capsys
) -> None:
    issues = (
        IssueRecord("changed", "Changed", status="open", updated_at="2026-07-14T01:00:00Z"),
        IssueRecord("context", "Context", status="open", updated_at="2026-07-01T01:00:00Z"),
        IssueRecord("closed", "Closed", status="closed", updated_at="2026-07-01T01:00:00Z"),
    )

    class WeeklyAdapter:
        def load(self):
            return WorkspaceSnapshot("workspace-test", "1.0.5", None), issues

    def fake_candidates(_population, _issues, _vectors, **kwargs):
        assert kwargs["eligible_issue_ids"] == {"changed"}
        assert kwargs["max_candidates"] == 2
        assert kwargs["max_dependency_candidates_per_issue"] == 7
        assert kwargs["max_echoes_per_target"] == 1
        assert kwargs["max_echo_alternatives_per_active"] == 2
        assert kwargs["lane_reservations"] == {
            "dependency": 1,
            "echo": 0,
            "overlap": 1,
        }
        return CandidateRanking(
            candidates=(
                {
                    "kind": "possible-overlap",
                    "lane": "dependency",
                    "issue_id": "changed",
                    "related_issue_id": "context",
                    "similarity": 0.9,
                },
                {
                    "kind": "completed-work-echo",
                    "lane": "echo",
                    "issue_id": "changed",
                    "related_issue_id": "closed",
                    "similarity": 0.88,
                },
            ),
            qualified=3,
            dropped_by_issue_cap=0,
            dropped_by_run_cap=1,
            lanes={
                "dependency": LaneMetrics(
                    qualified=1,
                    admitted=1,
                    reserved=1,
                    admitted_to_reservation=1,
                ),
                "echo": LaneMetrics(qualified=1, admitted=1),
                "overlap": LaneMetrics(
                    qualified=1,
                    dropped_by_run_cap=1,
                    reserved=1,
                    unused_reserved=1,
                ),
            },
        )

    monkeypatch.setattr(cli, "BeadsAdapter", WeeklyAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(cli, "_candidate_evidence", fake_candidates)
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )

    assert (
        cli.main(
            [
                "sweep",
                "--weekly-review-budget",
                "2",
                "--max-dependency-candidates-per-issue",
                "7",
                "--max-echoes-per-target",
                "1",
                "--max-echo-alternatives-per-active",
                "2",
                "--changed-since",
                "2026-07-10T00:00:00Z",
                "--output",
                str(tmp_path / "weekly"),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["candidates"]) == 2
    assert payload["parameters"]["filters"]["incremental_scope"]["mode"] == "changed-since"
    assert payload["parameters"]["candidate_policy"]["max_dependencies_per_issue"] == 7
    assert payload["parameters"]["candidate_policy"]["review_budget"] == {
        "admitted_candidates": 2,
        "candidate_limit": 2,
        "mode": "weekly",
        "omitted_by_lane": {"dependency": 0, "echo": 0, "overlap": 1},
        "omitted_candidates": 1,
        "code_surface_scope": "outside-semantic-candidate-limit",
        "lane_capacity": {
            "dependency": {"admitted_to_reservation": 1, "reserved": 1, "unused": 0},
            "echo": {"admitted_to_reservation": 0, "reserved": 0, "unused": 0},
            "overlap": {"admitted_to_reservation": 0, "reserved": 1, "unused": 1},
        },
        "reservation_order": ["dependency", "echo", "overlap"],
        "selection_policy": "reserved-lane-access-then-priority-reflow",
        "priority_order": [
            "typed-dependency",
            "high-confidence-completed-work-echo",
            "possible-overlap",
        ],
    }
    markdown = (tmp_path / "weekly" / "report.md").read_text()
    assert "Mode: weekly" in markdown
    assert "Omitted by lane: dependency=0, echo=0, overlap=1" in markdown
    assert "Dependency: 1 reserved; 1 admitted to reservation; 0 unused" in markdown
    assert "Code-surface leads: separately prioritized outside" in markdown


def test_ranked_candidates_keep_structural_admission_and_specific_explanation() -> None:
    issues = (
        IssueRecord(
            id="demo-a",
            title="Persist authentication session",
            description="Store login tokens between restarts",
            acceptance_criteria="Users remain signed in after relaunch",
            status="open",
            parent_id="demo-parent",
        ),
        IssueRecord(
            id="demo-b",
            title="Remember authentication session",
            description="Keep login tokens between restarts",
            acceptance_criteria="Users remain signed in after relaunch",
            status="open",
            parent_id="demo-parent",
        ),
    )

    class ScoreIndex:
        def score(self, _left_id, _right_id):
            return 0.75

    result = cli._candidate_evidence(
        list(issues),
        issues,
        {},
        echo_threshold=0.8,
        overlap_threshold=0.8,
        similarity_index=ScoreIndex(),
        exception_margin=0.1,
        reciprocal_rank=0,
    )

    candidate = result.candidates[0]
    assert candidate["admission_reason"] == "shared-parent-threshold-exception"
    assert candidate["field_evidence"]
    assert "strongest field alignment" in candidate["why_surfaced"]


def test_sweep_batches_only_signal_issues_and_reports_no_signal_and_epics(
    monkeypatch, tmp_path, capsys
) -> None:
    issues = (
        IssueRecord("active-a", "A", status="open", issue_type="feature"),
        IssueRecord("active-b", "B", status="open", issue_type="task"),
        IssueRecord("no-signal", "No signal", status="open", issue_type="feature"),
        IssueRecord("container", "Broad epic", status="open", issue_type="epic"),
        IssueRecord("closed", "Completed", status="closed", issue_type="task"),
        IssueRecord("active-wisp", "Patrol", status="open", ephemeral=True),
        IssueRecord("closed-wisp", "Patrol", status="closed", ephemeral=True),
    )

    class CandidateAdapter:
        def load(self):
            return WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"), issues

    def fake_candidates(population, all_issues, _vectors, **_kwargs):
        assert [issue.id for issue in population] == ["active-a", "active-b", "no-signal"]
        assert {issue.id for issue in all_issues} == {
            "active-a",
            "active-b",
            "no-signal",
            "container",
            "closed",
        }
        return CandidateRanking(
            candidates=(
                {
                    "kind": "possible-overlap",
                    "issue_id": "active-a",
                    "related_issue_id": "active-b",
                    "similarity": 0.9,
                },
                {
                    "kind": "completed-work-echo",
                    "issue_id": "active-a",
                    "related_issue_id": "closed",
                    "similarity": 0.9,
                },
            ),
            qualified=2,
            dropped_by_issue_cap=0,
            dropped_by_run_cap=0,
        )

    monkeypatch.setattr(cli, "BeadsAdapter", CandidateAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(cli, "_candidate_evidence", fake_candidates)
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )

    output = tmp_path / "candidate-artifacts"
    assert cli.main(["sweep", "--size", "5", "--output", str(output), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["batches"] == [
        {
            "batch": 1,
            "kind": "connected-component",
            "issue_ids": ["active-a", "active-b"],
            "review_units": [{"issue_ids": ["active-a", "active-b"]}],
        }
    ]
    assert payload["batch_diagnostics"]["max_batch_size"] == 2
    assert payload["batch_diagnostics"]["configured_max_batch_size"] == 5
    assert payload["no_signal"] == {"count": 1, "issue_ids": ["no-signal"]}
    assert payload["excluded"] == {
        "by_reason": {"ephemeral": 2, "epic": 1},
        "count": 3,
        "issue_ids": ["active-wisp", "closed-wisp", "container"],
    }
    manifest = json.loads((output / "batch-1.json").read_text())
    assert {issue["id"] for issue in manifest["issues"]} == {"active-a", "active-b"}
    assert {item["related_issue_id"] for item in manifest["neighbor_evidence"]} == {
        "active-b",
        "closed",
    }
    assert "Ephemeral runtime records excluded: 2" in (output / "report.md").read_text()


def test_sweep_can_explicitly_include_ephemeral_records(monkeypatch, tmp_path, capsys) -> None:
    issues = (
        IssueRecord("durable", "Durable", status="open"),
        IssueRecord("wisp", "Patrol", status="open", ephemeral=True),
    )

    class EphemeralAdapter:
        def load(self):
            return WorkspaceSnapshot("workspace-test", "1.0.5", None), issues

    def fake_candidates(population, all_issues, _vectors, **_kwargs):
        assert {issue.id for issue in population} == {"durable", "wisp"}
        assert {issue.id for issue in all_issues} == {"durable", "wisp"}
        return CandidateRanking(
            candidates=(), qualified=0, dropped_by_issue_cap=0, dropped_by_run_cap=0
        )

    monkeypatch.setattr(cli, "BeadsAdapter", EphemeralAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=8))
    monkeypatch.setattr(cli, "_candidate_evidence", fake_candidates)
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )

    output = tmp_path / "include-ephemeral"
    assert cli.main(["sweep", "--include-ephemeral", "--output", str(output), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parameters"]["filters"]["include_ephemeral"] is True
    assert payload["excluded"] == {"by_reason": {}, "count": 0, "issue_ids": []}


def test_changed_since_scopes_candidates_reports_unchanged_and_writes_checkpoint(
    monkeypatch, tmp_path, capsys
) -> None:
    issues = (
        IssueRecord("changed", "Changed", status="open", updated_at="2026-07-14T01:00:00Z"),
        IssueRecord("unchanged", "Unchanged", status="open", updated_at="2026-07-01T01:00:00Z"),
    )

    class IncrementalAdapter:
        def load(self):
            return WorkspaceSnapshot("workspace-test", "1.0.5", None), issues

    def fake_candidates(_population, _issues, _vectors, **kwargs):
        assert kwargs["eligible_issue_ids"] == {"changed"}
        return CandidateRanking(
            candidates=(
                {
                    "kind": "possible-overlap",
                    "issue_id": "changed",
                    "related_issue_id": "unchanged",
                    "similarity": 0.9,
                },
            ),
            qualified=1,
            dropped_by_issue_cap=0,
            dropped_by_run_cap=0,
        )

    monkeypatch.setattr(cli, "BeadsAdapter", IncrementalAdapter)
    monkeypatch.setattr(cli, "_provider", lambda _name: HashingProvider(dimension=32))
    monkeypatch.setattr(cli, "_candidate_evidence", fake_candidates)

    def fake_surfaces(_args, _snapshot, population, eligible_issue_ids):
        assert {issue.id for issue in population} == {"changed", "unchanged"}
        assert eligible_issue_ids == {"changed"}
        return {"collisions": [], "scope": "incremental"}

    monkeypatch.setattr(cli, "_surface_analysis", fake_surfaces)
    monkeypatch.setattr(
        cli, "_workspace_paths", lambda _identity: (tmp_path / "cache", tmp_path / "state")
    )
    checkpoint = tmp_path / "checkpoint.json"
    assert (
        cli.main(
            [
                "sweep",
                "--code-surfaces",
                "--changed-since",
                "2026-07-10T00:00:00Z",
                "--write-checkpoint",
                str(checkpoint),
                "--output",
                str(tmp_path / "out"),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["code_surface_analysis"] == {
        "collisions": [],
        "scope": "incremental",
    }
    assert payload["parameters"]["filters"]["incremental_scope"] == {
        "checkpoint_created_at": None,
        "changed_active_count": 1,
        "changed_closed_count": 0,
        "deleted_since_checkpoint_count": 0,
        "mode": "changed-since",
        "unchanged_active_count": 1,
        "unknown_timestamp_count": 0,
    }
    assert payload["excluded"] == {
        "by_reason": {"unchanged": 1},
        "count": 1,
        "issue_ids": ["unchanged"],
    }
    checkpoint_payload = json.loads(checkpoint.read_text())
    assert set(checkpoint_payload["issues"]) == {"changed", "unchanged"}
    assert "Changed" not in checkpoint.read_text()
    if os.name == "posix":
        assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    markdown = (tmp_path / "out" / "report.md").read_text()
    assert "Review scope: changed-since" in markdown
    assert "Unchanged active records excluded: 1" in markdown


def test_neighbors_reports_rank_and_reverse_rank(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["neighbors", "demo-1", "--include-closed", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [item["rank"] for item in payload["neighbors"]] == [1, 2]
    for item in payload["neighbors"]:
        # With three records, the seed is either the neighbor's first or second choice.
        assert item["reverse_rank"] in {1, 2}
        assert item["text_claims"] == []


def test_neighbors_reverse_rank_matches_the_neighbors_own_query(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["neighbors", "demo-1", "--include-closed", "--json"]) == 0
    forward = {item["id"]: item for item in json.loads(capsys.readouterr().out)["neighbors"]}
    for neighbor_id, item in forward.items():
        assert cli.main(["neighbors", neighbor_id, "--include-closed", "--json"]) == 0
        reverse = json.loads(capsys.readouterr().out)["neighbors"]
        assert [entry["id"] for entry in reverse].index("demo-1") + 1 == item["reverse_rank"]


CLAIMING_ISSUES = (
    IssueRecord(
        id="demo-1",
        title="Persist authentication tokens",
        description="Keep users signed in across browser restarts",
        status="open",
    ),
    IssueRecord(
        id="demo-2",
        title="Remember login state",
        description="Store authentication tokens between sessions",
        status="open",
        notes="Absorbed by demo-1 after the auth review.",
    ),
    IssueRecord(
        id="demo-3",
        title="Lazy load gallery images",
        description="Defer thumbnail loading until images are visible",
        status="open",
        notes="Fixed by demo-2; not a duplicate of demo-1.",
        close_reason="private close text",
    ),
    IssueRecord(
        id="demo-4",
        title="Image placeholders",
        status="closed",
        notes="Superseded by demo-3.",
    ),
)


class ClaimingAdapter:
    def load(self):
        return WorkspaceSnapshot("workspace-test", "1.0.5", "/tmp/demo/.beads"), CLAIMING_ISSUES


def test_neighbors_batch_loads_the_corpus_once(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", ClaimingAdapter)
    loads = []
    original = cli._load_source
    monkeypatch.setattr(cli, "_load_source", lambda args: loads.append(1) or original(args))
    ids_file = tmp_path / "seeds.txt"
    ids_file.write_text("# seeds\ndemo-2\n\ndemo-1  # repeated seed is ignored\n")

    assert cli.main(["neighbors", "demo-1", "--ids-file", str(ids_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert loads == [1]
    assert payload["report_type"] == "neighbors-batch"
    assert [result["issue"]["id"] for result in payload["results"]] == ["demo-1", "demo-2"]
    seed_two = {item["id"]: item for item in payload["results"][1]["neighbors"]}
    assert seed_two["demo-1"]["text_claims"] == [
        {"issue_id": "demo-2", "related_issue_id": "demo-1", "kind": "absorbed-by"}
    ]

    assert cli.main(["neighbors", "demo-1", "demo-2"]) == 0
    rendered = capsys.readouterr().out
    assert rendered.count("# Semantic neighbors for") == 2
    assert "demo-2 absorbed-by demo-1" in rendered


def test_neighbors_rejects_missing_seeds_before_loading(monkeypatch, tmp_path, capsys) -> None:
    _configure(monkeypatch, tmp_path)

    assert cli.main(["neighbors"]) == 2
    assert "at least one ISSUE_ID" in capsys.readouterr().err
    assert cli.main(["neighbors", "demo-1", "demo-404"]) == 2
    assert "issue not found: demo-404" in capsys.readouterr().err


def test_mentions_reports_unlinked_active_claims_without_text(
    monkeypatch, tmp_path, capsys
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "BeadsAdapter", ClaimingAdapter)

    def no_provider(_name):
        raise AssertionError("mentions must not load an embedding provider")

    monkeypatch.setattr(cli, "_provider", no_provider)

    assert cli.main(["mentions", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["report_type"] == "mentions"
    assert payload["policy"]["tracker_mutation_allowed"] is False
    assert [(c["issue_id"], c["kind"], c["related_issue_id"]) for c in payload["claims"]] == [
        ("demo-2", "absorbed-by", "demo-1"),
        ("demo-3", "fixed-by", "demo-2"),
    ]
    assert payload["summary"]["claimants_scanned"] == 3
    assert "private close text" not in output
    assert "after the auth review" not in output

    assert cli.main(["mentions", "--include-closed", "--limit", "1", "--json"]) == 0
    limited = json.loads(capsys.readouterr().out)
    assert len(limited["claims"]) == 1
    assert limited["summary"]["claims_found"] == 3
    assert limited["summary"]["omitted_by_limit"] == 2

    assert cli.main(["mentions"]) == 0
    rendered = capsys.readouterr().out
    assert "- Claims found: 2 (absorbed-by 1, fixed-by 1)" in rendered
    assert "| demo-2 | open | absorbed-by | demo-1 | open | notes | no |" in rendered


def test_code_surface_flags_reach_the_analysis(monkeypatch, tmp_path) -> None:
    _configure(monkeypatch, tmp_path)
    captured = {}

    class Analysis:
        def to_dict(self):
            return {"collisions": [], "warnings": []}

    def analyze(_issues, **kwargs):
        captured.update(kwargs)
        return Analysis()

    monkeypatch.setattr(cli, "analyze_code_surfaces", analyze)

    assert (
        cli.main(
            [
                "collisions",
                "--min-confidence",
                "observed",
                "--branch-pattern",
                r"lane/(?P<id>[a-z0-9.]+)",
                "--json",
            ]
        )
        == 0
    )
    assert captured["min_confidence"] == "observed"
    assert [pattern.pattern for pattern in captured["branch_patterns"]] == [
        r"lane/(?P<id>[a-z0-9.]+)"
    ]
    assert cli.main(["collisions", "--branch-pattern", "lane/.*"]) == 2
