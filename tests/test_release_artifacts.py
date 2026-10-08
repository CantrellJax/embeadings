from __future__ import annotations

import importlib.util
import io
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "verify_release.py"
SPEC = importlib.util.spec_from_file_location("verify_release", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
verify_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_release)


def _wheel(path: Path, version: str = "0.4.0") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"embeadings-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: embeadings\nVersion: {version}\n",
        )
    return path


def _source_archive(
    path: Path,
    version: str = "0.4.0",
    *,
    extra_files: tuple[str, ...] = (),
) -> Path:
    root = f"embeadings-{version}"
    files = {
        "LICENSE": "MIT\n",
        "PKG-INFO": f"Metadata-Version: 2.4\nName: embeadings\nVersion: {version}\n",
        "README.md": "# emBEADings\n",
        "pyproject.toml": "[build-system]\nrequires = ['hatchling']\n",
        "schemas/v1/sweep.schema.json": "{}\n",
        "src/embead/__init__.py": "\n",
    }
    files.update(dict.fromkeys(extra_files, "test content\n"))
    with tarfile.open(path, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_artifact_version_reads_wheel_metadata(tmp_path: Path) -> None:
    assert verify_release.artifact_version(_wheel(tmp_path / "package.whl")) == "0.4.0"


def test_source_artifact_version_reads_package_metadata(tmp_path: Path) -> None:
    source = _source_archive(tmp_path / "package.tar.gz")

    assert verify_release.source_artifact_version(source) == "0.4.0"
    verify_release.verify_source_contents(source)


@pytest.mark.parametrize(
    "repository_path",
    (
        ".beads/interactions.jsonl",
        ".github/workflows/release.yml",
        "benchmarks/results.json",
        "docs/research/private-pilot.md",
        "plugins/embeadings/plugin.json",
        "release.log",
    ),
)
def test_source_contents_reject_repository_only_files(tmp_path: Path, repository_path: str) -> None:
    source = _source_archive(tmp_path / "package.tar.gz", extra_files=(repository_path,))

    with pytest.raises(RuntimeError, match="repository-only content"):
        verify_release.verify_source_contents(source)


def test_source_contents_require_runtime_source_and_schemas(tmp_path: Path) -> None:
    source = _source_archive(tmp_path / "package.tar.gz")
    with tarfile.open(source, mode="w:gz") as archive:
        payload = b"Metadata-Version: 2.4\nName: embeadings\nVersion: 0.4.0\n"
        info = tarfile.TarInfo("embeadings-0.4.0/PKG-INFO")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="missing required package content"):
        verify_release.verify_source_contents(source)


def test_project_version_matches_public_package() -> None:
    assert verify_release.project_version() == "0.5.1"


def test_package_readme_uses_pypi_safe_absolute_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = [
        markdown_target or html_source
        for markdown_target, html_source in re.findall(
            r"!?\[[^]]*\]\(([^)]+)\)|<img\s+[^>]*src=\"([^\"]+)\"",
            readme,
        )
    ]

    assert targets
    assert all(target.startswith(("https://", "http://", "#", "mailto:")) for target in targets)


def test_discover_artifacts_requires_exact_pair(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "package.whl")
    source = tmp_path / "package.tar.gz"
    source.touch()

    assert verify_release.discover_artifacts(tmp_path) == (wheel, source)


def test_discover_artifacts_rejects_ambiguous_dist(tmp_path: Path) -> None:
    _wheel(tmp_path / "first.whl")
    _wheel(tmp_path / "second.whl")
    (tmp_path / "package.tar.gz").touch()

    with pytest.raises(RuntimeError, match="exactly one"):
        verify_release.discover_artifacts(tmp_path)


def test_write_checksums_is_stable(tmp_path: Path) -> None:
    wheel = tmp_path / "package.whl"
    source = tmp_path / "package.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")

    first = verify_release.write_checksums(tmp_path, (wheel, source)).read_text()
    second = verify_release.write_checksums(tmp_path, (wheel, source)).read_text()

    assert first == second
    assert "package.whl" in first
    assert "package.tar.gz" in first
