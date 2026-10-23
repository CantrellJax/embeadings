"""Optional user defaults, read from a TOML file outside any analyzed repository.

The file is ``$EMBEAD_CONFIG`` when set, else ``config.toml`` in the platform's user config
directory for ``embeadings`` (``~/.config/embeadings/config.toml`` on Linux,
``~/Library/Application Support/embeadings/config.toml`` on macOS).  Only these keys are read::

    [triage]
    agent_note_prefixes = ["NIGHT TRIAGE:", "URGENCY REGRADE", "OWNERSHIP SWEEP:"]

A missing file means built-in defaults.  Command-line flags replace a configured list.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from platformdirs import user_config_path

from .touch import DEFAULT_AGENT_NOTE_PREFIXES


def config_path() -> Path:
    configured = os.environ.get("EMBEAD_CONFIG")
    return (
        Path(configured).expanduser()
        if configured
        else user_config_path("embeadings") / ("config.toml")
    )


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    if not target.is_file():
        return {}
    try:
        with target.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid config file {target}: {exc}") from exc


def agent_note_prefixes(cli_values: Sequence[str] | None) -> tuple[str, ...]:
    """CLI flags, else ``[triage] agent_note_prefixes`` from the config file, else defaults."""

    if cli_values:
        return tuple(cli_values)
    section = load_config().get("triage") or {}
    configured = section.get("agent_note_prefixes") if isinstance(section, dict) else None
    if configured is None:
        return DEFAULT_AGENT_NOTE_PREFIXES
    if not isinstance(configured, list) or not all(isinstance(item, str) for item in configured):
        raise ValueError(f"{config_path()}: triage.agent_note_prefixes must be a list of strings")
    return tuple(configured)
