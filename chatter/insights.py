"""Local activity summaries for Chatter's Insights tab.

This module deliberately consumes only Chatter's own history JSONL and the
local custom dictionary. It contains no UI code so the calculations remain
easy to test and the dashboard can stay a lightweight view over existing
data rather than becoming a second analytics store.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from typing import Iterable


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?")


@dataclass(frozen=True)
class InsightSummary:
    total_words: int
    dictations: int
    average_words: int
    average_wpm: int | None
    words_today: int
    sessions_today: int
    active_days: int
    current_streak: int
    longest_streak: int
    cleanup_sessions: int
    dictionary_entries: int
    pasted_count: int
    average_processing_ms: int | None
    daily_words: tuple[tuple[date, int], ...]
    contexts: tuple[tuple[str, int], ...]


def count_words(text: str) -> int:
    """Count human-readable words without treating punctuation as words."""
    return len(_WORD_RE.findall(text or ""))


def _entry_words(entry: dict) -> int:
    stored = entry.get("word_count")
    if isinstance(stored, (int, float)) and stored >= 0:
        return int(stored)
    return count_words(str(entry.get("text", "")))


def _entry_seconds(entry: dict) -> float:
    for key in ("audio_seconds", "duration_seconds"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def _entry_date(entry: dict) -> date | None:
    try:
        timestamp = float(entry.get("ts", 0))
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _streak_length(active_days: set[date], start: date) -> int:
    length = 0
    cursor = start
    while cursor in active_days:
        length += 1
        cursor -= timedelta(days=1)
    return length


def _current_streak(active_days: set[date], today: date) -> int:
    if today in active_days:
        return _streak_length(active_days, today)
    yesterday = today - timedelta(days=1)
    if yesterday in active_days:
        return _streak_length(active_days, yesterday)
    return 0


def _longest_streak(active_days: set[date]) -> int:
    longest = 0
    for active_day in active_days:
        if active_day - timedelta(days=1) not in active_days:
            length = 1
            cursor = active_day + timedelta(days=1)
            while cursor in active_days:
                length += 1
                cursor += timedelta(days=1)
            longest = max(longest, length)
    return longest


def _context_counts(entries: Iterable[dict]) -> tuple[tuple[str, int], ...]:
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for entry in entries:
        app = str(entry.get("context_app", "")).strip()
        mode = str(entry.get("context_mode", "")).strip()
        label = app or {
            "email": "Professional email",
            "notes": "Notes / journal",
            "coding": "Coding / AI prompts",
            "social": "Social / chat",
            "browser": "Browser fields",
        }.get(mode, "Unclassified")
        key = label.casefold()
        labels.setdefault(key, label)
        counts[key] += 1
    return tuple((labels[key], count) for key, count in counts.most_common(5))


def summarize(
    entries: Iterable[dict],
    *,
    dictionary_entries: int = 0,
    days: int | None = 30,
    now: datetime | None = None,
) -> InsightSummary:
    """Return a dashboard-ready summary for local dictation history.

    ``days`` is inclusive of today. ``None`` means all history. ``now`` is
    injectable so date boundaries can be tested without depending on a clock.
    """
    current = now or datetime.now()
    today = current.date()
    all_entries = [entry for entry in entries if entry.get("kind", "dictation") == "dictation"]
    if days is None:
        filtered = all_entries
        chart_days = 14
        dated = [_entry_date(entry) for entry in all_entries]
        dated = [item for item in dated if item is not None]
        chart_anchor = max([today, *dated]) if dated else today
    else:
        cutoff = today - timedelta(days=max(days - 1, 0))
        filtered = [
            entry for entry in all_entries
            if (entry_date := _entry_date(entry)) is not None and cutoff <= entry_date <= today
        ]
        chart_days = days
        chart_anchor = today

    total_words = sum(_entry_words(entry) for entry in filtered)
    total_seconds = sum(_entry_seconds(entry) for entry in filtered)
    word_count = len(filtered)
    average_wpm = round(total_words / total_seconds * 60) if total_seconds > 0 else None
    average_words = round(total_words / word_count) if word_count else 0
    words_today = sum(_entry_words(entry) for entry in filtered if _entry_date(entry) == today)
    sessions_today = sum(1 for entry in filtered if _entry_date(entry) == today)

    active_days = {_entry_date(entry) for entry in filtered}
    active_days.discard(None)
    cleanup_sessions = sum(1 for entry in filtered if entry.get("cleanup_applied"))
    pasted_count = sum(1 for entry in filtered if entry.get("pasted"))
    processing_values = [
        float(entry["processing_ms"])
        for entry in filtered
        if isinstance(entry.get("processing_ms"), (int, float)) and entry["processing_ms"] >= 0
    ]
    average_processing_ms = round(sum(processing_values) / len(processing_values)) if processing_values else None

    daily_words: list[tuple[date, int]] = []
    for offset in range(chart_days - 1, -1, -1):
        day = chart_anchor - timedelta(days=offset)
        daily_words.append((day, sum(_entry_words(entry) for entry in filtered if _entry_date(entry) == day)))

    return InsightSummary(
        total_words=total_words,
        dictations=word_count,
        average_words=average_words,
        average_wpm=average_wpm,
        words_today=words_today,
        sessions_today=sessions_today,
        active_days=len(active_days),
        current_streak=_current_streak(active_days, today),
        longest_streak=_longest_streak(active_days),
        cleanup_sessions=cleanup_sessions,
        dictionary_entries=dictionary_entries,
        pasted_count=pasted_count,
        average_processing_ms=average_processing_ms,
        daily_words=tuple(daily_words),
        contexts=_context_counts(filtered),
    )
