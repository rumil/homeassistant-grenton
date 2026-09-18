"""Shared normalization for Grenton labels used as device/entity names."""

from __future__ import annotations


def normalize_label(value: str | None) -> str | None:
    """Trim leading/trailing whitespace from a Grenton label.

    Used in the single shared places where a label becomes a device or entity
    name, so mappers don't each have to strip. A label that is empty (or only
    whitespace) becomes ``None`` so the device/entity falls back to its default
    name instead of showing a blank. This only affects the display name; it is
    never applied to ``unique_id``s, which are derived from widget ids.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
