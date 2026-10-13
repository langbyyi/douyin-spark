"""Shared wall-clock schedule rules for previews and runtime dispatch."""

from datetime import UTC, date, datetime, tzinfo


def due_at(day: date, time: str, zone: tzinfo) -> datetime | None:
    """Skip DST gaps; use the first occurrence of an ambiguous wall time once."""
    value = datetime.combine(day, datetime.strptime(time, "%H:%M").time(), zone)
    restored = value.astimezone(UTC).astimezone(zone)
    return value if restored.replace(tzinfo=None) == value.replace(tzinfo=None) else None
