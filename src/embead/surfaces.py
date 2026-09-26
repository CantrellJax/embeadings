"""Revision-bound code-surface evidence for active work records.

The module deliberately indexes pointers, never source contents.  Explicit paths come from the
work record, while observed paths come from local Git worktrees associated with an issue.  The
result is advisory collision evidence suitable for a read-only review queue.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .identifiers import IssueIdResolver, prune_ancestors


class GitRunner(Protocol):
    def __call__(self, cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


def _default_git_runner(cwd: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


_CODE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".md",
        ".php",
        ".proto",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sql",
        ".svelte",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_IGNORED_PARTS = frozenset(
    {
        ".beads",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "dist",
        "node_modules",
    }
)
_GENERIC_ROOTS = frozenset({"app", "apps", "docs", "lib", "packages", "src", "test", "tests"})
_DIRECTORY_ROOTS = _GENERIC_ROOTS | frozenset(
    {".github", "backend", "cmd", "frontend", "internal", "pkg", "scripts", "services"}
)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"(?m)^[ \t]*(?:```|~~~)")
_PATH_RE = re.compile(
    r"(?<![\w:/.-])"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9]+|/))"
    r"(?:(?P<separator>::|#)(?P<symbol>[A-Za-z_][A-Za-z0-9_.-]*))?"
)
_ROOT_PATH_RE = re.compile(
    r"`(?P<path>[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)"
    r"(?:(?P<separator>::|#)(?P<symbol>[A-Za-z_][A-Za-z0-9_.-]*))?`"
)
_EDIT_INTENT_RE = re.compile(
    r"\b(?:add|change|create|edit|fix|implement|migrate|modify|move|refactor|remove|rename|"
    r"repair|replace|rewrite|update|write)\b",
    re.IGNORECASE,
)
_REFERENCE_INTENT_RE = re.compile(
    r"\b(?:audit|check|document|inspect|inventory|read|reference|review|see|trace|verify)\b",
    re.IGNORECASE,
)

# A worktree is evidence of current implementation, not an unbounded history report.  A valid but
# stale base can otherwise turn most of a repository into "observed" evidence and create a
# quadratic collision queue.  Working-tree and untracked paths are deliberately not subject to
# this historical-diff guard.
_MAX_COMMITTED_DIFF_PATHS = 250

# Ascending strength: explicit (both sides are prose mentions), corroborated (one side observed in a
# worktree), observed (both sides observed).
CONFIDENCE_LEVELS = ("explicit", "corroborated", "observed")


@dataclass(frozen=True, slots=True)
class CodePointer:
    issue_id: str
    path: str
    source: str
    confidence: str
    revision: str | None
    symbol: str | None = None
    source_field: str | None = None
    edit_intent: str = "unknown"
    context_kind: str = "prose"
    path_presence: str = "unavailable"


@dataclass(frozen=True, slots=True)
class CodeSurfaceCollision:
    issue_id: str
    related_issue_id: str
    kind: str
    confidence: str
    shared_paths: tuple[str, ...]
    shared_symbols: tuple[str, ...]
    shared_modules: tuple[str, ...]
    evidence_sources: tuple[str, ...]
    edit_intent: str
    intent_source_fields: tuple[str, ...]
    context_kind: str
    path_presence: str
    revision_relation: str
    what_to_verify: str


@dataclass(frozen=True, slots=True)
class CodeSurfaceAnalysis:
    repository_available: bool
    repository_context: str
    repository_revision: str | None
    base_reference: str | None
    base_revision: str | None
    issue_count: int
    pointer_count: int
    issues_with_explicit_surfaces: int
    issues_with_observed_surfaces: int
    issues_without_surfaces: int
    worktrees_discovered: int
    worktrees_associated: int
    source_counts: dict[str, int]
    context_counts: dict[str, int]
    path_presence_counts: dict[str, int]
    hub_surface_limit: int
    hub_surfaces: tuple[dict[str, Any], ...]
    pairs_omitted_by_hub_guard: int
    pairs_omitted_by_module_guard: int
    surfaces: tuple[dict[str, Any], ...]
    collisions: tuple[CodeSurfaceCollision, ...]
    hub_guard_sample: tuple[dict[str, Any], ...] = ()
    min_confidence: str = "explicit"
    pairs_omitted_by_confidence_filter: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "collisions": [asdict(item) for item in self.collisions],
        }


@dataclass(frozen=True, slots=True)
class _Worktree:
    path: Path
    head: str | None
    branch: str | None


@dataclass(frozen=True, slots=True)
class _ChangedPaths:
    paths: tuple[str, ...]
    base_diff_count: int = 0
    base_diff_excluded: bool = False
    base_diff_failed: bool = False


def extract_explicit_pointers(
    issue: Any,
    *,
    revision: str | None,
    repository_path: str | Path | None = None,
) -> tuple[CodePointer, ...]:
    """Extract conservative repository-relative path and optional symbol references."""

    issue_id = str(getattr(issue, "id", ""))
    fields = (
        ("title", getattr(issue, "title", "")),
        ("description", getattr(issue, "description", "")),
        ("acceptance_criteria", getattr(issue, "acceptance_criteria", "")),
        ("design", getattr(issue, "design", "")),
        ("notes", getattr(issue, "notes", "")),
    )
    pointers: set[CodePointer] = set()
    repository = Path(repository_path).resolve() if repository_path is not None else None
    for field, raw in fields:
        text = _mask_html_comments(_URL_RE.sub(" ", str(raw or "")))
        for match in (*_PATH_RE.finditer(text), *_ROOT_PATH_RE.finditer(text)):
            normalized = _normalize_path(match.group("path"))
            if normalized is None:
                continue
            pointers.add(
                CodePointer(
                    issue_id=issue_id,
                    path=normalized,
                    symbol=match.group("symbol") or None,
                    source="explicit-reference",
                    confidence="explicit",
                    revision=revision,
                    source_field=field,
                    edit_intent=_explicit_edit_intent(text, match.start(), match.end()),
                    context_kind=_explicit_context_kind(text, match.start()),
                    path_presence=_path_presence(repository, normalized),
                )
            )
    return tuple(sorted(pointers, key=_pointer_key))


def analyze_code_surfaces(
    issues: Sequence[Any],
    *,
    workspace_path: str | Path | None,
    invocation_path: str | Path | None = None,
    worktree_mappings: Mapping[str, str | Path] | None = None,
    base_reference: str = "origin/main",
    runner: GitRunner = _default_git_runner,
    max_collision_evidence: int = 8,
    hub_surface_limit: int = 5,
    hub_guard_sample_limit: int = 0,
    eligible_issue_ids: frozenset[str] | None = None,
    branch_patterns: Sequence[re.Pattern[str]] = (),
    min_confidence: str = "explicit",
) -> CodeSurfaceAnalysis:
    """Build explicit and observed surfaces, then derive bounded pairwise collisions."""

    if min_confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f"min_confidence must be one of {', '.join(CONFIDENCE_LEVELS)}")
    if hub_surface_limit < 1:
        raise ValueError("hub surface issue limit must be positive")
    if hub_guard_sample_limit < 0:
        raise ValueError("hub guard sample limit cannot be negative")
    ordered_issues = sorted(issues, key=lambda item: str(getattr(item, "id", "")))
    issue_ids = tuple(str(getattr(issue, "id", "")) for issue in ordered_issues)
    warnings: list[str] = []
    tracker_repo = Path(workspace_path).resolve() if workspace_path else None
    repo, repository_context, context_warning = _select_repository_context(
        tracker_repo,
        Path(invocation_path).resolve() if invocation_path else None,
        runner,
    )
    if context_warning:
        warnings.append(context_warning)
    repository_available = bool(repo and _is_git_repository(repo, runner))
    revision = _git_value(repo, ["rev-parse", "HEAD"], runner) if repository_available else None
    effective_base: str | None = None
    base_revision: str | None = None
    worktrees: tuple[_Worktree, ...] = ()
    associations: dict[str, _Worktree] = {}
    if repository_available:
        effective_base = _resolve_base_reference(repo, base_reference, runner)
        if effective_base is None:
            warnings.append(
                "No local base reference was available; observed surfaces use working changes only."
            )
        else:
            base_revision = _git_value(repo, ["rev-parse", effective_base], runner)
    else:
        warnings.append(
            "No Git repository was available; only explicit code references were analyzed."
        )

    pointers: list[CodePointer] = []
    for issue in ordered_issues:
        pointers.extend(
            extract_explicit_pointers(
                issue,
                revision=revision,
                repository_path=repo if repository_available else None,
            )
        )

    if repository_available and repo is not None:
        worktrees = _list_worktrees(repo, runner)
        associations = _associate_worktrees(
            issue_ids,
            worktrees,
            worktree_mappings or {},
            branch_patterns,
        )
        for issue_id, worktree in sorted(associations.items()):
            changed_paths = _changed_paths(worktree, effective_base, runner)
            if changed_paths.base_diff_excluded:
                warnings.append(
                    f"Observed committed diff for issue {issue_id} was excluded: base reference "
                    f"'{effective_base}' produced {changed_paths.base_diff_count} eligible "
                    f"code-surface paths, above the safety limit of {_MAX_COMMITTED_DIFF_PATHS}. "
                    "Tracked and untracked working-tree changes remain included; choose a current "
                    "--base-ref to restore committed-change evidence."
                )
            elif changed_paths.base_diff_failed:
                warnings.append(
                    f"Observed committed diff for issue {issue_id} could not be computed from base "
                    f"reference '{effective_base}'. Tracked and untracked working-tree changes "
                    "remain included; verify --base-ref for committed-change evidence."
                )
            for changed_path in changed_paths.paths:
                normalized = _normalize_git_path(changed_path)
                if normalized is None:
                    continue
                pointers.append(
                    CodePointer(
                        issue_id=issue_id,
                        path=normalized,
                        source="active-worktree-diff",
                        confidence="observed",
                        revision=worktree.head,
                        edit_intent="observed-edit",
                        context_kind="observed-worktree",
                        path_presence=_path_presence(worktree.path, normalized),
                    )
                )

    pointers = sorted(set(pointers), key=_pointer_key)
    by_issue: dict[str, list[CodePointer]] = defaultdict(list)
    for pointer in pointers:
        by_issue[pointer.issue_id].append(pointer)
    collisions, hub_surfaces, hub_omissions, module_omissions, hub_guard_sample = _collisions(
        by_issue,
        max_evidence=max_collision_evidence,
        hub_surface_limit=hub_surface_limit,
        hub_guard_sample_limit=hub_guard_sample_limit,
        eligible_issue_ids=eligible_issue_ids,
    )
    # Confidence is a pair property, so the filter runs after pairing and is counted, never silent.
    floor = CONFIDENCE_LEVELS.index(min_confidence)
    retained = tuple(
        item for item in collisions if CONFIDENCE_LEVELS.index(item.confidence) >= floor
    )
    omitted_by_confidence = len(collisions) - len(retained)
    collisions = retained
    explicit_ids = {
        pointer.issue_id for pointer in pointers if pointer.source == "explicit-reference"
    }
    observed_ids = {
        pointer.issue_id for pointer in pointers if pointer.source == "active-worktree-diff"
    }
    explicit_pointers = [pointer for pointer in pointers if pointer.source == "explicit-reference"]
    if (
        repository_available
        and explicit_pointers
        and all(pointer.path_presence == "missing" for pointer in explicit_pointers)
    ):
        warnings.append(
            "No explicit code pointers resolved in the selected repository; verify that the "
            "tracker is attached to its implementation checkout before treating explicit-only "
            "leads as edit collisions."
        )
    surfaces = tuple(
        {
            "issue_id": issue_id,
            "pointers": [asdict(pointer) for pointer in by_issue[issue_id]],
        }
        for issue_id in sorted(by_issue)
    )
    return CodeSurfaceAnalysis(
        repository_available=repository_available,
        repository_context=repository_context,
        repository_revision=revision,
        base_reference=effective_base,
        base_revision=base_revision,
        issue_count=len(ordered_issues),
        pointer_count=len(pointers),
        issues_with_explicit_surfaces=len(explicit_ids),
        issues_with_observed_surfaces=len(observed_ids),
        issues_without_surfaces=len(set(issue_ids) - set(by_issue)),
        worktrees_discovered=len(worktrees),
        worktrees_associated=len(associations),
        source_counts=dict(sorted(Counter(pointer.source for pointer in pointers).items())),
        context_counts=dict(sorted(Counter(pointer.context_kind for pointer in pointers).items())),
        path_presence_counts=dict(
            sorted(Counter(pointer.path_presence for pointer in pointers).items())
        ),
        hub_surface_limit=hub_surface_limit,
        hub_surfaces=hub_surfaces,
        pairs_omitted_by_hub_guard=hub_omissions,
        pairs_omitted_by_module_guard=module_omissions,
        hub_guard_sample=hub_guard_sample,
        min_confidence=min_confidence,
        pairs_omitted_by_confidence_filter=omitted_by_confidence,
        surfaces=surfaces,
        collisions=collisions,
        warnings=tuple(warnings),
    )


def parse_worktree_mappings(values: Iterable[str]) -> dict[str, Path]:
    """Parse repeatable ``ISSUE_ID=PATH`` command-line mappings."""

    mappings: dict[str, Path] = {}
    for value in values:
        issue_id, separator, raw_path = value.partition("=")
        if not separator or not issue_id.strip() or not raw_path.strip():
            raise ValueError("worktree mappings must use ISSUE_ID=PATH")
        issue_id = issue_id.strip()
        if issue_id in mappings:
            raise ValueError(f"duplicate worktree mapping for {issue_id}")
        mappings[issue_id] = Path(raw_path).expanduser().resolve()
    return mappings


def _normalize_path(value: str) -> str | None:
    raw = value.strip("`'\"()[]{}<>,;:").replace("\\", "/")
    is_directory = raw.endswith("/")
    raw = raw.rstrip("/")
    if not raw or len(raw) > 240 or raw.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:", raw):
        return None
    path = PurePosixPath(raw)
    if any(part in {"", ".", ".."} or part in _IGNORED_PARTS for part in path.parts):
        return None
    if is_directory and path.parts[0] not in _DIRECTORY_ROOTS:
        return None
    if not is_directory and path.suffix.casefold() not in _CODE_EXTENSIONS:
        return None
    return path.as_posix() + ("/" if is_directory else "")


def _normalize_git_path(value: str) -> str | None:
    normalized = _normalize_path(value)
    if normalized is not None:
        return normalized
    raw = value.strip().replace("\\", "/").rstrip("/")
    path = PurePosixPath(raw)
    invalid_part = any(part in {".", ".."} or part in _IGNORED_PARTS for part in path.parts)
    if not raw or raw.startswith("/") or invalid_part:
        return None
    return path.as_posix()


def _module_for(path: str) -> str | None:
    parts = PurePosixPath(path.rstrip("/")).parts
    directories = parts if path.endswith("/") else parts[:-1]
    if len(directories) < 2:
        return None
    if directories[0] in _GENERIC_ROOTS:
        return "/".join(directories[:2])
    return "/".join(directories[:2])


def _mask_html_comments(text: str) -> str:
    """Remove issue-template prose while preserving offsets for bounded context checks."""

    return _HTML_COMMENT_RE.sub(lambda match: " " * len(match.group()), text)


def _explicit_context_kind(text: str, position: int) -> str:
    fences_before = sum(match.start() < position for match in _FENCE_RE.finditer(text))
    return "code-fence" if fences_before % 2 else "prose"


def _path_presence(repository: Path | None, path: str) -> str:
    if repository is None:
        return "unavailable"
    return "existing" if (repository / path.rstrip("/")).exists() else "missing"


def _explicit_edit_intent(text: str, start: int, end: int) -> str:
    """Classify bounded local wording without retaining the surrounding text."""

    context = text[max(0, start - 80) : min(len(text), end + 40)]
    if _EDIT_INTENT_RE.search(context):
        return "likely-edit"
    if _REFERENCE_INTENT_RE.search(context):
        return "reference-only"
    return "unknown"


def _collision_edit_intent(pointers: Sequence[CodePointer]) -> str:
    intents = {pointer.edit_intent for pointer in pointers}
    if "observed-edit" in intents:
        return "observed-edit"
    if "likely-edit" in intents and "reference-only" in intents:
        return "mixed"
    if "likely-edit" in intents:
        return "likely-edit"
    if "reference-only" in intents:
        return "reference-only"
    return "unknown"


def _collision_context_kind(pointers: Sequence[CodePointer]) -> str:
    contexts = {pointer.context_kind for pointer in pointers}
    if "observed-worktree" in contexts:
        return "observed-worktree"
    if contexts == {"code-fence"}:
        return "code-fence-only"
    if "code-fence" in contexts and "prose" in contexts:
        return "mixed"
    return "prose"


def _collision_path_presence(pointers: Sequence[CodePointer]) -> str:
    presence = {pointer.path_presence for pointer in pointers}
    available = presence - {"unavailable"}
    if not available:
        return "unavailable"
    if available == {"existing"} and "unavailable" not in presence:
        return "all-existing"
    if available == {"missing"} and "unavailable" not in presence:
        return "all-missing"
    return "mixed"


def _collisions(
    by_issue: Mapping[str, Sequence[CodePointer]],
    *,
    max_evidence: int,
    hub_surface_limit: int,
    hub_guard_sample_limit: int = 0,
    eligible_issue_ids: frozenset[str] | None = None,
) -> tuple[
    tuple[CodeSurfaceCollision, ...],
    tuple[dict[str, Any], ...],
    int,
    int,
    tuple[dict[str, Any], ...],
]:
    path_issues: dict[str, set[str]] = defaultdict(set)
    module_issues: dict[str, set[str]] = defaultdict(set)
    for issue_id, pointers in by_issue.items():
        for pointer in pointers:
            if not pointer.path.endswith("/"):
                path_issues[pointer.path].add(issue_id)
            module = _module_for(pointer.path)
            if module:
                module_issues[module].add(issue_id)
    hub_paths = {path for path, members in path_issues.items() if len(members) > hub_surface_limit}
    hub_modules = {
        module for module, members in module_issues.items() if len(members) > hub_surface_limit
    }
    hub_surfaces = tuple(
        sorted(
            (
                {"kind": "path", "surface": path, "issue_count": len(path_issues[path])}
                for path in hub_paths
            ),
            key=lambda item: (-item["issue_count"], item["surface"]),
        )
        + sorted(
            (
                {
                    "kind": "module",
                    "surface": module,
                    "issue_count": len(module_issues[module]),
                }
                for module in hub_modules
            ),
            key=lambda item: (-item["issue_count"], item["surface"]),
        )
    )
    results: list[CodeSurfaceCollision] = []
    omitted_by_hub_guard = 0
    omitted_by_module_guard = 0
    hub_guard_sample: list[dict[str, Any]] = []
    issue_ids = sorted(by_issue)
    for index, issue_id in enumerate(issue_ids):
        for related_id in issue_ids[index + 1 :]:
            if eligible_issue_ids is not None and not ({issue_id, related_id} & eligible_issue_ids):
                continue
            left = by_issue[issue_id]
            right = by_issue[related_id]
            left_paths = {pointer.path for pointer in left if not pointer.path.endswith("/")}
            right_paths = {pointer.path for pointer in right if not pointer.path.endswith("/")}
            all_shared_paths = left_paths & right_paths
            left_symbols = {
                f"{pointer.path}::{pointer.symbol}" for pointer in left if pointer.symbol
            }
            right_symbols = {
                f"{pointer.path}::{pointer.symbol}" for pointer in right if pointer.symbol
            }
            shared_symbols = sorted(left_symbols & right_symbols)[:max_evidence]
            left_modules = {module for pointer in left if (module := _module_for(pointer.path))}
            right_modules = {module for pointer in right if (module := _module_for(pointer.path))}
            observed_shared_paths = {
                path
                for path in all_shared_paths
                if any(
                    pointer.path == path and pointer.source == "active-worktree-diff"
                    for pointer in (*left, *right)
                )
            }
            shared_paths = sorted(
                path
                for path in all_shared_paths
                if path not in hub_paths or path in observed_shared_paths
            )[:max_evidence]
            symbol_paths = {symbol.rsplit("::", 1)[0] for symbol in shared_symbols}
            shared_paths = sorted(set(shared_paths) | symbol_paths)[:max_evidence]
            non_hub_shared_modules = sorted(
                module for module in left_modules & right_modules if module not in hub_modules
            )[:max_evidence]
            observed_modules = {
                module
                for pointer in (*left, *right)
                if pointer.source == "active-worktree-diff"
                and (module := _module_for(pointer.path))
            }
            shared_modules = [
                module for module in non_hub_shared_modules if module in observed_modules
            ]
            if not shared_paths and not shared_symbols and not non_hub_shared_modules:
                if all_shared_paths or left_modules & right_modules:
                    omitted_by_hub_guard += 1
                    if len(hub_guard_sample) < hub_guard_sample_limit:
                        hub_guard_sample.append(
                            {
                                "issue_id": issue_id,
                                "related_issue_id": related_id,
                                "suppressing_paths": sorted(all_shared_paths & hub_paths)[
                                    :max_evidence
                                ],
                                "suppressing_modules": sorted(
                                    left_modules & right_modules & hub_modules
                                )[:max_evidence],
                            }
                        )
                continue
            if (
                not shared_paths
                and not shared_symbols
                and non_hub_shared_modules
                and not shared_modules
            ):
                omitted_by_module_guard += 1
                continue
            contributing_left = [
                pointer
                for pointer in left
                if pointer.path in shared_paths
                or (pointer.symbol and f"{pointer.path}::{pointer.symbol}" in shared_symbols)
                or _module_for(pointer.path) in shared_modules
            ]
            contributing_right = [
                pointer
                for pointer in right
                if pointer.path in shared_paths
                or (pointer.symbol and f"{pointer.path}::{pointer.symbol}" in shared_symbols)
                or _module_for(pointer.path) in shared_modules
            ]
            sources = tuple(
                sorted({pointer.source for pointer in (*contributing_left, *contributing_right)})
            )
            observed_sides = (
                any(pointer.source == "active-worktree-diff" for pointer in contributing_left),
                any(pointer.source == "active-worktree-diff" for pointer in contributing_right),
            )
            confidence = (
                "observed"
                if all(observed_sides)
                else "corroborated"
                if any(observed_sides)
                else "explicit"
            )
            revisions = {pointer.revision for pointer in (*left, *right) if pointer.revision}
            edit_intent = _collision_edit_intent((*contributing_left, *contributing_right))
            context_kind = _collision_context_kind((*contributing_left, *contributing_right))
            path_presence = _collision_path_presence((*contributing_left, *contributing_right))
            intent_source_fields = tuple(
                sorted(
                    {
                        pointer.source_field
                        for pointer in (*contributing_left, *contributing_right)
                        if pointer.source_field
                    }
                )
            )
            revision_relation = (
                "unavailable" if not revisions else "same" if len(revisions) == 1 else "different"
            )
            kind = "exact-file" if shared_paths else "shared-module"
            target = (
                "the shared file paths" if shared_paths else "the shared implementation modules"
            )
            results.append(
                CodeSurfaceCollision(
                    issue_id=issue_id,
                    related_issue_id=related_id,
                    kind=kind,
                    confidence=confidence,
                    shared_paths=tuple(shared_paths),
                    shared_symbols=tuple(shared_symbols),
                    shared_modules=tuple(shared_modules),
                    evidence_sources=sources,
                    edit_intent=edit_intent,
                    intent_source_fields=intent_source_fields,
                    context_kind=context_kind,
                    path_presence=path_presence,
                    revision_relation=revision_relation,
                    what_to_verify=(
                        f"Verify whether concurrent work will modify {target} before "
                        "implementation or merge."
                    ),
                )
            )
    collisions = tuple(
        sorted(
            results,
            key=lambda item: (
                0 if item.kind == "exact-file" else 1,
                {"observed": 0, "corroborated": 1, "explicit": 2}[item.confidence],
                {
                    "observed-worktree": 0,
                    "prose": 1,
                    "mixed": 2,
                    "code-fence-only": 3,
                }[item.context_kind],
                {
                    "all-existing": 0,
                    "mixed": 1,
                    "all-missing": 2,
                    "unavailable": 3,
                }[item.path_presence],
                {
                    "observed-edit": 0,
                    "likely-edit": 1,
                    "mixed": 2,
                    "unknown": 3,
                    "reference-only": 4,
                }[item.edit_intent],
                item.issue_id,
                item.related_issue_id,
            ),
        )
    )
    return (
        collisions,
        hub_surfaces,
        omitted_by_hub_guard,
        omitted_by_module_guard,
        tuple(hub_guard_sample),
    )


def _select_repository_context(
    tracker_repo: Path | None,
    invocation_path: Path | None,
    runner: GitRunner,
) -> tuple[Path | None, str, str | None]:
    """Prefer the invoking worktree when it belongs to the tracker repository."""

    if invocation_path is None:
        return (
            (tracker_repo, "tracker-workspace", None)
            if tracker_repo is not None
            else (None, "unavailable", None)
        )

    invocation_root = _git_path(invocation_path, ["rev-parse", "--show-toplevel"], runner)
    tracker_root = (
        _git_path(tracker_repo, ["rev-parse", "--show-toplevel"], runner)
        if tracker_repo is not None
        else None
    )
    if invocation_root is not None and tracker_root is None:
        return (
            invocation_root,
            "invocation-worktree",
            "Tracker workspace is not a Git repository; provenance uses the invoking worktree.",
        )
    if invocation_root is None and tracker_root is not None:
        return (
            tracker_root,
            "tracker-workspace",
            "Invocation is outside a Git worktree; provenance uses the tracker workspace.",
        )
    if invocation_root is None or tracker_root is None:
        return None, "unavailable", None
    if invocation_root == tracker_root:
        return invocation_root, "invocation-worktree", None

    invocation_common = _git_path(
        invocation_root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        runner,
    )
    tracker_common = _git_path(
        tracker_root,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        runner,
    )
    if invocation_common is not None and invocation_common == tracker_common:
        return invocation_root, "invocation-worktree", None
    return (
        tracker_root,
        "tracker-workspace",
        "Invoking Git repository does not share the tracker repository; provenance uses the "
        "tracker workspace.",
    )


def _git_path(path: Path, arguments: Sequence[str], runner: GitRunner) -> Path | None:
    value = _git_value(path, arguments, runner)
    return Path(value).resolve() if value else None


def _is_git_repository(path: Path, runner: GitRunner) -> bool:
    result = runner(path, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_value(path: Path | None, arguments: Sequence[str], runner: GitRunner) -> str | None:
    if path is None:
        return None
    result = runner(path, arguments)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _resolve_base_reference(path: Path, requested: str, runner: GitRunner) -> str | None:
    for candidate in dict.fromkeys((requested, "main", "master")):
        result = runner(path, ["rev-parse", "--verify", candidate])
        if result.returncode == 0:
            return candidate
    return None


def _list_worktrees(path: Path, runner: GitRunner) -> tuple[_Worktree, ...]:
    result = runner(path, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return ()
    worktrees: list[_Worktree] = []
    current: dict[str, str] = {}
    for line in (*result.stdout.splitlines(), ""):
        if not line:
            if "worktree" in current:
                worktrees.append(
                    _Worktree(
                        path=Path(current["worktree"]).resolve(),
                        head=current.get("HEAD"),
                        branch=current.get("branch", "").removeprefix("refs/heads/") or None,
                    )
                )
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return tuple(sorted(worktrees, key=lambda item: str(item.path)))


def _associate_worktrees(
    issue_ids: Sequence[str],
    worktrees: Sequence[_Worktree],
    explicit: Mapping[str, str | Path],
    branch_patterns: Sequence[re.Pattern[str]] = (),
) -> dict[str, _Worktree]:
    by_path = {worktree.path: worktree for worktree in worktrees}
    associations: dict[str, _Worktree] = {}
    explicit_owners: dict[Path, str] = {}
    for issue_id, raw_path in explicit.items():
        if issue_id not in issue_ids:
            raise ValueError(
                f"worktree mapping references issue outside the evaluated population: {issue_id}; "
                "mappings require an active included issue (check --status and --include-epics, "
                "or map a currently active issue)"
            )
        path = Path(raw_path).resolve()
        if path not in by_path:
            raise ValueError(f"mapped path is not a registered Git worktree: {path}")
        if owner := explicit_owners.get(path):
            raise ValueError(
                f"mapped path is already associated with issue {owner}: {path}; "
                "one worktree can provide observed evidence for only one issue"
            )
        associations[issue_id] = by_path[path]
        explicit_owners[path] = issue_id

    resolver = IssueIdResolver(issue_ids)
    suffix_counts = Counter(_numeric_suffix(issue_id) for issue_id in issue_ids)
    for worktree in worktrees:
        if worktree.path in explicit_owners:
            continue
        branch = worktree.branch or ""
        candidates: set[str] = set(resolver.branch_candidates(branch)) if branch else set()
        for pattern in branch_patterns:
            for match in pattern.finditer(branch):
                captured = match.group("id") if "id" in pattern.groupindex else match.group(1)
                if captured and (resolved := resolver.resolve(captured)):
                    candidates.add(resolved)
        for issue_id in issue_ids:
            suffix = _numeric_suffix(issue_id)
            full_match = bool(
                re.search(
                    rf"(?:^|[/_-]){re.escape(issue_id)}(?:$|[/_-])",
                    branch,
                    re.IGNORECASE,
                )
            )
            suffix_match = bool(
                suffix
                and suffix_counts[suffix] == 1
                and re.search(
                    rf"(?:^|[/_.-])bead[-_]{re.escape(suffix)}(?:$|[/_.-])",
                    branch,
                    re.IGNORECASE,
                )
            )
            if full_match or suffix_match:
                candidates.add(issue_id)
        # A branch naming a child also spells its parent's prefix; the deepest ID is the work.
        remaining = [
            issue_id for issue_id in prune_ancestors(candidates) if issue_id not in associations
        ]
        if len(remaining) == 1:
            associations[remaining[0]] = worktree
    return associations


def compile_branch_patterns(values: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Compile ``--branch-pattern`` values; each must capture the issue ID."""

    patterns = []
    for value in values:
        try:
            pattern = re.compile(value, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid --branch-pattern {value!r}: {exc}") from exc
        if "id" not in pattern.groupindex and pattern.groups < 1:
            raise ValueError(
                f"--branch-pattern {value!r} must capture the issue ID in a group "
                "(preferably a named group: (?P<id>...))"
            )
        patterns.append(pattern)
    return tuple(patterns)


def _numeric_suffix(issue_id: str) -> str | None:
    match = re.search(r"\.(\d+)$", issue_id)
    return match.group(1) if match else None


def _changed_paths(
    worktree: _Worktree, base_reference: str | None, runner: GitRunner
) -> _ChangedPaths:
    paths: set[str] = set()
    base_diff_count = 0
    base_diff_excluded = False
    base_diff_failed = False
    if base_reference:
        base_result = runner(
            worktree.path,
            [
                "diff",
                "--name-only",
                "--diff-filter=ACMRD",
                "-z",
                f"{base_reference}...HEAD",
            ],
        )
        if base_result.returncode != 0:
            base_diff_failed = True
        else:
            base_paths = {
                value
                for value in base_result.stdout.split("\0")
                if value and _normalize_git_path(value) is not None
            }
            base_diff_count = len(base_paths)
            if base_diff_count > _MAX_COMMITTED_DIFF_PATHS:
                base_diff_excluded = True
            else:
                paths.update(base_paths)

    working_commands = [
        ["diff", "--name-only", "--diff-filter=ACMRD", "-z", "HEAD"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ]
    for command in working_commands:
        result = runner(worktree.path, command)
        if result.returncode == 0:
            paths.update(value for value in result.stdout.split("\0") if value)
    return _ChangedPaths(
        paths=tuple(sorted(paths)),
        base_diff_count=base_diff_count,
        base_diff_excluded=base_diff_excluded,
        base_diff_failed=base_diff_failed,
    )


def _pointer_key(pointer: CodePointer) -> tuple[str, ...]:
    return (
        pointer.issue_id,
        pointer.path,
        pointer.symbol or "",
        pointer.source,
        pointer.source_field or "",
        pointer.edit_intent,
        pointer.context_kind,
        pointer.path_presence,
        pointer.confidence,
        pointer.revision or "",
    )
