"""Strictly read-only access to Beads through its supported CLI."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeAlias

from .models import DependencyLink, IssueRecord, WorkspaceSnapshot
from .trackers import TrackerError


class BeadsError(TrackerError):
    """Raised when Beads cannot provide a safe, valid snapshot."""


Runner: TypeAlias = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_ALLOWED_COMMANDS = frozenset({"version", "context", "list"})
MINIMUM_BEADS_VERSION = "1.0.5"
BEADS_CAPABILITY_CONTRACT = (
    "bd --readonly version --json, bd --readonly context --json, and "
    "bd --readonly list --all --limit 0 --json"
)
_SEMVER_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<suffix>(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$"
)
_RFC3339_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<offset>Z|[+-]\d{2}:\d{2})$"
)


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


class BeadsAdapter:
    """Small allowlisted adapter that cannot invoke tracker mutation commands."""

    def __init__(self, *, binary: str = "bd", runner: Runner = _default_runner) -> None:
        self._binary = binary
        self._runner = runner

    @property
    def binary(self) -> str:
        """Return the configured executable name or path without invoking it."""

        return self._binary

    def resolved_binary(self) -> str | None:
        """Return the executable selected by PATH, if it is currently discoverable."""

        resolved = shutil.which(self._binary)
        return str(Path(resolved).resolve()) if resolved else None

    def _run(self, command: str, *arguments: str) -> str:
        if command not in _ALLOWED_COMMANDS:
            raise BeadsError(f"Beads command is not allowlisted: {command}")
        argv = [self._binary, "--readonly", command, *arguments]
        try:
            result = self._runner(argv)
        except OSError as exc:
            raise BeadsError(
                f"Beads executable {self._binary!r} could not be started. "
                "Install a compatible bd release and ensure it is on PATH."
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if len(detail) > 500:
                detail = detail[:500] + "…"
            raise BeadsError(f"bd {command} failed: {detail or 'unknown error'}")
        return result.stdout

    def _run_json(self, command: str, *arguments: str) -> Any:
        output = self._run(command, *arguments, "--json")
        try:
            return json.loads(output)
        except (json.JSONDecodeError, TypeError) as exc:
            raise BeadsError(f"bd {command} returned malformed JSON") from exc

    def version(self) -> str:
        payload = self._run_json("version")
        version: str | None = None
        if isinstance(payload, str) and payload.strip():
            version = payload.strip()
        elif isinstance(payload, Mapping):
            for key in ("version", "beads_version"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    version = value.strip()
                    break
        if version is None:
            raise BeadsError("bd version JSON has an unsupported shape")
        _require_compatible_version(version)
        return version

    def workspace_snapshot(self) -> WorkspaceSnapshot:
        payload = self._run_json("context")
        if not isinstance(payload, Mapping):
            raise BeadsError("bd context JSON must be an object")

        path_value = _first(payload, "workspace_path", "beads_dir", "repo_root", "path")
        identity_value = _first(payload, "project_id", "workspace_id", "repository_id", "repo_id")
        if path_value is not None and not isinstance(path_value, str):
            raise BeadsError("bd context workspace path must be a string")
        if identity_value is not None and not isinstance(identity_value, str):
            raise BeadsError("bd context workspace identity must be a string")
        if not identity_value and not path_value:
            raise BeadsError("bd context JSON contains no workspace identity")

        canonical_path = str(Path(path_value).expanduser().resolve()) if path_value else None
        workspace_id = identity_value or hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
        version = self.version()
        return WorkspaceSnapshot(
            workspace_id=workspace_id,
            workspace_path=canonical_path,
            beads_version=version,
            tracker_name="beads",
            tracker_version=version,
        )

    def list_issues(self) -> tuple[IssueRecord, ...]:
        payload = self._run_json("list", "--all", "--limit", "0")
        raw_issues = _issue_list(payload)
        records = tuple(_parse_issue(item) for item in raw_issues)
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise BeadsError("bd list returned duplicate issue IDs")
        return records

    def load(self) -> tuple[WorkspaceSnapshot, tuple[IssueRecord, ...]]:
        """Return snapshot metadata and all issues from the live workspace."""

        snapshot = self.workspace_snapshot()
        issues = self.list_issues()
        relationship_types = Counter(
            link.relationship_type for issue in issues for link in issue.dependency_links
        )
        live_digest = _canonical_state_digest(issues)
        export_count, export_digest, divergence_reasons, source_warnings = _export_diagnostics(
            snapshot.workspace_path,
            live_records=issues,
            live_digest=live_digest,
        )
        snapshot = replace(
            snapshot,
            dependency_count=sum(relationship_types.values()),
            dependency_type_counts=tuple(sorted(relationship_types.items())),
            live_issue_count=len(issues),
            export_issue_count=export_count,
            live_source_digest=live_digest,
            export_source_digest=export_digest,
            source_divergence_reasons=divergence_reasons,
            source_warnings=source_warnings,
        )
        return snapshot, issues


def _require_compatible_version(version: str) -> None:
    match = _SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise BeadsError(
            f"bd reported unrecognized version {version!r}; emBEADings requires Beads "
            f">={MINIMUM_BEADS_VERSION} and the read-only JSON capability contract: "
            f"{BEADS_CAPABILITY_CONTRACT}."
        )
    detected = tuple(int(match.group(field)) for field in ("major", "minor", "patch"))
    minimum = tuple(int(part) for part in MINIMUM_BEADS_VERSION.split("."))
    prerelease = match.group("suffix").startswith("-")
    if detected < minimum or (detected == minimum and prerelease):
        raise BeadsError(
            f"Beads {version} is unsupported; emBEADings requires Beads "
            f">={MINIMUM_BEADS_VERSION}. Upgrade bd and verify the read-only JSON "
            f"capability contract: {BEADS_CAPABILITY_CONTRACT}."
        )


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _normalized_update_marker(value: str) -> str:
    """Normalize supported Beads timestamps to UTC, rounded to whole seconds."""

    stripped = value.strip()
    match = _RFC3339_PATTERN.fullmatch(stripped)
    if match is None:
        return stripped
    try:
        offset = match.group("offset")
        parsed = datetime.fromisoformat(
            f"{match.group('date')}{'+00:00' if offset == 'Z' else offset}"
        )
    except ValueError:
        return stripped
    fraction = match.group("fraction") or ""
    # Beads 1.0.x rounds nanosecond JSONL timestamps to whole seconds when
    # importing into Dolt. Compare at that supported round-trip precision.
    if fraction and fraction[0] >= "5":
        parsed += timedelta(seconds=1)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_state_marker(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        identifier = _first(record, "id", "issue_id")
        status = _first(record, "status")
        issue_type = _first(record, "issue_type", "type") or ""
        priority = record.get("priority")
        updated_at = _first(record, "updated_at", "updatedAt") or ""
        ephemeral = record.get("ephemeral", False)
        dependencies_value = _first(record, "dependencies", "depends_on", "dependency_ids")
    else:
        identifier = getattr(record, "id", None)
        status = getattr(record, "status", None)
        issue_type = getattr(record, "issue_type", "") or ""
        priority = getattr(record, "priority", None)
        updated_at = getattr(record, "updated_at", "") or ""
        ephemeral = getattr(record, "ephemeral", False)
        dependencies_value = None
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("state digest record contains no valid ID")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("state digest record contains no valid status")
    if not isinstance(issue_type, str):
        raise ValueError("state digest record contains an invalid issue type")
    if not isinstance(updated_at, str):
        raise ValueError("state digest record contains an invalid update marker")
    if not isinstance(ephemeral, bool):
        raise ValueError("state digest record contains invalid ephemeral metadata")
    try:
        normalized_priority = _parse_priority(priority)
        if isinstance(record, Mapping):
            _, links = _parse_dependencies(dependencies_value, source_id=identifier.strip())
        else:
            links = getattr(record, "dependency_links", ())
    except BeadsError as exc:
        raise ValueError("state digest record contains invalid structural metadata") from exc
    dependency_markers = sorted(
        (link.target_id, link.relationship_type.strip().casefold()) for link in links
    )
    return {
        "id": identifier.strip(),
        "status": status.strip().casefold(),
        "issue_type": issue_type.strip().casefold(),
        "priority": normalized_priority,
        "updated_at": _normalized_update_marker(updated_at),
        "ephemeral": ephemeral,
        "dependencies": dependency_markers,
    }


def _canonical_state_markers(records: Sequence[Any]) -> list[dict[str, Any]]:
    return sorted(
        (_canonical_state_marker(record) for record in records), key=lambda item: item["id"]
    )


def _canonical_state_digest(records: Sequence[Any]) -> str:
    markers = _canonical_state_markers(records)
    encoded = json.dumps(markers, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _state_divergence_reasons(
    live_records: Sequence[Any], export_records: Sequence[Any]
) -> tuple[str, ...]:
    live = {marker["id"]: marker for marker in _canonical_state_markers(live_records)}
    export = {marker["id"]: marker for marker in _canonical_state_markers(export_records)}
    reasons: set[str] = set()
    if len(live_records) != len(export_records):
        reasons.add("record_count")
    if live.keys() != export.keys():
        reasons.add("issue_identity")
    categories = {
        "status": "status",
        "issue_type": "issue_type",
        "priority": "priority",
        "updated_at": "update_marker",
        "ephemeral": "ephemeral",
        "dependencies": "dependency_structure",
    }
    for identifier in live.keys() & export.keys():
        for field, category in categories.items():
            if live[identifier][field] != export[identifier][field]:
                reasons.add(category)
    return tuple(sorted(reasons))


def _export_diagnostics(
    workspace_path: str | None,
    *,
    live_records: Sequence[Any],
    live_digest: str,
) -> tuple[int | None, str | None, tuple[str, ...], tuple[str, ...]]:
    if not workspace_path:
        return None, None, (), ()
    export_path = Path(workspace_path) / "issues.jsonl"
    if not export_path.is_file():
        return None, None, (), ()

    records: list[Mapping[str, Any]] = []
    try:
        with export_path.open(encoding="utf-8") as export:
            for line in export:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, Mapping):
                    raise ValueError("export record is not an object")
                records.append(record)
        export_digest = _canonical_state_digest(records)
        divergence_reasons = _state_divergence_reasons(live_records, records)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, None, (), ("A discoverable Beads JSONL export could not be inspected safely.",)

    count = len(records)
    live_count = len(live_records)
    if count == live_count:
        if export_digest == live_digest:
            return count, export_digest, (), ()
        return (
            count,
            export_digest,
            divergence_reasons,
            (
                "Live Beads data and the discoverable JSONL export have matching issue counts but "
                "different canonical state digests; live data was used.",
            ),
        )
    return (
        count,
        export_digest,
        divergence_reasons,
        (
            f"Live Beads data contains {live_count} issues while the discoverable JSONL export "
            f"contains {count}; live data was used.",
        ),
    )


def _issue_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        present = [key for key in ("issues", "items", "data") if key in payload]
        if len(present) != 1 or not isinstance(payload[present[0]], list):
            raise BeadsError("bd list JSON has an unsupported shape")
        return payload[present[0]]
    raise BeadsError("bd list JSON must be an array or issue envelope")


def _required_string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BeadsError(f"issue {key} must be a non-empty string")
    return value.strip()


def _optional_string(item: Mapping[str, Any], *keys: str) -> str:
    value = _first(item, *keys)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BeadsError(f"issue field {keys[0]} must be a string")
    return value


def _parse_priority(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BeadsError("issue priority must be an integer from 0 to 4")
    if isinstance(value, str) and value.upper().startswith("P"):
        value = value[1:]
    try:
        priority = int(value)
    except (TypeError, ValueError) as exc:
        raise BeadsError("issue priority must be an integer from 0 to 4") from exc
    if priority not in range(5):
        raise BeadsError("issue priority must be an integer from 0 to 4")
    return priority


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise BeadsError(f"issue {field_name} must be a boolean")
    return value


def _parse_labels(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BeadsError("issue labels must be an array")
    labels: list[str] = []
    for label in value:
        if isinstance(label, str):
            name = label
        elif isinstance(label, Mapping) and isinstance(label.get("name"), str):
            name = label["name"]
        else:
            raise BeadsError("issue label has an unsupported shape")
        if not name.strip():
            raise BeadsError("issue labels must be non-empty")
        labels.append(name.strip())
    return tuple(sorted(set(labels)))


def _parse_dependencies(
    value: Any, *, source_id: str
) -> tuple[tuple[str, ...], tuple[DependencyLink, ...]]:
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise BeadsError("issue dependencies must be an array")
    dependencies: list[str] = []
    links: list[DependencyLink] = []
    for dependency in value:
        if isinstance(dependency, str):
            dependency_id = dependency
            relationship_type = "depends-on"
        elif isinstance(dependency, Mapping):
            # Current Beads payloads contain both ``issue_id`` (the source) and
            # ``depends_on_id`` (the target). Target-specific fields must win.
            dependency_id = _first(
                dependency, "depends_on_id", "dependency_id", "target_id", "id", "issue_id"
            )
            relationship_type = (
                _first(dependency, "type", "dependency_type", "relationship_type") or "depends-on"
            )
        else:
            raise BeadsError("issue dependency has an unsupported shape")
        if not isinstance(dependency_id, str) or not dependency_id.strip():
            raise BeadsError("issue dependency contains no valid ID")
        if not isinstance(relationship_type, str) or not relationship_type.strip():
            raise BeadsError("issue dependency contains no valid relationship type")
        target_id = dependency_id.strip()
        if target_id == source_id:
            raise BeadsError(f"issue {source_id} contains a self-dependency")
        dependencies.append(target_id)
        links.append(
            DependencyLink(
                source_id=source_id,
                target_id=target_id,
                relationship_type=relationship_type.strip(),
            )
        )
    ordered_links = tuple(
        sorted(
            set(links),
            key=lambda link: (link.target_id, link.relationship_type, link.source_id),
        )
    )
    return tuple(sorted(set(dependencies))), ordered_links


def _parse_issue(raw: Any) -> IssueRecord:
    if not isinstance(raw, Mapping):
        raise BeadsError("each issue must be a JSON object")
    parent = _first(raw, "parent_id", "parent")
    if isinstance(parent, Mapping):
        parent = _first(parent, "id", "issue_id")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise BeadsError("issue parent must be an issue ID")

    issue_id = _required_string(raw, "id")
    dependencies, dependency_links = _parse_dependencies(
        _first(raw, "dependencies", "depends_on", "dependency_ids"), source_id=issue_id
    )
    return IssueRecord(
        id=issue_id,
        title=_required_string(raw, "title"),
        description=_optional_string(raw, "description", "body"),
        status=_required_string(raw, "status"),
        issue_type=_optional_string(raw, "issue_type", "type").strip(),
        priority=_parse_priority(raw.get("priority")),
        labels=_parse_labels(raw.get("labels")),
        parent_id=parent.strip() if isinstance(parent, str) else None,
        dependencies=dependencies,
        dependency_links=dependency_links,
        acceptance_criteria=_optional_string(
            raw, "acceptance_criteria", "acceptanceCriteria", "acceptance"
        ),
        design=_optional_string(raw, "design", "design_notes"),
        notes=_optional_string(raw, "notes", "current_notes"),
        close_reason=_optional_string(raw, "close_reason"),
        updated_at=_optional_string(raw, "updated_at", "updatedAt"),
        assignee=_optional_string(raw, "assignee").strip(),
        ephemeral=_parse_bool(raw.get("ephemeral"), field_name="ephemeral"),
    )
