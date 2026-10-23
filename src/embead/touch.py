"""When did a person last touch a record, as opposed to a bulk agent pass?

Bulk agent passes ("NIGHT TRIAGE: P1 -> P2", "OWNERSHIP SWEEP: ...") append a note to hundreds of
records at once.  Each one bumps ``updated_at``, so a record nobody has looked at in months reads as
fresh.  ``last_human_touch`` is the newest dated comment or note entry that does not start with a
known agent prefix, falling back to ``created_at``.  ``updated_at`` is never used, because an agent
note, a claim, or a status sweep moves it just as well as a person does.

Note entries are lines; an indented or bulleted line continues the entry above it.  An entry counts
only when a date or timestamp appears near its start (``2026-10-21: ...``, ``RECONCILED 2026-07-23
with ...``) and falls between the record's creation and its last update: a date outside that window
is a reference to some other day, not the day the note was written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any

DEFAULT_AGENT_NOTE_PREFIXES = ("NIGHT TRIAGE:", "URGENCY REGRADE", "OWNERSHIP SWEEP:")
# A note's own date sits near its start; a date later in the line is usually a reference.
_DATE_WINDOW = 80
_STAMP_RE = re.compile(
    r"(?<![\d-])(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[T ](?P<time>\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?\s*(?P<zone>Z|UTC|[+-]\d{2}:?\d{2})?)?"
    r"(?![\d])"
)
# Stamps and punctuation an agent may put before its prefix: "2026-10-21 NIGHT TRIAGE: ...".
_LEADING_STAMP_RE = re.compile(r"^[\s\[(]*(?:\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+\S*)?[\s\])\-:,]*)?")
_CONTINUATION_RE = re.compile(r"^(?:\s+|[-*•]\s)")


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = _STAMP_RE.match(text)
        return _stamp_instant(match) if match else None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp_instant(match: re.Match[str]) -> datetime | None:
    try:
        day = date.fromisoformat(match.group("date"))
    except ValueError:
        return None
    clock = match.group("time") or "00:00"
    zone = match.group("zone") or "Z"
    zone = "+00:00" if zone in {"Z", "UTC"} else zone
    if ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        parsed = datetime.fromisoformat(f"{day.isoformat()}T{clock}{zone}")
    except ValueError:
        return None
    return parsed


def _format(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_agent_entry(text: str, prefixes: Sequence[str]) -> bool:
    """True when ``text`` (after any leading date stamp) starts with an agent prefix."""

    head = _LEADING_STAMP_RE.sub("", text, count=1).lstrip().casefold()
    return any(head.startswith(prefix.strip().casefold()) for prefix in prefixes if prefix.strip())


def note_entries(notes: str) -> list[str]:
    """Split notes into entries: one per line, with indented or bulleted lines continuing one."""

    entries: list[str] = []
    for line in notes.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        if entries and _CONTINUATION_RE.match(line):
            entries[-1] += "\n" + line
        else:
            entries.append(line.strip())
    return entries


def _entry_date(
    entry: str, *, earliest: datetime | None, latest: datetime | None
) -> datetime | None:
    match = _STAMP_RE.search(entry.split("\n", 1)[0][:_DATE_WINDOW])
    if match is None:
        return None
    instant = _stamp_instant(match)
    if instant is None:
        return None
    precise = bool(match.group("time"))
    # A bare date is a whole day: compare it with the window's days, not its instants.
    if earliest and (instant < earliest if precise else instant.date() < earliest.date()):
        return None
    if latest and (instant > latest if precise else instant.date() > latest.date()):
        return None
    return instant


def last_human_touch(
    issue: Any,
    prefixes: Sequence[str] = DEFAULT_AGENT_NOTE_PREFIXES,
    comments: Iterable[Any] | None = None,
) -> tuple[str | None, str]:
    """Return ``(timestamp, source)``: source is ``comment``, ``note``, ``created``, or ``unknown``.

    ``comments`` defaults to the record's own ``comments``; each needs ``created_at`` and ``text``.
    """

    created = _instant(getattr(issue, "created_at", ""))
    updated = _instant(getattr(issue, "updated_at", ""))
    best: tuple[datetime, str] | None = None

    def offer(instant: datetime | None, source: str) -> None:
        nonlocal best
        if instant is not None and (best is None or instant > best[0]):
            best = (instant, source)

    for comment in getattr(issue, "comments", ()) if comments is None else comments:
        text = str(getattr(comment, "text", "") or "")
        if not is_agent_entry(text, prefixes):
            offer(_instant(getattr(comment, "created_at", "")), "comment")
    for entry in note_entries(str(getattr(issue, "notes", "") or "")):
        if not is_agent_entry(entry, prefixes):
            offer(_entry_date(entry, earliest=created, latest=updated), "note")
    if best is None or (created is not None and created >= best[0]):
        return (_format(created), "created") if created else (None, "unknown")
    return _format(best[0]), best[1]
